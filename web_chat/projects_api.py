"""The projects API: the hierarchy, and which chat belongs to which project.

The store is :mod:`web_chat.projects`; the chats live in the space's context
manager, whose ``project_id`` metadata this module is the only writer of. The
two are joined here so a project deletion re-parents both the sub-projects (the
store's job) and the chats (this module's), and so the list can report how many
chats each project holds.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.context import is_tool_result
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from web_chat.plan_board import STATES, latest_plan
from web_chat.projects import Project, ProjectError
from web_chat.schemas import (
    ContinueRequest,
    ConversationProjectRequest,
    ProjectCreateRequest,
    ProjectUpdateRequest,
)
from web_chat.tasks_api import _bd_for, _counts, _issues, _task

#: The metadata type of a message that carries context into a fresh chat.
SEED_TYPE = "context_seed"

#: How much of a chat's tail a ``tail`` continuation carries, and how long.
TAIL_MESSAGES = 8
SEED_LIMIT = 20000
ANSWER_TAIL_LIMIT = 2000


def register_project_routes(api: APIRouter, current_space: Any) -> None:
    """Mount the project routes on *api*; every one resolves the space itself."""

    @api.get("/api/projects")
    async def list_projects(space: Any = Depends(current_space)) -> JSONResponse:
        counts = {project_id: len(ids) for project_id, ids in space.conversations_by_project().items()}
        return JSONResponse(
            {"projects": [_project(project, counts.get(project.id, 0)) for project in space.project_store.list()]}
        )

    @api.post("/api/projects")
    async def create_project(body: ProjectCreateRequest, space: Any = Depends(current_space)) -> JSONResponse:
        try:
            project = space.project_store.create(
                body.title, parent_id=body.parent_id, description=body.description or ""
            )
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return JSONResponse(_project(project, 0), status_code=201)

    @api.patch("/api/projects/{project_id}")
    async def update_project(
        project_id: str, body: ProjectUpdateRequest, space: Any = Depends(current_space)
    ) -> JSONResponse:
        changes = body.model_dump(exclude_unset=True)
        try:
            project = space.project_store.update(project_id, **changes)
        except KeyError:
            raise HTTPException(status_code=404, detail="Project not found") from None
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        counts = {pid: len(ids) for pid, ids in space.conversations_by_project().items()}
        return JSONResponse(_project(project, counts.get(project.id, 0)))

    @api.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, space: Any = Depends(current_space)) -> JSONResponse:
        """Remove a project, moving its sub-projects and chats up to its parent."""
        try:
            parent_id, moved_projects = space.project_store.delete(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Project not found") from None
        moved_chats = _reparent_chats(space, project_id, parent_id)
        return JSONResponse({"ok": True, "moved_chats": moved_chats, "moved_projects": len(moved_projects)})

    @api.put("/api/chat/conversations/{context_id}/project")
    async def set_conversation_project(
        context_id: str, body: ConversationProjectRequest, space: Any = Depends(current_space)
    ) -> JSONResponse:
        """Bind a chat to a project, or detach it when ``project_id`` is null."""
        if body.project_id is not None and not space.project_exists(body.project_id):
            raise HTTPException(status_code=404, detail="Project not found") from None
        # None must reach the metadata (detaching); update_conversation_metadata drops it.
        if not space.context_manager().update_context_metadata(
            context_id, {"project_id": body.project_id}, create=False
        ):
            raise HTTPException(status_code=404, detail="Conversation not found") from None
        return JSONResponse({"id": context_id, "project_id": body.project_id})

    @api.post("/api/chat/conversations/{context_id}/continue")
    async def continue_conversation(
        context_id: str, body: ContinueRequest, space: Any = Depends(current_space)
    ) -> JSONResponse:
        """A new chat seeded from this one; it inherits the source's project."""
        manager = space.context_manager()
        view = manager.conversation_view(context_id)
        if view is None:
            raise HTTPException(status_code=404, detail="Conversation not found") from None
        metadata = view["metadata"]
        project_id = metadata.get("project_id") or None
        if project_id is not None and not space.project_exists(project_id):
            project_id = None  # a project deleted under the chat: continue without it
        title = metadata.get("title") or context_id
        seed = _seed_text(
            body.strategy, title, view["messages"], latest_plan(_plan_messages(manager, context_id))
        )
        new_id = manager.start_new_context()
        changes = {"created_by_web": True, "continued_from": context_id}
        if project_id is not None:
            changes["project_id"] = project_id
        manager.update_context_metadata(new_id, changes)
        manager.append_message_to(
            new_id,
            "user",
            seed,
            {"type": SEED_TYPE, "source_id": context_id, "source_title": title},
        )
        return JSONResponse(
            {"id": new_id, "project_id": project_id, "continued_from": context_id, "strategy": body.strategy},
            status_code=201,
        )

    @api.get("/api/projects/{project_id}/plan")
    async def project_plan(project_id: str, space: Any = Depends(current_space)) -> JSONResponse:
        """Every task the project's chats planned, with the tracker's statuses."""
        if not space.project_exists(project_id):
            raise HTTPException(status_code=404, detail="Project not found") from None
        manager = space.context_manager()
        chats: Dict[str, str] = {}
        merged: Dict[str, Dict[str, Any]] = {}
        for context_id in space.conversations_by_project().get(project_id, []):
            view = manager.conversation_view(context_id)
            if view is None:
                continue
            chats[context_id] = view["metadata"].get("title") or context_id
            for task in (latest_plan(_plan_messages(manager, context_id)) or {}).get("tasks") or []:
                task_id = task.get("id")
                if not isinstance(task_id, str):
                    continue
                entry = merged.get(task_id)
                if entry is None:
                    merged[task_id] = {**task, "chat_ids": [context_id], "source": "trace"}
                elif context_id not in entry["chat_ids"]:
                    entry["chat_ids"].append(context_id)
        tasks = list(merged.values())
        statuses = {task["id"]: task.get("status") for task in tasks}
        for task in tasks:
            # A snapshot from before plans carried states: derive it as bd's tasks do.
            task.setdefault("state", _trace_state(task, statuses))
        # The tracker is the source of truth about a task's status; a task no
        # conversation's plan named never enters the project's board.
        known = await _bd_tasks(space)
        for task in tasks:
            if task["id"] in known:
                task.update(known[task["id"]])
                task["source"] = "bd"
        return JSONResponse({"tasks": tasks, "counts": _counts(tasks), "chats": chats, "total": len(tasks)})

    @api.get("/api/chat/conversations/{context_id}/plan")
    async def conversation_plan(context_id: str, space: Any = Depends(current_space)) -> JSONResponse:
        """The newest plan snapshot this chat's turns left, or an empty one."""
        manager = space.context_manager()
        if manager.conversation_view(context_id) is None:
            raise HTTPException(status_code=404, detail="Conversation not found") from None
        plan = latest_plan(_plan_messages(manager, context_id))
        return JSONResponse(plan or {"tasks": [], "counts": {state: 0 for state in STATES}, "total": 0})


def _trace_state(task: Dict[str, Any], statuses: Dict[str, Any]) -> str:
    """The board state of a planned task from its status and what it waits for."""
    status = task.get("status") or "open"
    if status == "open":
        waits = [dep for dep in task.get("depends_on") or [] if statuses.get(dep, "open") != "closed"]
        return "waiting" if waits else "ready"
    return "waiting" if status == "deferred" else status


def seed_project_context(space: Any, context_id: str, project_id: str) -> bool:
    """Seed a fresh chat with its project's shared notes; False when there are none."""
    project = space.project_store.get(project_id)
    if project is None or not project.notes:
        return False
    return space.context_manager().append_message_to(
        context_id,
        "user",
        f"Project context: {project.title}\n\n{project.notes}",
        {"type": SEED_TYPE, "source_id": f"project:{project.id}", "source_title": project.title},
    )


def _plan_messages(manager: Any, context_id: str) -> List[Dict[str, Any]]:
    """The same recent slice a turn reads its plan from (plan_board.latest_plan)."""
    try:
        snapshot = manager.get_context_messages(context_id, limit=40)
    except Exception:  # noqa: BLE001 - a plan is best-effort, never a hard failure
        return []
    return list((snapshot or {}).get("messages") or [])


async def _bd_tasks(space: Any) -> Dict[str, Dict[str, Any]]:
    """The tracker's tasks by id; an empty map when bd or the tracker is missing."""
    try:
        result = await _bd_for(space, ["list", "--limit", "0", "--all", "--json", "--sort", "updated", "--reverse"])
    except HTTPException:
        return {}
    if not result.get("success"):
        return {}
    try:
        issues = _issues(result)
    except HTTPException:
        return {}
    statuses = {issue.get("id"): issue.get("status") for issue in issues}
    return {issue["id"]: _task(issue, statuses) for issue in issues if isinstance(issue.get("id"), str)}


def _message_text(message: Any) -> str:
    """A conversation message's text, whether it holds a string or content parts."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    parts: List[str] = []
    for part in content if isinstance(content, list) else []:
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _seed_text(strategy: str, title: str, messages: List[Any], plan: Optional[Dict[str, Any]]) -> str:
    """The seed message a continued chat starts from: the plan, or the tail."""
    lines = [f'Continuing from "{title}".']
    if strategy == "tail":
        chosen = [
            f"{message.role}: {_message_text(message)}"
            for message in messages
            if not is_tool_result(message)
        ][-TAIL_MESSAGES:]
        return "\n".join([*lines, "", *chosen])[:SEED_LIMIT]
    tasks = (plan or {}).get("tasks") or []
    if tasks:
        lines += ["", "Plan so far:"]
        for task in tasks:
            deps = task.get("depends_on") or []
            waits = f" (depends on {', '.join(deps)})" if deps else ""
            lines.append(f"- [{task.get('status') or 'open'}] {task.get('title') or task.get('id')}{waits}")
    answer = next(
        (_message_text(message) for message in reversed(messages) if message.role == "assistant"),
        "",
    )
    if answer:
        lines += ["", "Where the last answer left off:", answer[-ANSWER_TAIL_LIMIT:]]
    return "\n".join(lines)[:SEED_LIMIT]


def _project(project: Project, chat_count: int) -> Dict[str, Any]:
    data = project.to_dict()
    data["chat_count"] = chat_count
    return data


def _reparent_chats(space: Any, project_id: str, parent_id: Optional[str]) -> int:
    """Move every chat of a deleted project to *parent_id*; return how many moved."""
    manager = space.context_manager()
    moved = 0
    for context_id in space.conversations_by_project().get(project_id, []):
        if manager.update_context_metadata(context_id, {"project_id": parent_id}, create=False):
            moved += 1
    return moved
