"""The plan of a conversation, as one live step of the turn's trace.

A coordinator creates its plan in the task tracker (``beads_plan``) and its
agents take, update and close the tasks. The board follows those calls as the
trace sees them - arguments and results, never a query of its own - and keeps
a single ``plan`` step up to date: every task with its state and what it waits
for. The step is part of the trace, so it streams to every viewer, replays to
a page that attaches late and is stored with the answer.

A turn starts from the plan the conversation's last plan step left, so work on
tasks planned earlier shows on the board of the turn that does it. The board
appears in a turn once one of its tasks changes or a plan is created.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from web_chat.trace import Step, StepKind, TraceRecorder

#: Task states the board shows. ``ready`` and ``waiting`` are open tasks with
#: and without unfinished dependencies.
CLOSED = "closed"
IN_PROGRESS = "in_progress"
STATES = ("ready", "waiting", IN_PROGRESS, "blocked", CLOSED)

#: Tools whose calls change the plan; ``orchestrate`` claims a task through
#: the init_tools it hands its agent.
TRACKED_TOOLS = frozenset({
    "beads_plan", "beads_create", "beads_update", "beads_close", "beads_dep", "orchestrate",
})


def _data(value: Any) -> Any:
    """A JSON tool argument or result as data; None when it is not JSON."""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


#: What ``bd create`` prints without ``--json`` - the text beads_create returns:
#: "✓ Created issue: grid-a1b — Fix the build".
_CREATED = re.compile(r"Created issue:\s+(?P<id>[\w.-]+)(?:\s+[—–-]\s+(?P<title>[^\n]+))?")


def _created(output: Any) -> Optional[dict[str, Any]]:
    """The id and title of the task a beads_create call made; None when it says none."""
    result = _data(output)
    if isinstance(result, dict):
        return result if isinstance(result.get("id"), str) else None
    match = _CREATED.search(output) if isinstance(output, str) else None
    return {"id": match["id"], "title": (match["title"] or "").strip()} if match else None


def _failed(output: Any) -> bool:
    """Whether a tracker tool answered with an error instead of a result."""
    if isinstance(output, str) and output.lstrip().startswith("❌"):
        return True
    data = _data(output)
    return isinstance(data, dict) and bool(data.get("error"))


class PlanBoard:
    """The tasks a conversation planned, kept as the turn's ``plan`` step."""

    def __init__(self, recorder: TraceRecorder, tasks: Iterable[dict[str, Any]] = ()) -> None:
        self._recorder = recorder
        self._step: Optional[Step] = None
        # By task id, in the order they were planned.
        self._tasks: dict[str, dict[str, Any]] = {}
        for task in tasks:
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                self._tasks[task["id"]] = {
                    "id": task["id"],
                    "key": task.get("key") or "",
                    "title": task.get("title") or task["id"],
                    "status": task.get("status") if task.get("status") in (IN_PROGRESS, "blocked", CLOSED) else "open",
                    "depends_on": [dep for dep in task.get("depends_on") or [] if isinstance(dep, str)],
                }

    @classmethod
    def continuing(cls, recorder: TraceRecorder, stored_messages: Iterable[dict[str, Any]]) -> "PlanBoard":
        """A board that starts from the newest plan step among *stored_messages*."""
        for message in reversed(list(stored_messages)):
            trace = (message.get("metadata") or {}).get("trace") or []
            plans = [step for step in trace if isinstance(step, dict) and step.get("kind") == StepKind.PLAN.value]
            if plans:
                return cls(recorder, (plans[-1].get("plan") or {}).get("tasks") or [])
        return cls(recorder)

    # -- what the trace saw ----------------------------------------------------
    def called(self, tool: str, arguments: Any) -> None:
        """A call started: an agent launched on a task claims it."""
        if tool != "orchestrate":
            return
        args = _data(arguments) or {}
        init_tools = _data(args.get("init_tools")) if isinstance(args, dict) else None
        changed = False
        for call in init_tools if isinstance(init_tools, list) else []:
            params = call.get("parameters") if isinstance(call, dict) else None
            if not isinstance(params, dict) or call.get("name") != "beads_update":
                continue
            if params.get("claim") or params.get("status") == IN_PROGRESS:
                changed |= self._set(params.get("bead_id"), IN_PROGRESS)
        if changed:
            self._publish()

    def finished(self, tool: str, arguments: Any, output: Any) -> None:
        """A tracker call answered: the board takes in what it changed."""
        if tool not in TRACKED_TOOLS or tool == "orchestrate" or _failed(output):
            return
        args = _data(arguments)
        args = args if isinstance(args, dict) else {}
        changed = False
        if tool == "beads_plan":
            changed = self._planned(args, _data(output))
        elif tool == "beads_create":
            result = _created(output)
            if result is not None:
                self._tasks[result["id"]] = {
                    "id": result["id"], "key": "", "title": result.get("title") or args.get("title") or result["id"],
                    "status": "open", "depends_on": [],
                }
                changed = True
        elif tool == "beads_update":
            status = IN_PROGRESS if args.get("claim") else args.get("status")
            changed = self._set(args.get("bead_id"), status)
        elif tool == "beads_close":
            changed = self._set(args.get("bead_id"), CLOSED)
        elif tool == "beads_dep":
            task = self._tasks.get(args.get("child_id"))
            parent = args.get("parent_id")
            if task is not None and isinstance(parent, str):
                if args.get("action") == "add" and parent not in task["depends_on"]:
                    task["depends_on"].append(parent)
                    changed = True
                elif args.get("action") == "remove" and parent in task["depends_on"]:
                    task["depends_on"].remove(parent)
                    changed = True
        if changed:
            self._publish()

    def _planned(self, args: dict[str, Any], result: Any) -> bool:
        ids = result.get("ids") if isinstance(result, dict) else None
        if not isinstance(ids, dict):
            return False
        for task in args.get("tasks") or []:
            if not isinstance(task, dict) or task.get("key") not in ids:
                continue
            key = task["key"]
            self._tasks[ids[key]] = {
                "id": ids[key],
                "key": key,
                "title": task.get("title") or key,
                "status": "open",
                # A key of this plan becomes its task's id; an id stays as it is.
                "depends_on": [ids.get(dep, dep) for dep in task.get("depends_on") or []],
            }
        return True

    def _set(self, bead_id: Any, status: Any) -> bool:
        task = self._tasks.get(bead_id) if isinstance(bead_id, str) else None
        if task is None or not isinstance(status, str) or task["status"] == status:
            return False
        task["status"] = status if status in (IN_PROGRESS, "blocked", CLOSED) else "open"
        return True

    # -- the step --------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Every task with the state it shows, and the counts."""
        titles = {task_id: task["key"] or task["title"] for task_id, task in self._tasks.items()}
        tasks = []
        for task in self._tasks.values():
            waits_for = [dep for dep in task["depends_on"] if self._tasks.get(dep, {}).get("status") != CLOSED]
            state = task["status"]
            if state == "open":
                state = "waiting" if waits_for else "ready"
            tasks.append({
                **{name: task[name] for name in ("id", "key", "title", "status", "depends_on")},
                "state": state,
                "waits_for": [titles.get(dep, dep) for dep in waits_for],
            })
        counts = {state: sum(task["state"] == state for task in tasks) for state in STATES}
        return {"tasks": tasks, "counts": counts, "total": len(tasks)}

    def _publish(self) -> None:
        plan = self.snapshot()
        done, total = plan["counts"][CLOSED], plan["total"]
        fields = {
            "title": f"Plan · {done}/{total} done",
            "subtitle": f"{plan['counts'][IN_PROGRESS]} in progress · {plan['counts']['ready']} ready",
            "tone": "positive" if total and done == total else "neutral",
            "plan": plan,
        }
        if self._step is None:
            self._step = self._recorder.note(StepKind.PLAN, **fields)
        else:
            self._recorder.update(self._step, **fields)
