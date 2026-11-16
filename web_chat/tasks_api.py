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

With chat spaces (web_chat.space) a project keeps a tracker of its own, in its
space, beside the user's: the chats of a project plan there. bd names a
tracker's tasks after its directory, so a task's id says whose it is
(``prj-1a2b3c4d-x7q``): the routes on one task find its tracker by it, a new
task goes to the project the request names, and the list without a project is
every tracker's tasks together, each with its ``project_id``.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import tracker_lifecycle, tracker_service
from tools.beads_tools import BEAD_ID as _ID, _bd_problem, _init_args, run_bd
from web_chat.plan_board import latest_plan

logger = logging.getLogger("grid.web_chat.tasks_api")

#: A task of a project's own tracker: bd prefixes its ids with the tracker
#: directory's name (tools.beads_tools._init_args), the project's id.
PROJECT_TASK = re.compile(r"^(prj-[0-9a-f]{8})-")

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
    #: The project whose tracker the task goes to; none: the user's.
    project_id: Optional[str] = Field(default=None, max_length=64)
    #: The client's key for this action: the same key with the same request
    #: answers the task the first attempt made instead of creating a second.
    request_id: Optional[str] = Field(default=None, max_length=120)


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
    #: The client's key for this change; a repeat answers the first result.
    request_id: Optional[str] = Field(default=None, max_length=120)


class TaskCloseRequest(BaseModel):
    reason: str = Field(default="Completed", min_length=1, max_length=2000)
    request_id: Optional[str] = Field(default=None, max_length=120)


class TaskDepRequest(BaseModel):
    action: Literal["add", "remove"]
    parent_id: str = Field(min_length=1, max_length=64)
    #: The client's key for this change; a repeat answers the first result.
    request_id: Optional[str] = Field(default=None, max_length=120)


def _issue_id(bead_id: str) -> str:
    if not _ID.match(bead_id):
        raise HTTPException(status_code=400, detail=f"Not a task id: {bead_id}")
    return bead_id


def _reason(res: dict[str, Any]) -> str:
    """What bd said when it refused: its error, else its output, clipped.

    An answer with no text at all is not a refusal but a lost answer: bd was
    stopped on a timeout or died, so the write may already be in the tracker.
    Saying "the tracker refused" there would be a guess about a database
    nobody read.
    """
    text = (res.get("error") or res.get("output") or "").strip()
    if not text:
        text = (
            "bd did not answer: the change may or may not have been applied"
            if not res.get("success")
            else "bd did not answer"
        )
    return text[:500]


def _unconfirmed(res: dict[str, Any]) -> bool:
    """Whether a failed write may still have reached the tracker."""
    return not res.get("success") and not (res.get("error") or res.get("output") or "").strip()


def _refused_write(res: dict[str, Any]) -> JSONResponse:
    """The page's answer for a write bd did not confirm.

    A refusal with bd's own words is a ``400`` - nothing was written and the
    reader can fix the request. An answer that never came is ``202`` with
    ``committed: null``: the page must not tell the user nothing happened when
    nobody can know, and must not offer a blind retry for it.
    """
    reason = _reason(res)
    unconfirmed = _unconfirmed(res)
    payload = {
        "ok": False,
        "isError": True,
        "code": res.get("code") or ("tracker_unknown" if unconfirmed else "tracker_rejected"),
        "committed": None if unconfirmed else False,
        "reason": reason,
    }
    if unconfirmed:
        return JSONResponse(payload, status_code=202)
    return JSONResponse(payload, status_code=400)


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


def _graph(issues: List[dict[str, Any]]) -> dict[str, List[str]]:
    """What each issue waits for, by id: the edges a cycle would run along."""
    return {
        issue["id"]: _dep_ids(issue)
        for issue in issues
        if isinstance(issue.get("id"), str)
    }


def _dependency_ring(graph: dict[str, List[str]], child: str, parent: str) -> Optional[List[str]]:
    """The chain from *parent* down to *child*, when *parent* already waits for it.

    Adding *child* - depends on - *parent* would then close a ring; the returned
    path names the tasks already between them (``[parent, ..., child]``). None
    when no such chain exists and the edge is safe.
    """
    if child == parent:
        return [child]
    seen = {parent}
    previous: dict[str, str] = {}
    queue = [parent]
    while queue:
        at = queue.pop(0)
        for dep in graph.get(at) or []:
            if dep in seen:
                continue
            seen.add(dep)
            previous[dep] = at
            if dep == child:
                path = [child]
                while path[-1] in previous:
                    path.append(previous[path[-1]])
                path.reverse()
                return path
            queue.append(dep)
    return None


def _ring_reason(names: dict[str, str], child: str, parent: str, ring: List[str]) -> str:
    """Why the edge is refused, naming the ring in the tasks' own titles."""
    label = lambda task_id: names.get(task_id) or task_id
    chain = " → ".join(label(task_id) for task_id in [child, *ring])
    return f"Cannot add this dependency: it would close a cycle ({chain}). Remove one of the links first."


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
        # What the run that took the task reported, when the source carried it
        # (a bd issue does not; a plan snapshot does).
        "report": issue.get("report") if isinstance(issue.get("report"), dict) else None,
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


def _project_known(space: Any, project_id: str) -> bool:
    """Whether the space holds a project by this id (a stub without the route: no)."""
    exists = getattr(space, "project_exists", None)
    if not callable(exists):
        return False
    try:
        return bool(exists(project_id))
    except Exception:  # noqa: BLE001 - an unreadable project list knows no project
        return False


def _project_directory(space: Any, project_id: str) -> Path:
    """The tracker of a project the request *names*; 404 when there is no such project.

    A project the space does not hold is refused before anything is written.
    The user's own tracker is not a fallback here: a task meant for a project
    would otherwise land in the global tracker, and nothing would say so.
    """
    trackers = _project_trackers(space)
    if project_id in trackers:
        return trackers[project_id]
    if not _project_known(space, project_id):
        raise HTTPException(status_code=404, detail=f"Unknown project: {project_id}")
    of_project = getattr(space, "tracker_dir", None)
    return Path(of_project(project_id)) if of_project is not None else Path(space.workspace_path)


def _tracker(space: Any, project: Optional[str] = None, task_id: Optional[str] = None) -> Path:
    """The directory of the tracker meant: the project's, named or told by the
    task's id; else the user's.

    A named project must exist (:func:`_project_directory`). An id naming a
    project this space does not hold is read in the user's tracker, where a
    task with such an id lives when no chat space made it.
    """
    if project is not None:
        return _project_directory(space, project)
    if task_id:
        match = PROJECT_TASK.match(task_id)
        if match and match.group(1) in _project_trackers(space):
            return _project_trackers(space)[match.group(1)]
    return Path(space.workspace_path)


async def _bd_for(
    space: Any, args: List[str], *, project: Optional[str] = None, task_id: Optional[str] = None
) -> dict[str, Any]:
    """Run one bd command in the tracker meant (:func:`_tracker`); 503 when bd cannot run."""
    problem = _bd_problem()
    if problem:
        raise HTTPException(status_code=503, detail=f"The tracker is unavailable: {problem}")
    return await run_bd(args, cwd=str(_tracker(space, project, task_id)))


def _project_trackers(space: Any) -> Dict[str, Path]:
    """The projects that keep a tracker of their own, by id: those with one started."""
    of_projects = getattr(space, "project_trackers", None)
    return dict(of_projects()) if of_projects is not None else {}


def _chat_sources(space: Any) -> Dict[str, List[str]]:
    """The chats whose plans named each task id, by id.

    Best-effort and read-only: the global tracker has no project to group by,
    so a task's source chats come from scanning the conversations' newest
    plans (the same slice a plan card reads). A space without the routes -
    a stub in a test, an older server - simply answers none.
    """
    out: Dict[str, List[str]] = {}
    manager = getattr(space, "context_manager", None)
    if not callable(manager):
        return out
    try:
        views = manager().conversation_views()
    except Exception:  # noqa: BLE001 - sources are a convenience, never a failure
        return out
    for view in views or []:
        context_id = view.get("id") if isinstance(view, dict) else None
        if not context_id:
            continue
        try:
            snapshot = manager().get_context_messages(context_id, limit=40)
        except Exception:  # noqa: BLE001
            continue
        plan = latest_plan(list((snapshot or {}).get("messages") or []))
        for task in (plan or {}).get("tasks") or []:
            task_id = task.get("id") if isinstance(task, dict) else None
            if not isinstance(task_id, str):
                continue
            ids = out.setdefault(task_id, [])
            if context_id not in ids:
                ids.append(context_id)
    return out


# -- writes: the tracker tools' own service, for the page ---------------------


class _HostDirectory:
    """The tracker a page request writes in, as a tool run's factory is.

    ``tools.beads_tools`` resolves its directory and its own workspace through
    the run's factory; a page request has no agent run, so this stands in for
    one: the tracker of the user (or project) the request names, bd on the host
    (the page works in the user's own space, not inside an agent's container),
    and the signed-in user as the actor every write is journaled under.
    """

    def __init__(self, directory: Path, user: Optional[str]) -> None:
        self.tracker_directory = str(directory)
        self.container_id = None
        self.extra_roots: Dict[str, str] = {}
        self.user_id = user
        self.config = SimpleNamespace(get_working_directory=lambda: str(directory))


def _action_key(header: Optional[str], body_request_id: Optional[str]) -> str:
    """The one key of a page action: the client's if it sent one, else this request's.

    A page that repeats an action whose answer was lost sends the same key and
    reads the first answer instead of writing twice; a key the client cannot
    send leaves the request to mint its own, which protects nothing but is
    still the journal record the answer is reported from.
    """
    for candidate in (body_request_id, header):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "ui-" + uuid.uuid4().hex


def _host_write(space: Any, directory: Path, action: str, request_id: str) -> Any:
    """The write context of one page action: its user, its own run, its tracker."""
    user = getattr(space, "user_id", None)
    raw = SimpleNamespace(
        user_id=user,
        # A page action is its own short run: it must refuse a task a live run
        # holds, and it must not take one from a run whose lease is over.
        run_id=f"ui-{action}-{request_id}",
        agent_id="tasks-page",
        context_id=None,
        parent_run_id=None,
        container_id=None,
        factory=_HostDirectory(directory, user),
        task_bindings=[],
    )
    return SimpleNamespace(context=raw)


async def _release_bindings(context: Any) -> None:
    """Hand back the task bindings a page action made.

    A page claim is an action, not a run: the journal refuses it a task a live
    run holds, but it must not keep a lease of its own once the answer is
    sent - the next run would have to wait for it to expire.
    """
    raw = getattr(context, "context", None)
    run_id = getattr(raw, "run_id", None)
    for entry in list(getattr(raw, "task_bindings", None) or []):
        try:
            await asyncio.to_thread(
                tracker_lifecycle.release,
                entry["directory"],
                entry["bead_id"],
                run_id,
                reason="a page action is not a run",
            )
        except Exception:  # noqa: BLE001 - the tracker's answer is what the page reads
            logger.warning(
                "the page's claim binding of %s could not be released",
                entry.get("bead_id"),
                exc_info=True,
            )


def _refusal(receipt: Any) -> JSONResponse:
    """The page's answer for a write the service refused or could not confirm.

    A refusal the journal or bd recorded is a ``400`` with what was refused;
    one whose answer never came is ``202`` with ``committed: null`` - the page
    must not say nothing happened when nobody can know.
    """
    payload = getattr(receipt, "payload", None) or {}
    committed = getattr(receipt, "committed", None)
    reason = str(payload.get("error") or payload.get("reason") or "").strip()[:500]
    body: Dict[str, Any] = {
        "ok": False,
        "isError": True,
        "code": payload.get("code") or ("tracker_unknown" if committed is None else "tracker_rejected"),
        "committed": committed,
        "reason": reason or "the tracker refused",
    }
    if payload.get("operation_id"):
        body["operation_id"] = payload["operation_id"]
    return JSONResponse(body, status_code=202 if committed is None else 400)


async def _shown(space: Any, bead_id: str) -> Optional[dict[str, Any]]:
    """The task as the tracker holds it now; None when it cannot be read."""
    res = await _bd_for(space, ["show", bead_id, "--json"], task_id=bead_id)
    if not res.get("success"):
        return None
    for issue in _issues(res):
        if isinstance(issue.get("id"), str):
            return issue
    return None


def _write_page(issue: Optional[dict[str, Any]], project: Optional[str] = None) -> JSONResponse:
    """The page's task for a write that the tracker confirmed."""
    if issue is None:
        # The journal holds the commit; the tracker could not be read back.
        return JSONResponse({"ok": True, "isError": False, "committed": True, "reason": "the tracker did not answer a read-back"}, status_code=202)
    task = _task(issue)
    if project:
        task["project_id"] = project
    return JSONResponse(task)


def register_task_routes(api: APIRouter, current_space: Any) -> None:
    """The tracker page's routes, each acting in the asking user's workspace."""

    @api.get("/api/tasks")
    async def list_tasks(
        status: Optional[Literal["open", "in_progress", "blocked", "deferred", "closed"]] = None,
        project: Optional[str] = None,
        space: Any = Depends(current_space),
    ) -> JSONResponse:
        args = ["list", "--limit", "0", "--all", "--json", "--sort", "updated", "--reverse"]
        if status:
            args += ["--status", status]
        # One tracker when a project is named; else the user's and every project's.
        sources: Dict[Optional[str], Any] = {project: None} if project else {None: None, **{
            project_id: None for project_id in _project_trackers(space)}}
        answers = await asyncio.gather(*(_bd_for(space, args, project=source) for source in sources))
        res = answers[0]
        if not any(answer.get("success") for answer in answers):
            # No tracker in this workspace yet, or bd refused to read it: the
            # page still opens, with the reason and a way to start one.
            return JSONResponse({"available": False, "reason": _reason(res), "tasks": [], "total": 0})
        tasks = []
        for source, answer in zip(sources, answers):
            if not answer.get("success"):
                continue
            issues = _issues(answer)
            statuses = {issue.get("id"): issue.get("status") for issue in issues}
            for issue in issues:
                task = _task(issue, statuses)
                if source is not None:
                    task["project_id"] = source
                tasks.append(task)
        # The chats that planned a task, so a bd-only board still points back.
        sources = _chat_sources(space)
        for task in tasks:
            chat_ids = sources.get(task["id"])
            if chat_ids:
                task["chat_ids"] = chat_ids
        return JSONResponse({"available": True, "reason": None, "tasks": tasks, "total": len(tasks), "counts": _counts(tasks)})

    @api.get("/api/tasks/{bead_id}")
    async def show_task(bead_id: str, space: Any = Depends(current_space)) -> JSONResponse:
        res = await _bd_for(space, ["show", _issue_id(bead_id)], task_id=bead_id)
        if not res.get("success"):
            raise HTTPException(status_code=404 if "not found" in _reason(res).lower() else 400, detail=_reason(res))
        task = _task(_one(res))
        task["chat_ids"] = _chat_sources(space).get(task["id"], [])
        match = PROJECT_TASK.match(task["id"])
        if match:
            task["project_id"] = match.group(1)
        return JSONResponse(task)

    @api.post("/api/tasks")
    async def create_task(
        body: TaskCreateRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        space: Any = Depends(current_space),
    ) -> JSONResponse:
        """Create a task through the tracker service: its journal, its marker, one answer.

        The request id (the body's ``request_id``, else the ``Idempotency-Key``
        header) is the client's to keep: repeating the same action after a lost
        answer answers the first bead instead of creating a second one. A
        project the request names must be the space's - a task meant for one
        never lands in the user's own tracker.
        """
        problem = _bd_problem()
        if problem:
            raise HTTPException(status_code=503, detail=f"The tracker is unavailable: {problem}")
        project = (body.project_id or "").strip() or None
        directory = _project_directory(space, project) if project else Path(space.workspace_path)
        if not (directory / ".beads").is_dir():
            # A project's (or the user's) first task starts its tracker: the
            # user asked for it, and the write below needs somewhere to land.
            started = await run_bd(_init_args(str(directory)), cwd=str(directory))
            if not started.get("success") and "already" not in (
                started.get("error") or started.get("output") or ""
            ).lower():
                return _refused_write(started)
        request_id = _action_key(idempotency_key, body.request_id)
        context = _host_write(space, directory, "create", request_id)
        try:
            receipt = await tracker_service.create_bead(
                context=context,
                title=body.title,
                priority=body.priority,
                type=body.type,
                description=body.description,
                acceptance=body.acceptance,
                assignee=body.assignee or "",
                depends_on=body.depends_on,
                request_id=request_id,
            )
        finally:
            await _release_bindings(context)
        if receipt.is_error or receipt.committed is not True:
            return _refusal(receipt)
        created = receipt.data.get("id")
        issue = await _shown(space, created) if isinstance(created, str) else None
        answer = _write_page(issue, project)
        answer.status_code = 201
        return answer

    @api.patch("/api/tasks/{bead_id}")
    async def update_task(
        bead_id: str,
        body: TaskUpdateRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        space: Any = Depends(current_space),
    ) -> JSONResponse:
        """Change one task through the tracker service, and answer with what it holds."""
        bead_id = _issue_id(bead_id)
        problem = _bd_problem()
        if problem:
            raise HTTPException(status_code=503, detail=f"The tracker is unavailable: {problem}")
        changes = {
            "status": body.status,
            "title": body.title,
            "description": body.description,
            "acceptance": body.acceptance,
            "notes": body.notes,
            "priority": body.priority,
            "type": body.type,
            "assignee": body.assignee,
        }
        if all(value is None for value in changes.values()):
            raise HTTPException(status_code=400, detail="Nothing to update.")
        request_id = _action_key(idempotency_key, body.request_id)
        context = _host_write(space, _tracker(space, task_id=bead_id), "update", request_id)
        try:
            receipt = await tracker_service.update_bead(
                context=context, bead_id=bead_id, request_id=request_id, **changes
            )
        finally:
            await _release_bindings(context)
        if receipt.is_error or receipt.committed is not True:
            return _refusal(receipt)
        return _write_page(await _shown(space, bead_id))

    @api.post("/api/tasks/{bead_id}/close")
    async def close_task(
        bead_id: str,
        body: TaskCloseRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        space: Any = Depends(current_space),
    ) -> JSONResponse:
        """Close a task: the receipt names the reason only once the tracker holds it."""
        bead_id = _issue_id(bead_id)
        problem = _bd_problem()
        if problem:
            raise HTTPException(status_code=503, detail=f"The tracker is unavailable: {problem}")
        request_id = _action_key(idempotency_key, body.request_id)
        context = _host_write(space, _tracker(space, task_id=bead_id), "close", request_id)
        try:
            receipt = await tracker_service.close_bead(
                context=context, bead_id=bead_id, reason=body.reason, request_id=request_id
            )
        finally:
            await _release_bindings(context)
        if receipt.is_error or receipt.committed is not True:
            return _refusal(receipt)
        issue = await _shown(space, bead_id)
        if issue is None or issue.get("status") != "closed":
            # The journal holds a commit but the tracker does not read back
            # closed: that is not a confirmation, so no receipt is issued.
            return JSONResponse(
                {
                    "ok": False,
                    "isError": True,
                    "code": "tracker_unconfirmed",
                    "committed": None,
                    "reason": "the tracker accepted the close, but the task does not read back closed.",
                },
                status_code=202,
            )
        return JSONResponse(
            {"ok": True, "isError": False, "committed": True, "id": bead_id, "close_reason": body.reason}
        )

    @api.post("/api/tasks/{bead_id}/claim")
    async def claim_task(
        bead_id: str,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        space: Any = Depends(current_space),
    ) -> JSONResponse:
        """Claim a task and answer with a typed receipt of what the tracker holds.

        This is the lifecycle integration point: a caller that must *show*
        ownership (the chat's plan card, a runtime handing its own executed
        claim over) reads the receipt instead of inferring it from prose. The
        claim goes through the tracker service, so it is journaled like the
        agent tools' claims and refused on a task a live run holds; the receipt
        is issued only after the task reads back in progress, and the page's
        own binding is released at once - a page claim is an action, not a run
        that keeps a lease.
        """
        bead_id = _issue_id(bead_id)
        problem = _bd_problem()
        if problem:
            raise HTTPException(status_code=503, detail=f"The tracker is unavailable: {problem}")
        request_id = _action_key(idempotency_key, None)
        context = _host_write(space, _tracker(space, task_id=bead_id), "claim", request_id)
        try:
            receipt = await tracker_service.update_bead(
                context=context, bead_id=bead_id, claim=True, request_id=request_id
            )
        finally:
            await _release_bindings(context)
        if receipt.is_error or receipt.committed is not True:
            return _refusal(receipt)
        issue = await _shown(space, bead_id)
        status = (issue or {}).get("status")
        if status != "in_progress":
            # bd accepted the write but the tracker does not show it: not a
            # confirmation, so no receipt is issued.
            return JSONResponse(
                {
                    "ok": False,
                    "isError": True,
                    "code": "tracker_unconfirmed",
                    "committed": None,
                    "reason": "bd answered, but the task does not read back in progress.",
                },
                status_code=202,
            )
        typed = {
            "tool": "beads_update",
            "isError": False,
            "bead_id": bead_id,
            "status": status,
            "assignee": issue.get("assignee"),
            "committed": True,
        }
        return JSONResponse({"ok": True, "isError": False, "committed": True, "receipt": typed})

    @api.post("/api/tasks/{bead_id}/deps")
    async def task_dependency(
        bead_id: str,
        body: TaskDepRequest,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        space: Any = Depends(current_space),
    ) -> JSONResponse:
        """Add or remove one dependency edge, through the journaled tracker service."""
        # bd dep add/remove <blocked> <blocker>: this task waits for the parent.
        child = _issue_id(bead_id)
        parent = _issue_id(body.parent_id)
        problem = _bd_problem()
        if problem:
            raise HTTPException(status_code=503, detail=f"The tracker is unavailable: {problem}")
        directory = _tracker(space, task_id=child)
        if body.action == "add":
            # bd does not refuse a ring itself, so the graph is checked here
            # first: a task that already waits, through the chain, for *child*
            # cannot also become its blocker. The reason names the would-be ring.
            if child == parent:
                raise HTTPException(status_code=400, detail="A task cannot depend on itself.")
            if directory != _tracker(space, task_id=parent):
                raise HTTPException(status_code=400, detail="A task can wait only for a task of its own tracker.")
            listing = await _bd_for(space, ["list", "--limit", "0", "--all", "--json"], task_id=child)
            data = listing.get("data") if listing.get("success") else None
            issues = [issue for issue in data if isinstance(issue, dict)] if isinstance(data, list) else []
            ring = _dependency_ring(_graph(issues), child, parent)
            if ring:
                names = {issue.get("id"): issue.get("title") for issue in issues}
                raise HTTPException(status_code=400, detail=_ring_reason(names, child, parent, ring))
        request_id = _action_key(idempotency_key, body.request_id)
        context = _host_write(space, directory, "dep", request_id)
        try:
            receipt = await tracker_service.dep(
                context=context,
                action=body.action,
                child_id=child,
                parent_id=parent,
                request_id=request_id,
            )
        finally:
            await _release_bindings(context)
        if receipt.is_error:
            return _refusal(receipt)
        # A dependency is a state, not a transcript: the answer is the tracker
        # holding the edge (or not), which is also what proves a lost answer.
        issue = await _shown(space, child)
        held = parent in (_dep_ids(issue) if issue else [])
        if held != (body.action == "add"):
            return JSONResponse(
                {
                    "ok": False,
                    "isError": True,
                    "code": "tracker_unconfirmed",
                    "committed": None,
                    "reason": "the tracker does not read back the edge this request asked for.",
                },
                status_code=202,
            )
        return JSONResponse(
            {"ok": True, "isError": False, "committed": True, "id": child, "action": body.action, "parent_id": parent}
        )

    @api.post("/api/tasks/init")
    async def init_tracker(project: Optional[str] = None, space: Any = Depends(current_space)) -> JSONResponse:
        """Start a tracker in the user's workspace - or the project's space -
        the page's own choice, never an implicit side effect of reading it."""
        res = await _bd_for(space, _init_args(str(_tracker(space, project))), project=project)
        if not res.get("success"):
            text = (res.get("error") or res.get("output") or "").lower()
            if "already" not in text:
                return _refused_write(res)
        return JSONResponse({"ok": True})
