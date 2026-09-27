"""Small, persistent acceptance loop over Grid's background worker supervisor.

No LLM, MCP or beads dependency here. Only research jobs may overlap; change
and verification jobs hold exclusive access to the shared workspace.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any
import uuid

READ_TOOLS = frozenset({"file_read", "glob_tool", "grep_tool"})
VERIFY_TOOLS = READ_TOOLS | {"bash_tool"}
CHANGE_TOOLS = VERIFY_TOOLS | {"file_write", "file_edit", "file_append"}
TOOL_PROFILES = {
    "research": READ_TOOLS,
    "change": CHANGE_TOOLS,
    "verification": VERIFY_TOOLS,
}
ACTIVE = {"queued", "running"}


def now() -> datetime:
    return datetime.now(timezone.utc)


class PipelineRuntime:
    """Own task attempts and reviews; the supervisor owns worker execution.

    A state file is an audit record, not a resumable process checkpoint. Create
    a fresh run after a crash and inspect its predecessor before retrying writes.
    """

    def __init__(
        self,
        supervisor: Any,
        *,
        goal: str,
        workdir: Path,
        state_path: Path,
        deadline_seconds: float = 900,
        max_attempts: int = 3,
        max_launches: int = 20,
    ) -> None:
        if not goal.strip():
            raise ValueError("goal must not be empty")
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive and finite")
        if max_attempts < 1 or max_launches < 1:
            raise ValueError("attempt and launch limits must be positive")
        self.supervisor = supervisor
        self.state_path = Path(state_path).resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        # Never silently replace an earlier run, including one still in progress.
        with self.state_path.open("x", encoding="utf-8"):
            pass
        self._lock = asyncio.Lock()
        self._watchers: set[asyncio.Task] = set()
        self._deadline = asyncio.get_running_loop().time() + deadline_seconds
        self.state = {
            "version": 1,
            "run_id": f"run-{uuid.uuid4().hex[:12]}",
            "goal": goal,
            "workdir": str(Path(workdir).resolve()),
            "created_at": now().isoformat(),
            "deadline_at": (now() + timedelta(seconds=deadline_seconds)).isoformat(),
            "status": "running",
            "limits": {"max_attempts": max_attempts, "max_launches": max_launches},
            "launches": 0,
            "workspace_revision": 0,
            "tasks": {},
            "summary": None,
        }
        self._save()

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - asyncio.get_running_loop().time())

    def _save(self) -> None:
        self.state["updated_at"] = now().isoformat()
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.state_path)

    def _require_open(self) -> None:
        if self.state["status"] != "running":
            raise ValueError(f"Run is {self.state['status']}; no more actions allowed")
        if self.remaining_seconds <= 0:
            raise ValueError("Run deadline exceeded")

    def _task(self, task_id: str) -> dict:
        if task_id not in self.state["tasks"]:
            raise ValueError(f"Unknown task: {task_id}")
        return self.state["tasks"][task_id]

    @staticmethod
    def _latest(task: dict) -> dict:
        return task["attempts"][-1]

    def _active(self) -> list[dict]:
        return [
            task
            for task in self.state["tasks"].values()
            if task["attempts"] and self._latest(task)["worker"]["status"] in ACTIVE
        ]

    def system(self) -> dict:
        system = deepcopy(self.supervisor.get_system_info())
        system["workflow"] = {
            "start": "start_task with a concrete contract and tools from its profile",
            "wait": "wait_tasks on active tasks; inspect their results and artifacts",
            "review": "review_task; completed workers are not accepted tasks",
            "finish": "finish_run only after all required work is accepted",
        }
        available = {tool["name"] for tool in system["tools"]}
        allowed = set().union(*TOOL_PROFILES.values())
        system["tools"] = [tool for tool in system["tools"] if tool["name"] in allowed]
        system["task_tools"] = {
            kind: sorted(names & available) for kind, names in TOOL_PROFILES.items()
        }
        system["constraints"].update(self.state["limits"])
        system["constraints"]["remaining_seconds"] = self.remaining_seconds
        return system

    async def snapshot(self) -> dict:
        async with self._lock:
            return deepcopy(self.state)

    async def start_task(
        self,
        *,
        goal: str,
        scope: str,
        inputs: str,
        done_criteria: str,
        limits: str,
        kind: str,
        model: str,
        tools: list[str],
        timeout_seconds: int = 300,
        verification_for: str | None = None,
    ) -> dict:
        async with self._lock:
            self._require_open()
            if kind not in TOOL_PROFILES:
                raise ValueError("kind must be research, change or verification")
            if not all(value.strip() for value in (goal, scope, done_criteria)):
                raise ValueError("goal, scope and done_criteria must not be empty")
            if timeout_seconds < 1:
                raise ValueError("timeout_seconds must be positive")
            self.supervisor.catalog.require_model(model)
            selected = self.supervisor.catalog.require_tools(tools)
            if set(selected) - TOOL_PROFILES[kind]:
                raise ValueError(f"Tools outside the {kind} profile")
            target_attempt = None
            if kind == "verification":
                target = self._task(verification_for or "")
                candidate = self._latest(target)
                if (
                    target["kind"] != "change"
                    or candidate["worker"]["status"] != "completed"
                ):
                    raise ValueError("Verification requires a completed change task")
                if candidate["review"] is not None:
                    raise ValueError("Verify a change before accepting or rejecting it")
                target_attempt = candidate["number"]
            elif verification_for is not None:
                raise ValueError("Only verification tasks may specify verification_for")
            task = {
                "task_id": f"task-{uuid.uuid4().hex[:8]}",
                "goal": goal,
                "scope": scope,
                "inputs": inputs,
                "done_criteria": done_criteria,
                "limits": limits,
                "kind": kind,
                "model": model,
                "tools": selected,
                "timeout_seconds": timeout_seconds,
                "verification_for": verification_for,
                "target_attempt": target_attempt,
                "attempts": [],
            }
            await self._launch(task, feedback="")
            self.state["tasks"][task["task_id"]] = task
            self._save()
            return deepcopy(task)

    async def _launch(self, task: dict, *, feedback: str) -> None:
        if self.state["launches"] >= self.state["limits"]["max_launches"]:
            raise ValueError("Run launch budget exhausted")
        active = self._active()
        if active and (
            task["kind"] != "research" or any(t["kind"] != "research" for t in active)
        ):
            raise ValueError("Workspace busy: only research tasks may overlap")
        if task["kind"] == "change":
            pending = [
                t
                for t in self.state["tasks"].values()
                if t["kind"] == "change"
                and t["task_id"] != task["task_id"]
                and (self._latest(t)["review"] or {}).get("decision") != "accepted"
            ]
            if pending:
                raise ValueError("Resolve the existing change before starting another")
        timeout = min(
            task["timeout_seconds"],
            self.supervisor.constraints["max_timeout_seconds"],
            int(self.remaining_seconds),
        )
        if timeout < 1:
            raise ValueError("Insufficient time remaining to launch a worker")
        contract = {
            key: task[key]
            for key in (
                "task_id",
                "goal",
                "scope",
                "inputs",
                "done_criteria",
                "limits",
                "kind",
            )
        }
        contract["workdir"] = self.state["workdir"]
        if task["verification_for"]:
            target = self._task(task["verification_for"])
            contract["candidate"] = deepcopy(target)
        if feedback:
            contract["correction"] = {
                "feedback": feedback,
                "previous_attempt": deepcopy(self._latest(task)),
            }
        worker = await self.supervisor.start_agent(
            model=task["model"],
            task=json.dumps(contract, ensure_ascii=False),
            tools=task["tools"],
            timeout_seconds=timeout,
        )
        if task["kind"] == "change":
            self.state["workspace_revision"] += 1
        attempt = {
            "number": len(task["attempts"]) + 1,
            "workspace_revision": self.state["workspace_revision"],
            "feedback": feedback,
            "worker": worker,
            "review": None,
        }
        task["attempts"].append(attempt)
        self.state["launches"] += 1
        watcher = asyncio.create_task(self._watch(attempt))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def _watch(self, attempt: dict) -> None:
        agent_id = attempt["worker"]["agent_id"]
        while True:
            result = await self.supervisor.wait_agents([agent_id], timeout_seconds=60)
            worker = result["agents"][0]
            async with self._lock:
                self._update_worker(attempt, worker)
                self._save()
            if worker["status"] not in ACTIVE:
                return

    async def wait_tasks(
        self, task_ids: list[str], timeout_seconds: float = 60
    ) -> dict:
        async with self._lock:
            self._require_open()
            if not task_ids:
                raise ValueError("Supply at least one task ID")
            attempts = [
                self._latest(self._task(task_id)) for task_id in dict.fromkeys(task_ids)
            ]
            # Ignore already completed inputs when waiting for the next event.
            active = [a for a in attempts if a["worker"]["status"] in ACTIVE]
            ids = [a["worker"]["agent_id"] for a in active]
        if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 60:
            raise ValueError("Wait timeout must be between 0 and 60 seconds")
        if ids:
            result = await self.supervisor.wait_agents(
                ids, timeout_seconds=min(timeout_seconds, self.remaining_seconds)
            )
            async with self._lock:
                by_id = {worker["agent_id"]: worker for worker in result["agents"]}
                for attempt in active:
                    self._update_worker(attempt, by_id[attempt["worker"]["agent_id"]])
                self._save()
        return await self.snapshot()

    @staticmethod
    def _update_worker(attempt: dict, worker: dict) -> None:
        # An older wait snapshot must not resurrect a worker another waiter or
        # interrupt call has already observed in a terminal state.
        if attempt["worker"]["status"] in ACTIVE or worker["status"] not in ACTIVE:
            attempt["worker"] = worker

    async def review_task(
        self,
        task_id: str,
        *,
        decision: str,
        evidence: str,
        verification_task_id: str | None = None,
    ) -> dict:
        async with self._lock:
            self._require_open()
            task = self._task(task_id)
            attempt = self._latest(task)
            if (
                decision not in {"accepted", "rejected", "discarded"}
                or not evidence.strip()
            ):
                raise ValueError("Supply a decision and concrete review evidence")
            if attempt["worker"]["status"] in ACTIVE:
                raise ValueError("Wait for the worker before reviewing it")
            if attempt["review"] is not None:
                raise ValueError("Review is immutable; retry rejected work instead")
            if decision == "discarded" and task["kind"] == "change":
                raise ValueError(
                    "Changes cannot be discarded; correct/revert and verify them"
                )
            if decision == "accepted":
                if (
                    attempt["worker"]["status"] != "completed"
                    or not (attempt["worker"].get("result") or "").strip()
                ):
                    raise ValueError(
                        "Only completed workers with a result can be accepted"
                    )
                if task["kind"] == "change":
                    if not verification_task_id:
                        raise ValueError(
                            "Change requires an accepted verification of this attempt"
                        )
                    verification = self._task(verification_task_id or "")
                    check = self._latest(verification)
                    if (
                        verification["kind"] != "verification"
                        or verification["verification_for"] != task_id
                        or verification["target_attempt"] != attempt["number"]
                        or check["workspace_revision"]
                        != self.state["workspace_revision"]
                        or (check["review"] or {}).get("decision") != "accepted"
                    ):
                        raise ValueError(
                            "Change requires an accepted verification of this attempt"
                        )
                elif task["kind"] == "verification":
                    target = self._latest(self._task(task["verification_for"]))
                    if (
                        target["number"] != task["target_attempt"]
                        or attempt["workspace_revision"]
                        != self.state["workspace_revision"]
                    ):
                        raise ValueError(
                            "Verification is stale; verify the current attempt"
                        )
            attempt["review"] = {
                "decision": decision,
                "evidence": evidence,
                "verification_task_id": verification_task_id,
                "reviewed_at": now().isoformat(),
            }
            self._save()
            return deepcopy(task)

    async def retry_task(self, task_id: str, feedback: str) -> dict:
        async with self._lock:
            self._require_open()
            task = self._task(task_id)
            if not feedback.strip():
                raise ValueError("Correction requires specific feedback")
            if (self._latest(task)["review"] or {}).get("decision") != "rejected":
                raise ValueError("Only rejected tasks can be retried")
            if len(task["attempts"]) >= self.state["limits"]["max_attempts"]:
                raise ValueError("Task attempt budget exhausted")
            if task["kind"] == "verification":
                target = self._latest(self._task(task["verification_for"]))
                if (
                    target["number"] != task["target_attempt"]
                    or target["review"] is not None
                ):
                    raise ValueError(
                        "Create a new verification for the new change attempt"
                    )
            await self._launch(task, feedback=feedback)
            self._save()
            return deepcopy(task)

    async def interrupt_task(self, task_id: str, reason: str) -> dict:
        async with self._lock:
            self._require_open()
            if not reason.strip():
                raise ValueError("Supply an interruption reason")
            attempt = self._latest(self._task(task_id))
            attempt["worker"] = await self.supervisor.interrupt_agent(
                attempt["worker"]["agent_id"], reason=reason
            )
            self._save()
            return deepcopy(self._task(task_id))

    async def finish_run(self, summary: str) -> dict:
        async with self._lock:
            self._require_open()
            if not summary.strip():
                raise ValueError("Supply a final summary")
            if self._active():
                raise ValueError("Workers are still active")
            for task in self.state["tasks"].values():
                decision = (self._latest(task)["review"] or {}).get("decision")
                allowed = {"accepted", "discarded"}
                if task["kind"] == "verification":
                    allowed.add("rejected")  # A failed check remains useful history.
                if decision not in allowed:
                    raise ValueError(f"Unresolved task: {task['task_id']}")
            if self.state["tasks"] and not any(
                t["kind"] != "verification"
                and (self._latest(t)["review"] or {}).get("decision") == "accepted"
                for t in self.state["tasks"].values()
            ):
                raise ValueError("No required work was accepted")
            self.state.update(status="completed", summary=summary)
            self._save()
            return deepcopy(self.state)

    async def close(
        self, status: str = "interrupted", reason: str = "Run stopped"
    ) -> dict:
        """Cancel unfinished workers and save terminal state before factory cleanup."""
        if status not in {"interrupted", "timed_out", "failed", "blocked"}:
            raise ValueError("Invalid stop status")
        async with self._lock:
            if self.state["status"] == "running":
                self.state.update(status=status, summary=reason)
                self._save()
            active = list(self._active())
        for task in active:
            attempt = self._latest(task)
            worker = await self.supervisor.interrupt_agent(
                attempt["worker"]["agent_id"], reason=reason
            )
            async with self._lock:
                attempt["worker"] = worker
                self._save()
        if self._watchers:
            await asyncio.gather(*list(self._watchers))
        return await self.snapshot()
