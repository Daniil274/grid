"""The visual GRID's first slice: one project as a map.

The page (``web_chat/grid.html``) draws a project as a place: its parent path
above, its sub-projects around it, the tasks its tracker holds, the chats that
work there, and the files its space keeps. This module gathers exactly that
one answer, so the picture costs one round trip.

    GET /api/projects/{project_id}/map    the project, its place and its work

Every route acts for the identified user in that user's own space
(``web_chat.space``), so a project of another user's space is not found at all
(``404``). The route only reads - it creates no folder and no tracker - and
what it reads stays bounded:

* tasks come from the project's **own** tracker only: the ``.beads`` of the
  project's space. A project without one answers ``available: false``; the
  user's tracker is never opened in its place. Only a self-contained local
  database is admitted, explicitly pinned with --db in a clean environment.
  Every returned id must additionally carry the project's prefix;
* processes are the chats of the project, with the run state the rail shows;
* files are the top level of the project's space, without the tool state
  that lives there (``.beads``, ``.git``, ``.codegraph``, ``.grid``, every
  other hidden entry). A file is a file on disk, not a verified result.

No message text beyond a chat's title, no task notes or descriptions, no link
target and no absolute filesystem path leaves this route. bd and the folder
listing run off the event loop, each under its own deadline.

The answer is stable JSON with an ETag (sha256 of its bytes), so a browser
that already has this map revalidates it with ``If-None-Match`` and gets
``304``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response

from tools.beads_tools import _bd_problem, _find_bd
from web_chat.explorer_api import list_folder
from web_chat.space import SPACE_ID
from web_chat.tasks_api import _counts, _issues, _reason, _task
from web_chat.views import conversation_title

#: How many tasks, chats and file entries one map shows at most.
TASKS_LIMIT = 300
PROCESSES_LIMIT = 50
FILES_LIMIT = 100

#: How long bd and the folder listing may take before the map says so.
BD_TIMEOUT_SECONDS = 20.0
FOLDER_TIMEOUT_SECONDS = 10.0

#: What the map reads of a tracker: a list, and nothing that writes.
BD_LIST_ARGS = ("--readonly", "list", "--limit", "0", "--all", "--json", "--sort", "created")

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

#: The kinds of a file entry: a link is a link, never passed off as a file.
FILE_KINDS = ("dir", "file", "link", "other")

NO_TRACKER = "This project has no tracker of its own yet."
NO_SPACE = "This project has no space of its own on this server."
FOREIGN_TRACKER = "The tracker bd found is not this project's own; its tasks are not shown."


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
    found.sort(key=lambda project: ((project.title or "").casefold(), project.id))
    return [{"id": project.id, "title": project.title} for project in found]


def _project_space(space: Any, project_id: str) -> Optional[Path]:
    """The directory of the project's space, when this server keeps one.

    Only a name a space may have (``SPACE_ID``) becomes a path; nothing is
    created - the directory may well not exist yet.
    """
    if not getattr(space, "chat_spaces", False):
        return None
    if not SPACE_ID.fullmatch(str(project_id or "")):
        return None
    layout = getattr(space, "layout", None)
    project_spaces = getattr(layout, "project_spaces", None) if layout is not None else None
    if project_spaces is None:
        return None
    return Path(project_spaces) / project_id


def _own_tracker(directory: Optional[Path]) -> Optional[Path]:
    """An existing local database, or None *before* bd may discover anything.

    Support embedded Dolt and legacy SQLite, not redirects or remote/shared
    servers. A nonempty .beads (even metadata alone) is not a database pin.
    Configuration is deliberately fail-closed: unknown selector-bearing
    settings must not silently turn a project read into a different read.
    """
    if directory is None:
        return None
    directory = directory.absolute()
    beads = directory / ".beads"
    try:
        # HOME is rebound to the project for Dolt (it needs a directory).
        # Do not admit additional user-config roots beneath that HOME/XDG.
        if os.path.lexists(directory / ".config") or os.path.lexists(beads / "bd"):
            return None
        if directory.resolve() != directory or not stat.S_ISDIR(os.lstat(directory).st_mode):
            return None
        if not stat.S_ISDIR(os.lstat(beads).st_mode):
            return None
        # bd follows redirects and loads .env before opening its store.
        if any(os.path.lexists(beads / name) for name in ("redirect", ".env", "config.json", "config.local.yaml")):
            return None
        config = beads / "config.yaml"
        # Required even when empty: otherwise bd's config search climbs above
        # this project independently of the explicit database selection.
        if not stat.S_ISREG(os.lstat(config).st_mode):
            return None
        settings = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        if not isinstance(settings, dict) or set(settings) - {"issue-prefix", "actor", "json", "no-color"}:
            return None
        metadata = beads / "metadata.json"
        if os.path.lexists(metadata):
            if not stat.S_ISREG(os.lstat(metadata).st_mode):
                return None
            data = json.loads(metadata.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            if data.get("backend") == "dolt":
                # bd normalizes SQL database names independently of issue IDs.
                db_name = directory.name.replace("-", "_")
                if (set(data) - {"database", "backend", "dolt_mode", "dolt_database", "project_id"}
                        or data.get("database") != "dolt"
                        or data.get("dolt_mode") != "embedded"
                        or data.get("dolt_database") != db_name):
                    return None
                database = beads / "embeddeddolt"
                witness = database / db_name / ".dolt"
                if not stat.S_ISDIR(os.lstat(witness).st_mode):
                    return None
            elif set(data) <= {"database", "jsonl_export"} and data.get("database") == "beads.db":
                database = beads / "beads.db"
            else:
                return None
        else:
            database = beads / "beads.db"
        if database.name == "beads.db":
            if not stat.S_ISREG(os.lstat(database).st_mode):
                return None
            for suffix in ("-wal", "-shm", "-journal"):
                if os.path.lexists(str(database) + suffix) and not stat.S_ISREG(os.lstat(str(database) + suffix).st_mode):
                    return None
        else:
            if not stat.S_ISDIR(os.lstat(database).st_mode):
                return None
            # A linked Dolt data directory is just as foreign as a linked .beads.
            for root, dirs, files in os.walk(database, followlinks=False):
                for name in dirs + files:
                    if stat.S_ISLNK(os.lstat(Path(root) / name).st_mode):
                        return None
        if database.resolve() != database:
            return None
    except (OSError, ValueError, yaml.YAMLError):
        return None
    return database


def _read_tracker(directory: Path, args: List[str]) -> dict[str, Any]:
    """Run only an explicitly pinned read, with no ambient database selectors.

    Do not use run_bd here: it inherits BEADS/BD selectors and user config.
    --db alone is insufficient: bd 1.3 treats a missing path as a DB *name*.
    """
    database = _own_tracker(directory)
    if database is None:
        return {"success": False, "error": NO_TRACKER}
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMPDIR"}}
    env.update({"BEADS_DIR": str(database.parent), "BEADS_DB": str(database),
                "BEADS_DAEMON": "0", "BD_DISABLE_METRICS": "1", "BD_DISABLE_EVENT_FLUSH": "1",
                "HOME": str(directory), "XDG_CONFIG_HOME": str(directory / ".beads"),
                "APPDATA": str(directory / ".beads"), "USERPROFILE": str(directory),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    try:
        result = subprocess.run([_find_bd(), "--db", str(database), *args], cwd=directory, check=False,
                                env=env, capture_output=True, text=True, timeout=BD_TIMEOUT_SECONDS)
        if result.returncode:
            # bd errors may contain absolute paths or remote credentials.
            return {"success": False, "error": "The project's local tracker could not be read."}
        return {"success": True, "data": json.loads(result.stdout)}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "The tracker did not answer in time."}
    except (OSError, ValueError):
        return {"success": False, "error": "The project's local tracker could not be read."}


async def _bd_in(directory: Path, args: List[str]) -> dict[str, Any]:
    """One bd command in *directory*; 503 when bd cannot run here at all."""
    problem = _bd_problem()
    if problem:
        raise HTTPException(status_code=503, detail=f"The tracker is unavailable: {problem}")
    return await asyncio.to_thread(_read_tracker, directory, args)


def _no_tasks(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "items": [], "total": 0,
            "truncated": False, "counts": _counts([])}


async def _tasks(space: Any, project_id: str) -> dict[str, Any]:
    """The project's own tracker as the map's task items, bounded and sorted by age."""
    directory = _project_space(space, project_id)
    if directory is None:
        return _no_tasks(NO_SPACE)
    tracker = await asyncio.to_thread(_own_tracker, directory)
    if tracker is None:
        return _no_tasks(NO_TRACKER)
    try:
        answer = await asyncio.wait_for(_bd_in(directory, list(BD_LIST_ARGS)), BD_TIMEOUT_SECONDS + 1)
    except asyncio.TimeoutError:
        return _no_tasks("The tracker did not answer in time.")
    except HTTPException as exc:
        return _no_tasks(str(exc.detail))
    if not answer.get("success"):
        return _no_tasks(_reason(answer))
    try:
        issues = _issues(answer)
    except HTTPException as exc:
        return _no_tasks(str(exc.detail))
    # Defense in depth, not discovery isolation: the local database may have
    # been populated with issues belonging to a different project.
    prefix = f"{project_id}-"
    if any(not str(issue.get("id") or "").startswith(prefix) for issue in issues):
        return _no_tasks(FOREIGN_TRACKER)
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
        # Over every task of the tracker, not over the shown items.
        "counts": _counts(mapped),
    }


def _processes(space: Any, project_id: str) -> dict[str, Any]:
    """The chats of the project: which work now, which wait for the user.

    ``conversation_views`` gives every chat of the space (its metadata copied,
    its message list a shallow copy of references); the title is the user's,
    or the first words of the chat's first user message, as the rail shows it.
    """
    manager = getattr(space, "context_manager", None)
    empty = {"items": [], "total": 0, "truncated": False, "counts": {"active": 0, "awaiting_approval": 0}}
    if manager is None:
        return empty
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
    # The running chats above the idle ones; newest first; the id breaks a tie.
    items.sort(key=lambda item: str(item["chat_id"]))
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    items.sort(key=lambda item: not item["active"])
    return {
        "items": items[:PROCESSES_LIMIT],
        "total": len(items),
        "truncated": len(items) > PROCESSES_LIMIT,
        "counts": {
            "active": sum(item["active"] for item in items),
            "awaiting_approval": sum(item["awaiting_approval"] for item in items),
        },
    }


def _modified(mtime: Any) -> Optional[str]:
    if not isinstance(mtime, (int, float)):
        return None
    try:
        moment = datetime.fromtimestamp(float(mtime), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _file_counts(items: List[dict[str, Any]]) -> dict[str, int]:
    return {kind: sum(item["kind"] == kind for item in items) for kind in FILE_KINDS}


def _no_files(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "items": [], "total": 0, "truncated": False,
            "counts": _file_counts([])}


def _listing(entries: List[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for entry in entries:
        name = str(entry.get("name") or "")
        # Tools keep their state in hidden folders; they are not the project's files.
        if not name or name.startswith("."):
            continue
        kind = entry.get("kind") if entry.get("kind") in FILE_KINDS else "other"
        items.append(
            {
                "name": name,
                # Relative to the project's space: never an absolute path, never a link's target.
                "path": entry.get("path") or name,
                "kind": kind,
                "size": entry.get("size") if kind == "file" else None,
                "modified": _modified(entry.get("mtime")),
            }
        )
    return {
        "available": True,
        "reason": None,
        "items": items[:FILES_LIMIT],
        "total": len(items),
        "truncated": len(items) > FILES_LIMIT,
        # Over every entry, not over the shown items.
        "counts": _file_counts(items),
    }


async def _files(space: Any, project_id: str) -> dict[str, Any]:
    """The top level of the project's space: what is on disk there, nothing more."""
    directory = _project_space(space, project_id)
    if directory is None:
        return _no_files(NO_SPACE)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        # Nothing written there yet: an empty space, and the map does not make it.
        return _listing([])
    except OSError:
        return _no_files("This project's space cannot be read.")
    if not stat.S_ISDIR(info.st_mode):
        return _no_files("This project's space is not a folder; it is not read.")
    try:
        listing = await asyncio.wait_for(
            asyncio.to_thread(list_folder, directory, ""), FOLDER_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return _no_files("The project's files did not answer in time.")
    except Exception:  # noqa: BLE001 - gone meanwhile, refused: no files, not a 500
        return _no_files("This project's space cannot be read.")
    return _listing([entry for entry in listing.get("entries") or [] if isinstance(entry, dict)])


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
        if not SPACE_ID.fullmatch(project_id) or not space.project_exists(project_id):
            raise HTTPException(status_code=404, detail="Project not found")
        store = space.project_store
        project = store.get(project_id)
        if project is None:  # deleted between the two reads
            raise HTTPException(status_code=404, detail="Project not found")
        tasks, files = await asyncio.gather(_tasks(space, project_id), _files(space, project_id))
        processes = _processes(space, project_id)
        payload = {
            "project": _project_payload(project),
            "path": _ancestors(store, project),
            "children": _children(store, project_id),
            "tasks": tasks,
            "processes": processes,
            "files": files,
            "files_url": _files_url(space, project_id, [item["chat_id"] for item in processes["items"]]),
        }
        body = _canonical(payload)
        etag = _etag_of(body)
        # One user's map: no shared cache keeps it, and every use revalidates.
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if _matches(if_none_match, etag):
            return Response(status_code=304, headers=headers)
        return Response(content=body, media_type="application/json", headers=headers)
