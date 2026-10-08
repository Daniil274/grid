"""The tracker page's API: the tasks of the user's workspace, live from bd.

The chat's plan card follows the tracker calls a conversation's turns made
(web_chat.plan_board); this page asks the tracker itself, through the same bd
the agent tools run (tools.beads_tools), so it also sees what no conversation
touched. Every route acts for the identified user in that user's own space
(web_chat.space): one user never sees or changes another's tasks. Writes go
through bd only - no tracker file is touched by hand - and a server whose bd
cannot run answers that the tracker is unavailable instead of pretending.

Reads answer ``200`` with ``available: false`` and the reason when bd or the
tracker is missing, so the page can degrade; writes are refused (``503`` for
a missing bd, ``400`` for bd's own refusal). The board's task states are the
plan card's (web_chat.plan_board.STATES): an open task is ``ready`` without
unfinished dependencies and ``waiting`` with them.
"""

from __future__ import annotations

import json
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from tools.beads_tools import BEAD_ID as _ID, _bd_problem, _init_args, bd_flag as _flag, run_bd

#: bd's statuses; ``closed`` is not one of them: a task closes with its reason
#: through its own route, never as a quiet status change.
_STATUSES = ("open", "in_progress", "blocked", "deferred")


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    priority: int = Field(default=2, ge=0, le=4)
    type: Literal["task", "bug", "feature", "epic", "chore", "decision", "spike", "story", "milestone"] = "task"
    description: str = Field(default="", max_length=20000)
    acceptance: str = Field(default="", max_length=20000)
    assignee: Optional[str] = Field(default=None, max_length=100)
    depends_on: List[str] = Field(default_factory=list, max_length=50)


class TaskUpdateRequest(BaseModel):
    status: Optional[Literal["open", "in_progress", "blocked", "deferred"]] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=20000)
    acceptance: Optional[str] = Field(default=None, max_length=20000)
    # bd replaces the notes with what an update brings, as the agent tool does.
    notes: Optional[str] = Field(default=None, max_length=20000)
    priority: Optional[int] = Field(default=None, ge=0, le=4)
    type: Optional[Literal["task", "bug", "feature", "epic", "chore", "decision", "spike", "story", "milestone"]] = None
    assignee: Optional[str] = Field(default=None, max_length=100)


class TaskCloseRequest(BaseModel):
    reason: str = Field(default="Completed", min_length=1, max_length=2000)


class TaskDepRequest(BaseModel):
    action: Literal["add", "remove"]
    parent_id: str = Field(min_length=1, max_length=64)


def _issue_id(bead_id: str) -> str:
    if not _ID.match(bead_id):
        raise HTTPException(status_code=400, detail=f"Not a task id: {bead_id}")
    return bead_id


def _reason(res: dict[str, Any]) -> str:
    """What bd said when it refused: its error, else its output, clipped."""
    text = (res.get("error") or res.get("output") or "bd did not answer").strip()
    return text[:500]


def _dep_ids(issue: dict[str, Any]) -> List[str]:
    """The ids an issue waits for, from list's or show's dependency rows."""
    ids: List[str] = []
    for dep in issue.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        if dep.get("type") not in (None, "blocks", "depends_on", "depends-on"):
            continue  # a relates_to link blocks nothing
        dep_id = dep.get("depends_on_id") or dep.get("id")
        if isinstance(dep_id, str) and dep_id not in ids:
            ids.append(dep_id)
    return ids


def _task(issue: dict[str, Any], statuses: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """One bd issue as the page's task: the plan card's fields and states."""
    statuses = statuses or {}
    depends_on = _dep_ids(issue)
    waits = [dep for dep in depends_on if statuses.get(dep) != "closed"]
    status = issue.get("status") if issue.get("status") in (*_STATUSES, "closed") else "open"
    if status == "open":
        state = "waiting" if waits else "ready"
    elif status == "deferred":
        state = "waiting"
    else:
        state = status
    titles = {
        dep.get("depends_on_id") or dep.get("id"): dep.get("title")
        for dep in issue.get("dependencies") or []
        if isinstance(dep, dict)
    }
    return {
        "id": issue.get("id") or "",
        "title": issue.get("title") or issue.get("id") or "",
        "description": issue.get("description") or "",
        "acceptance": issue.get("acceptance_criteria") or "",
        "notes": issue.get("notes") or "",
        "status": status,
        "state": state,
        "priority": issue.get("priority") if isinstance(issue.get("priority"), int) else None,
        "type": issue.get("issue_type") or None,
        "assignee": issue.get("assignee") or None,
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "close_reason": issue.get("close_reason") or None,
        "depends_on": depends_on,
        "waits_for": [titles.get(dep) or dep for dep in waits],
    }


def _counts(tasks: List[dict[str, Any]]) -> dict[str, int]:
    return {
        state: sum(task["state"] == state for task in tasks)
        for state in ("ready", "waiting", "in_progress", "blocked", "closed")
    }


def _issues(res: dict[str, Any]) -> List[dict[str, Any]]:
    data = res.get("data")
    if data is None and res.get("output"):
        try:
            data = json.loads(res["output"])
        except ValueError:
            data = None
    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail=_reason(res))
    return [issue for issue in data if isinstance(issue, dict)]


def _one(res: dict[str, Any]) -> dict[str, Any]:
    """The single issue a show/create answered with."""
    for issue in _issues(res):
        if isinstance(issue.get("id"), str):
            return issue
    raise HTTPException(status_code=400, detail=_reason(res))


async def _bd_for(space: Any, args: List[str]) -> dict[str, Any]:
    """Run one bd command in the user's workspace; None when bd cannot run."""
    problem = _bd_problem()
    if problem:
        raise HTTPException(status_code=503, detail=f"The tracker is unavailable: {problem}")
    return await run_bd(args, cwd=str(space.workspace_path))


def register_task_routes(api: APIRouter, current_space: Any) -> None:
    """The tracker page's routes, each acting in the asking user's workspace."""

    @api.get("/api/tasks")
    async def list_tasks(
        status: Optional[Literal["open", "in_progress", "blocked", "deferred", "closed"]] = None,
        space: Any = Depends(current_space),
    ) -> JSONResponse:
        args = ["list", "--limit", "0", "--all", "--json", "--sort", "updated", "--reverse"]
        if status:
            args += ["--status", status]
        res = await _bd_for(space, args)
        if not res.get("success"):
            # No tracker in this workspace yet, or bd refused to read it: the
            # page still opens, with the reason and a way to start one.
            return JSONResponse({"available": False, "reason": _reason(res), "tasks": [], "total": 0})
        issues = _issues(res)
        statuses = {issue.get("id"): issue.get("status") for issue in issues}
        tasks = [_task(issue, statuses) for issue in issues]
        return JSONResponse({"available": True, "reason": None, "tasks": tasks, "total": len(tasks), "counts": _counts(tasks)})

    @api.get("/api/tasks/{bead_id}")
    async def show_task(bead_id: str, space: Any = Depends(current_space)) -> JSONResponse:
        res = await _bd_for(space, ["show", _issue_id(bead_id)])
        if not res.get("success"):
            raise HTTPException(status_code=404 if "not found" in _reason(res).lower() else 400, detail=_reason(res))
        task = _task(_one(res))
        return JSONResponse(task)

    @api.post("/api/tasks")
    async def create_task(body: TaskCreateRequest, space: Any = Depends(current_space)) -> JSONResponse:
        args = ["create", "--json", _flag("priority", body.priority), _flag("type", body.type)]
        if body.description:
            args.append(_flag("description", body.description))
        if body.acceptance:
            args.append(_flag("acceptance", body.acceptance))
        if body.assignee:
            args.append(_flag("assignee", body.assignee))
        deps = [dep for dep in body.depends_on if _ID.match(dep)]
        if deps:
            args.append(_flag("deps", ",".join(deps)))
        # The title is the one positional argument: after "--", a title such as
        # "--db=..." is a title, never a flag.
        args += ["--", body.title]
        res = await _bd_for(space, args)
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=_reason(res))
        return JSONResponse(_task(_one(res)), status_code=201)

    @api.patch("/api/tasks/{bead_id}")
    async def update_task(
        bead_id: str, body: TaskUpdateRequest, space: Any = Depends(current_space)
    ) -> JSONResponse:
        bead_id = _issue_id(bead_id)
        args = ["update", bead_id]
        if body.status is not None:
            args.append(_flag("status", body.status))
        if body.title is not None:
            args.append(_flag("title", body.title))
        if body.description is not None:
            args.append(_flag("description", body.description))
        if body.acceptance is not None:
            args.append(_flag("acceptance", body.acceptance))
        if body.notes is not None:
            args.append(_flag("notes", body.notes))
        if body.priority is not None:
            args.append(_flag("priority", body.priority))
        if body.type is not None:
            args.append(_flag("type", body.type))
        if body.assignee is not None:
            args.append(_flag("assignee", body.assignee))
        if len(args) == 2:
            raise HTTPException(status_code=400, detail="Nothing to update.")
        res = await _bd_for(space, args)
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=_reason(res))
        shown = await _bd_for(space, ["show", bead_id])
        return JSONResponse(_task(_one(shown)) if shown.get("success") else {"ok": True})

    @api.post("/api/tasks/{bead_id}/close")
    async def close_task(
        bead_id: str, body: TaskCloseRequest, space: Any = Depends(current_space)
    ) -> JSONResponse:
        res = await _bd_for(space, ["close", _issue_id(bead_id), _flag("reason", body.reason)])
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=_reason(res))
        return JSONResponse({"ok": True, "id": bead_id, "close_reason": body.reason})

    @api.post("/api/tasks/{bead_id}/deps")
    async def task_dependency(
        bead_id: str, body: TaskDepRequest, space: Any = Depends(current_space)
    ) -> JSONResponse:
        # bd dep add/remove <blocked> <blocker>: this task waits for the parent.
        res = await _bd_for(
            space, ["dep", body.action, _issue_id(bead_id), _issue_id(body.parent_id)]
        )
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=_reason(res))
        return JSONResponse({"ok": True, "id": bead_id, "action": body.action, "parent_id": body.parent_id})

    @api.post("/api/tasks/init")
    async def init_tracker(space: Any = Depends(current_space)) -> JSONResponse:
        """Start a tracker in the user's workspace: the page's own choice,
        never an implicit side effect of reading it."""
        res = await _bd_for(space, _init_args(str(space.workspace_path)))
        if not res.get("success"):
            text = (res.get("error") or res.get("output") or "").lower()
            if "already" not in text:
                raise HTTPException(status_code=400, detail=_reason(res))
        return JSONResponse({"ok": True})
