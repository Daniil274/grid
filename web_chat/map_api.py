"""The visual GRID's first slice: one project as a map.

The page (``web_chat/grid.html``) draws a project as a place: its parent path
above, its sub-projects around it, the tasks its tracker holds, the chats that
work there, and the results its space keeps. This module gathers exactly that
one answer, so the picture costs one round trip.

    GET /api/projects/{project_id}/map    the project, its place and its work

Every route acts for the identified user in that user's own space
(``web_chat.space``), so a project of another user's space is not found at all
(``404``). What the map reads stays cheap and bounded:

* tasks come from the project's own tracker, through the same bd the tracker
  page uses (``web_chat.tasks_api``); a project whose tracker bd cannot read
  answers ``available: false`` with the reason, never a server error;
* processes are the chats of the project, with the run state the rail shows;
* results are the top level of the project's space, without the tool state
  that lives there (``.beads``, ``.git``, ``.codegraph``, ``.grid``, every
  other hidden entry).

No message text, no task notes or descriptions, and no absolute filesystem
path leaves this route: the map shows geography, not the work itself. bd and
the folder listing run off the event loop, each under its own deadline.

The answer is stable JSON with an ETag over its bytes, so a browser that
already has this map revalidates it with ``If-None-Match`` and gets ``304``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response

from web_chat.explorer_api import list_folder
from web_chat.tasks_api import _bd_for, _counts, _issues, _reason, _task
from web_chat.views import conversation_title

#: How many tasks, chats and result entries one map shows at most.
TASKS_LIMIT = 300
PROCESSES_LIMIT = 50
RESULTS_LIMIT = 100

#: How long bd and the folder listing may take before the map says so.
BD_TIMEOUT_SECONDS = 20.0
FOLDER_TIMEOUT_SECONDS = 10.0

#: The task fields the map shows: what places a task, never what it says.
TASK_FIELDS = (
    "id",
    "title",
    "status",
    "state",
    "priority",
    "type",
    "assignee",
    "created_at",
    "updated_at",
    "depends_on",
    "close_reason",
)


def _canonical(payload: dict[str, Any]) -> str:
    """The one byte order of a map: what its ETag is taken over."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _etag_of(body: str) -> str:
    return '"' + hashlib.sha256(body.encode("utf-8")).hexdigest() + '"'


def _matches(header: Optional[str], etag: str) -> bool:
    """Whether an ``If-None-Match`` header names *etag* (weak tags and ``*`` too)."""
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == etag or candidate == "*":
            return True
    return False


def _project_payload(project: Any) -> dict[str, Any]:
    return {
        "id": project.id,
        "title": project.title,
        "parent_id": project.parent_id,
        "description": project.description,
        "updated_at": project.updated_at,
    }


def _ancestors(store: Any, project: Any) -> List[dict[str, str]]:
    """The project's parents, root first: the path the map draws above it."""
    path: List[dict[str, str]] = []
    seen = {project.id}
    parent_id = project.parent_id
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = store.get(parent_id)
        if parent is None:
            break
        path.append({"id": parent.id, "title": parent.title})
        parent_id = parent.parent_id
    path.reverse()
    return path


def _children(store: Any, project_id: str) -> List[dict[str, str]]:
    """The project's direct sub-projects, archived ones left out."""
    found = [
        project
        for project in store.list()
        if project.parent_id == project_id and not project.archived
    ]
    found.sort(key=lambda project: (project.title or "").casefold())
    return [{"id": project.id, "title": project.title} for project in found]


async def _tasks(space: Any, project_id: str) -> dict[str, Any]:
    """The project's tracker as the map's task items, bounded and sorted by age."""
    try:
        answer = await asyncio.wait_for(
            _bd_for(
                space,
                ["list", "--limit", "0", "--all", "--json", "--sort", "created"],
                project=project_id,
            ),
            BD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"available": False, "reason": "The tracker did not answer in time.", "items": [], "total": 0,
                "truncated": False, "counts": _counts([])}
    except HTTPException as exc:
        return {"available": False, "reason": str(exc.detail), "items": [], "total": 0,
                "truncated": False, "counts": _counts([])}
    if not answer.get("success"):
        return {"available": False, "reason": _reason(answer), "items": [], "total": 0,
                "truncated": False, "counts": _counts([])}
    try:
        issues = _issues(answer)
    except HTTPException as exc:
        return {"available": False, "reason": str(exc.detail), "items": [], "total": 0,
                "truncated": False, "counts": _counts([])}
    statuses = {issue.get("id"): issue.get("status") for issue in issues}
    mapped = [_task(issue, statuses) for issue in issues]
    # Oldest first: a task keeps its place on the map as newer ones arrive.
    mapped.sort(key=lambda task: (task.get("created_at") or "", task.get("id") or ""))
    items = [{name: task.get(name) for name in TASK_FIELDS} for task in mapped]
    return {
        "available": True,
        "reason": None,
        "items": items[:TASKS_LIMIT],
        "total": len(items),
        "truncated": len(items) > TASKS_LIMIT,
        "counts": _counts(mapped),
    }


def _processes(space: Any, project_id: str) -> dict[str, Any]:
    """The chats of the project: which work now, which wait for the user."""
    manager = getattr(space, "context_manager", None)
    if manager is None:
        return {"items": [], "total": 0, "truncated": False}
    try:
        views = manager().conversation_views()
    except Exception:  # noqa: BLE001 - a map without chats is still a map
        views = []
    awaiting: Dict[str, list] = {}
    of_approvals = getattr(space, "awaiting_approval", None)
    if callable(of_approvals):
        try:
            awaiting = of_approvals() or {}
        except Exception:  # noqa: BLE001
            awaiting = {}
    turns = getattr(space, "turns", None)
    items: List[dict[str, Any]] = []
    for view in views or []:
        if not isinstance(view, dict):
            continue
        metadata = view.get("metadata") or {}
        if (metadata.get("project_id") or None) != project_id:
            continue
        chat_id = view.get("id")
        if not chat_id:
            continue
        try:
            title = conversation_title(view)
        except Exception:  # noqa: BLE001 - the title is never worth a failed map
            title = metadata.get("title") or chat_id
        running = False
        if turns is not None:
            try:
                running = bool(turns.is_running(chat_id))
            except Exception:  # noqa: BLE001
                running = False
        items.append(
            {
                "chat_id": chat_id,
                "title": title,
                "updated_at": view.get("updated_at") or metadata.get("updated_at"),
                "active": running,
                "awaiting_approval": bool(awaiting.get(chat_id)),
            }
        )
    # Newest first, then the running chats above the idle ones.
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    items.sort(key=lambda item: not item["active"])
    return {"items": items[:PROCESSES_LIMIT], "total": len(items), "truncated": len(items) > PROCESSES_LIMIT}


def _project_space(space: Any, project_id: str) -> Optional[Path]:
    """The directory of the project's space, when this server keeps one."""
    if not getattr(space, "chat_spaces", False):
        return None
    layout = getattr(space, "layout", None)
    project_spaces = getattr(layout, "project_spaces", None) if layout is not None else None
    if project_spaces is None:
        return None
    return Path(project_spaces) / project_id


def _modified(mtime: Any) -> Optional[str]:
    if not isinstance(mtime, (int, float)):
        return None
    try:
        moment = datetime.fromtimestamp(float(mtime), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _unavailable_results(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "items": [], "total": 0, "truncated": False}


async def _results(space: Any, project_id: str) -> dict[str, Any]:
    """The top level of the project's space: what the work there left behind."""
    directory = _project_space(space, project_id)
    if directory is None:
        return _unavailable_results("This project has no space of its own on this server.")
    try:
        listing = await asyncio.wait_for(
            asyncio.to_thread(list_folder, directory, ""), FOLDER_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return _unavailable_results("The project's files did not answer in time.")
    except FileNotFoundError:
        return _unavailable_results("This project's space is empty so far.")
    except Exception:  # noqa: BLE001 - a missing folder, a link refused: no results, not a 500
        return _unavailable_results("This project's space cannot be read.")
    entries = [
        entry
        for entry in listing.get("entries") or []
        # Tools keep their state in hidden folders; they are not results.
        if isinstance(entry, dict) and not str(entry.get("name") or "").startswith(".")
    ]
    items = [
        {
            "name": entry.get("name"),
            # Relative to the project's space: never an absolute path.
            "path": entry.get("path") or entry.get("name"),
            "kind": "dir" if entry.get("kind") == "dir" else "file",
            "size": entry.get("size") if entry.get("kind") == "file" else None,
            "modified": _modified(entry.get("mtime")),
        }
        for entry in entries
    ]
    return {
        "available": True,
        "reason": None,
        "items": items[:RESULTS_LIMIT],
        "total": len(items),
        "truncated": len(items) > RESULTS_LIMIT,
    }


def _files_url(space: Any, project_id: str, chats: List[str]) -> Optional[str]:
    """Where the files page opens this project's space, when it can.

    The files page opens a chat's space by ``context`` and reaches the other
    spaces of that chat by a path's first name (``project``), so the project's
    space is reachable through one of its chats - and only when this server
    keeps a space per project.
    """
    if not chats or _project_space(space, project_id) is None:
        return None
    return f"/files?context={quote(chats[0], safe='')}&path=project"


def register_map_routes(api: APIRouter, current_space: Any) -> None:
    """Mount the map route on *api*; it resolves the space itself."""

    @api.get("/api/projects/{project_id}/map")
    async def project_map(
        project_id: str,
        space: Any = Depends(current_space),
        if_none_match: Optional[str] = Header(default=None, alias="If-None-Match"),
    ) -> Response:
        """One project as the visual GRID draws it; 404 for a project not of this space."""
        if not space.project_exists(project_id):
            raise HTTPException(status_code=404, detail="Project not found")
        store = space.project_store
        project = store.get(project_id)
        if project is None:  # deleted between the two reads
            raise HTTPException(status_code=404, detail="Project not found")
        tasks, results = await asyncio.gather(_tasks(space, project_id), _results(space, project_id))
        processes = _processes(space, project_id)
        payload = {
            "project": _project_payload(project),
            "path": _ancestors(store, project),
            "children": _children(store, project_id),
            "tasks": tasks,
            "processes": processes,
            "results": results,
            "files_url": _files_url(space, project_id, [item["chat_id"] for item in processes["items"]]),
        }
        body = _canonical(payload)
        etag = _etag_of(body)
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if _matches(if_none_match, etag):
            return Response(status_code=304, headers=headers)
        return Response(content=body, media_type="application/json", headers=headers)
