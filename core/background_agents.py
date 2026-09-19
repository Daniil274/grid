"""Background lifecycle manager for Codex-launched Grid agents."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Literal, Optional

from core.agent_catalog import AgentCatalog


class AgentStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = {
    AgentStatus.COMPLETED,
    AgentStatus.FAILED,
    AgentStatus.TIMED_OUT,
    AgentStatus.INTERRUPTED,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BackgroundAgentRun:
    agent_id: str
    model: str
    task_text: str
    tools: List[str]
    timeout_seconds: int
    created_at: datetime
    deadline_at: datetime
    status: AgentStatus = AgentStatus.QUEUED
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    final_output: Optional[str] = None
    error: Optional[str] = None
    interrupt_reason: Optional[str] = None
    handle: Optional[asyncio.Task[Any]] = field(default=None, repr=False)
    finished: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class BackgroundAgentSupervisor:
    """Start, observe, wait for, and interrupt independent Grid agents."""

    def __init__(
        self,
        *,
        factory: Any,
        catalog: AgentCatalog,
        max_concurrency: int = 4,
        default_timeout_seconds: int = 300,
        max_timeout_seconds: int = 900,
        result_max_chars: int = 20_000,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if default_timeout_seconds < 1 or max_timeout_seconds < default_timeout_seconds:
            raise ValueError("Invalid timeout limits")
        self.factory = factory
        self.catalog = catalog
        self.max_concurrency = max_concurrency
        self.default_timeout_seconds = default_timeout_seconds
        self.max_timeout_seconds = max_timeout_seconds
        self.result_max_chars = result_max_chars
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._runs: Dict[str, BackgroundAgentRun] = {}
        self._lock = asyncio.Lock()

    @property
    def constraints(self) -> Dict[str, Any]:
        return {
            "max_concurrency": self.max_concurrency,
            "default_timeout_seconds": self.default_timeout_seconds,
            "max_timeout_seconds": self.max_timeout_seconds,
            "result_max_chars": self.result_max_chars,
            "terminal_statuses": sorted(status.value for status in TERMINAL_STATUSES),
        }

    def get_system_info(self) -> Dict[str, Any]:
        return self.catalog.system_info(constraints=self.constraints)

    async def start_agent(
        self,
        *,
        model: str,
        task: str,
        tools: Iterable[str],
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        model = str(model).strip()
        task = str(task).strip()
        if not task:
            raise ValueError("task must be a non-empty string")
        self.catalog.require_model(model)
        selected_tools = self.catalog.require_tools(tools)

        effective_timeout = (
            self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        effective_timeout = int(effective_timeout)
        if effective_timeout < 1 or effective_timeout > self.max_timeout_seconds:
            raise ValueError(
                f"timeout_seconds must be between 1 and {self.max_timeout_seconds}"
            )

        created_at = _utc_now()
        run = BackgroundAgentRun(
            agent_id=f"agent-{uuid.uuid4().hex[:10]}",
            model=model,
            task_text=task,
            tools=selected_tools,
            timeout_seconds=effective_timeout,
            created_at=created_at,
            deadline_at=created_at + timedelta(seconds=effective_timeout),
        )
        async with self._lock:
            self._runs[run.agent_id] = run
            run.handle = asyncio.create_task(
                self._execute(run),
                name=f"grid-background-{run.agent_id}",
            )
        return self._serialize(run, include_result=False)

    async def _execute(self, run: BackgroundAgentRun) -> None:
        try:
            # The deadline starts at launch, not when a queued run acquires a
            # worker slot. This makes timeout_seconds a real wall-clock timer.
            await asyncio.wait_for(
                self._run_agent(run),
                timeout=run.timeout_seconds,
            )
        except asyncio.TimeoutError:
            run.status = AgentStatus.TIMED_OUT
            run.error = f"Agent exceeded its {run.timeout_seconds}s deadline."
        except asyncio.CancelledError:
            run.status = AgentStatus.INTERRUPTED
            run.error = run.interrupt_reason or "Agent was interrupted."
        except Exception as exc:
            run.status = AgentStatus.FAILED
            run.error = str(exc)
        finally:
            run.finished_at = _utc_now()
            run.finished.set()

    async def _run_agent(self, run: BackgroundAgentRun) -> None:
        async with self._semaphore:
            run.status = AgentStatus.RUNNING
            run.started_at = _utc_now()
            worker = await self.factory.create_dynamic_agent(
                name=run.agent_id,
                instructions=(
                    "You are an execution subagent launched by Codex through Grid. "
                    "Complete the assigned task using only the provided tools. "
                    "Do not delegate to other agents. Report the concrete result, "
                    "changed files, checks performed, and any unresolved blockers."
                ),
                model_key=run.model,
                tool_names=list(run.tools),
            )
            resolved_tool_names = {
                str(getattr(tool, "name", "") or getattr(tool, "__name__", ""))
                for tool in (getattr(worker, "tools", None) or [])
            }
            missing = [name for name in run.tools if name not in resolved_tool_names]
            if missing:
                raise RuntimeError(
                    "AgentFactory did not resolve requested tools: "
                    + ", ".join(missing)
                )

            result = await self.factory.run_agent_object_simple(
                worker,
                run.task_text,
                context_id=f"ctx-{uuid.uuid4().hex[:12]}",
            )
            run.final_output = self._truncate(str(result or ""))
            run.status = AgentStatus.COMPLETED

    def _truncate(self, text: str) -> str:
        if len(text) <= self.result_max_chars:
            return text
        omitted = len(text) - self.result_max_chars
        return (
            text[: self.result_max_chars] + f"\n\n... [truncated {omitted} characters]"
        )

    async def get_agents(
        self, agent_ids: Optional[Iterable[str]] = None
    ) -> Dict[str, Any]:
        runs = await self._select_runs(agent_ids)
        return {"agents": [self._serialize(run, include_result=True) for run in runs]}

    async def wait_agents(
        self,
        agent_ids: Iterable[str],
        *,
        timeout_seconds: float = 60.0,
        return_when: Literal["first", "all"] = "first",
    ) -> Dict[str, Any]:
        if timeout_seconds < 0 or timeout_seconds > 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if return_when not in {"first", "all"}:
            raise ValueError("return_when must be 'first' or 'all'")

        runs = await self._select_runs(agent_ids, require_ids=True)
        if not self._wait_condition_met(runs, return_when):
            waiters = [asyncio.create_task(run.finished.wait()) for run in runs]
            try:
                mode = (
                    asyncio.FIRST_COMPLETED
                    if return_when == "first"
                    else asyncio.ALL_COMPLETED
                )
                done, pending = await asyncio.wait(
                    waiters,
                    timeout=timeout_seconds,
                    return_when=mode,
                )
                reason = "agent_event" if done else "wait_timeout"
                for waiter in pending:
                    waiter.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            except asyncio.CancelledError:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
                raise
        else:
            reason = "already_terminal"

        return {
            "reason": reason,
            "agents": [self._serialize(run, include_result=True) for run in runs],
        }

    async def interrupt_agent(
        self, agent_id: str, *, reason: str = ""
    ) -> Dict[str, Any]:
        run = await self._get_run(agent_id)
        if run.status in TERMINAL_STATUSES:
            return self._serialize(run, include_result=True)
        run.interrupt_reason = reason.strip() or "Interrupted by Codex."
        if run.handle is not None:
            run.handle.cancel()
            await asyncio.gather(run.handle, return_exceptions=True)
        # A task cancelled before its coroutine gets its first scheduling turn
        # never executes _execute's finally block, so finalize it here as well.
        self._finalize_cancelled_run(run)
        return self._serialize(run, include_result=True)

    async def shutdown(self) -> None:
        async with self._lock:
            active = [
                run
                for run in self._runs.values()
                if run.status not in TERMINAL_STATUSES and run.handle is not None
            ]
        for run in active:
            run.interrupt_reason = "Grid MCP server is shutting down."
            run.handle.cancel()
        if active:
            await asyncio.gather(
                *(run.handle for run in active if run.handle), return_exceptions=True
            )
            for run in active:
                self._finalize_cancelled_run(run)
        cleanup = getattr(self.factory, "cleanup", None)
        if cleanup is not None:
            result = cleanup()
            if hasattr(result, "__await__"):
                await result

    async def _select_runs(
        self,
        agent_ids: Optional[Iterable[str]],
        *,
        require_ids: bool = False,
    ) -> List[BackgroundAgentRun]:
        ids = list(
            dict.fromkeys(
                str(agent_id).strip()
                for agent_id in (agent_ids or [])
                if str(agent_id).strip()
            )
        )
        if require_ids and not ids:
            raise ValueError("agent_ids must contain at least one agent ID")
        async with self._lock:
            if not ids:
                return sorted(self._runs.values(), key=lambda run: run.created_at)
            missing = [agent_id for agent_id in ids if agent_id not in self._runs]
            if missing:
                raise ValueError("Unknown agent IDs: " + ", ".join(missing))
            return [self._runs[agent_id] for agent_id in ids]

    async def _get_run(self, agent_id: str) -> BackgroundAgentRun:
        runs = await self._select_runs([agent_id], require_ids=True)
        return runs[0]

    @staticmethod
    def _wait_condition_met(runs: List[BackgroundAgentRun], return_when: str) -> bool:
        terminal = [run.status in TERMINAL_STATUSES for run in runs]
        return any(terminal) if return_when == "first" else all(terminal)

    @staticmethod
    def _finalize_cancelled_run(run: BackgroundAgentRun) -> None:
        if run.finished.is_set():
            return
        run.status = AgentStatus.INTERRUPTED
        run.error = run.interrupt_reason or "Agent was interrupted."
        run.finished_at = _utc_now()
        run.finished.set()

    @staticmethod
    def _serialize(run: BackgroundAgentRun, *, include_result: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "agent_id": run.agent_id,
            "status": run.status.value,
            "model": run.model,
            "tools": list(run.tools),
            "timeout_seconds": run.timeout_seconds,
            "created_at": run.created_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "deadline_at": run.deadline_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "error": run.error,
        }
        if include_result and run.status in TERMINAL_STATUSES:
            payload["result"] = run.final_output
        return payload
