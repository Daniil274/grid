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
  Every returned id must additionally carry the project's prefix. bd opens a
  bounded temporary copy of that tracker, with its home in the same temporary
  tree - embedded Dolt writes lock and journal files even for a ``--readonly``
  list - so neither bd nor Dolt writes inside the project. Without a host bd
  the same copy is opened by the isolation image, and the project itself is
  only ever mounted read-only;
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
import functools
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response

from tools.beads_tools import _docker_fallback_enabled, _find_bd, _get_isolation_image
from web_chat.build_info import SHORT as BUILD_COMMIT_SHORT
from web_chat.build_info import UNKNOWN, read_build_info
from web_chat.explorer_api import list_folder
from web_chat.space import SPACE_ID
from web_chat.tasks_api import _counts, _issues, _task
from web_chat.views import conversation_title

logger = logging.getLogger(__name__)

#: How many tasks, chats and file entries one map shows at most.
TASKS_LIMIT = 300
PROCESSES_LIMIT = 50
FILES_LIMIT = 100

#: How long bd and the folder listing may take before the map says so.
BD_TIMEOUT_SECONDS = 20.0
FOLDER_TIMEOUT_SECONDS = 10.0

#: What of the server's environment bd may inherit; everything else is dropped.
INHERITED_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMPDIR")

#: The bounds of the tracker copy one read is allowed to make.
MAX_TRACKER_COPY_BYTES = 128 * 1024 * 1024
MAX_TRACKER_COPY_ENTRIES = 20_000

#: Where the copy appears inside the isolation image; the project is mounted
#: read-only beside it and is never the database bd opens.
CONTAINER_TRACKER_DIR = "/tracker"
CONTAINER_PROJECT_DIR = "/project"

#: How long reading the image's user may take before the copy is run as its owner.
IMAGE_USER_TIMEOUT_SECONDS = 10.0

#: How much of bd's own message a log line keeps.
LOG_LIMIT = 800

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


class ReasonCode(str, Enum):
    """Why the map shows no tasks: a code to branch on, never a sentence.

    The browser gets the prose of :data:`REASON_TEXT` and nothing else; the code
    stays on this side, where a diagnostic can name the cause without parsing
    text. New causes get a new member - never a new sentence in a caller.
    """

    NO_SPACE = "no_space"
    NO_TRACKER = "no_tracker"
    FOREIGN_TRACKER = "foreign_tracker"
    BD_UNAVAILABLE = "bd_unavailable"
    BD_FAILED = "bd_failed"
    TIMEOUT = "timeout"
    INVALID_ANSWER = "invalid_answer"
    TRACKER_TOO_LARGE = "tracker_too_large"
    # The sections the map reads without bd name their own causes.
    NO_CHATS = "no_chats"
    UNREADABLE = "unreadable"
    NOT_A_FOLDER = "not_a_folder"
    FILES_TIMEOUT = "files_timeout"


#: What the map tells the browser about each cause: no paths, no bd stderr.
REASON_TEXT = {
    ReasonCode.NO_SPACE: "This project has no space of its own on this server.",
    ReasonCode.NO_TRACKER: "This project has no tracker of its own yet.",
    ReasonCode.FOREIGN_TRACKER: "The tracker bd found is not this project's own; its tasks are not shown.",
    ReasonCode.BD_UNAVAILABLE: "No issue tracker runtime is available on this server.",
    ReasonCode.BD_FAILED: "The project's local tracker could not be read.",
    ReasonCode.TIMEOUT: "The tracker did not answer in time.",
    ReasonCode.INVALID_ANSWER: "The project's local tracker answered with something the map cannot read.",
    ReasonCode.TRACKER_TOO_LARGE: "The project's tracker is too large to read on the map.",
    ReasonCode.NO_CHATS: "This project's chats cannot be listed on this server.",
    ReasonCode.UNREADABLE: "This project's space cannot be read.",
    ReasonCode.NOT_A_FOLDER: "This project's space is not a folder; it is not read.",
    ReasonCode.FILES_TIMEOUT: "The project's files did not answer in time.",
}

#: The files section names the same missing space with the same words.
NO_SPACE = REASON_TEXT[ReasonCode.NO_SPACE]

#: The server section: a short commit, or the one reason it cannot be named.
BUILD_UNKNOWN = "build_commit_unknown"
BUILD_UNKNOWN_TEXT = "This server does not know which commit of the code it runs."
_HEX = frozenset("0123456789abcdef")


@functools.lru_cache(maxsize=1)
def _build_info() -> dict[str, Any]:
    """Which code this process runs, read once: it cannot change under it."""
    return read_build_info()


def _public_build(info: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The build identity a page may show: a short commit or a typed reason.

    No full hash, no checkout path and no environment value beyond a validated
    short commit: either ``commit`` names the code, or ``reason_code`` names the
    one honest answer - this server cannot tell. ``version`` is always there.
    """
    info = _build_info() if info is None else info
    version = str(info.get("version") or UNKNOWN)
    commit = str(info.get("commit") or "").strip().lower()
    if len(commit) < BUILD_COMMIT_SHORT or not set(commit) <= _HEX:
        return {"version": version, "commit": None, "source": "unknown",
                "reason_code": BUILD_UNKNOWN, "reason": BUILD_UNKNOWN_TEXT}
    return {"version": version, "commit": commit[:BUILD_COMMIT_SHORT],
            "source": str(info.get("source") or UNKNOWN),
            "reason_code": None, "reason": None}


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
        # A user-config root in the project would be read through HOME/XDG.
        # Do not admit one: the read binds its own home outside the project.
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


def _failure(code: ReasonCode) -> dict[str, Any]:
    """A typed refusal: the code branches, the text is all the browser sees."""
    return {"success": False, "code": code, "error": REASON_TEXT[code]}


def _logged(text: Optional[str]) -> str:
    """bd's own message, one line and bounded: a log line, never a payload."""
    collapsed = " ".join((text or "").split())
    return collapsed[:LOG_LIMIT] + ("..." if len(collapsed) > LOG_LIMIT else "")


def _bd_executable() -> Optional[str]:
    """The host's bd when it is a real executable, else None (fallback territory).

    ``_find_bd`` answers bare ``bd`` when nothing is installed; that string is
    not a program, and asking subprocess for it is how the map used to lose the
    tracker on a server without bd.
    """
    found = _find_bd()
    if os.path.isfile(found) and os.access(found, os.X_OK):
        return found
    return shutil.which(found)


def _docker_bd_available() -> bool:
    """Whether bd may be run in the isolation image instead of on this host."""
    return _docker_fallback_enabled() and shutil.which("docker") is not None


@dataclass(frozen=True)
class Runner:
    """Which bd runtime one read uses, and the fact that chose it.

    ``kind`` is ``"host"`` when this server has a real bd, ``"docker"`` when
    the isolation image runs it, and ``None`` when there is no runtime at all.
    A diagnostic prints this; the map itself never does.
    """

    kind: Optional[str]
    executable: Optional[str] = None
    image: Optional[str] = None
    why: str = ""


@dataclass
class ReadTrace:
    """What one read did, for a human at a terminal - never for the browser."""

    runner: Runner = Runner(None, why="no runtime has been chosen yet")
    command: Optional[List[str]] = None
    returncode: Optional[int] = None
    stderr: str = ""
    reason: Optional[ReasonCode] = None

    def record(self, code: Optional[ReasonCode], **fields: Any) -> None:
        """Keep the cause of a refusal and what the run reported."""
        self.reason = code
        for name, value in fields.items():
            setattr(self, name, value)


def _resolve_runner() -> Runner:
    """Choose the bd runtime of one read, and say why, in one place.

    Both the map and ``python -m web_chat.map_check`` go through here, so a
    diagnostic cannot disagree with what the server would do.
    """
    host = _bd_executable()
    if host is not None:
        return Runner("host", executable=host, why=f"an executable bd on this host: {host}")
    if not _docker_bd_available():
        if not _docker_fallback_enabled():
            why = "no executable bd on PATH and the docker fallback is disabled (BEADS_DISABLE_DOCKER_FALLBACK)"
        else:
            why = "no executable bd on PATH and no docker on PATH to run the isolation image"
        return Runner(None, why=why)
    image = _get_isolation_image(None) or ""
    if not image:
        return Runner(None, why="no executable bd on PATH and the isolation image is not configured")
    return Runner("docker", image=image,
                  why=f"no executable bd on PATH; bd runs in the isolation image {image}")


def _tracker_env(home: Path, database: Path) -> dict[str, str]:
    """The environment of one read: the tracker pinned, every home in *home*."""
    env = {key: value for key, value in os.environ.items() if key in INHERITED_ENV}
    env.update({
        "BEADS_DIR": str(database.parent),
        "BEADS_DB": str(database),
        "BEADS_DAEMON": "0",
        "BD_DISABLE_METRICS": "1",
        "BD_DISABLE_EVENT_FLUSH": "1",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "APPDATA": str(home),
        "USERPROFILE": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    return env


def _tracker_copy(directory: Path, beads: Path, work: Path) -> tuple[Optional[Path], Optional[ReasonCode]]:
    """A bounded copy of *beads*, in a work dir named as the project.

    Embedded Dolt opens its store for writing even for a ``--readonly`` read:
    measured on bd 1.3.0, a list run creates ``.beads.gate.lock`` in its cwd
    and rewrites ``last-touched`` and the noms journal. A read-only mount is
    not an option either - Dolt then cannot open the database at all
    (``openat LOCK: permission denied``). So bd opens this copy, and runs with
    its cwd in the same temporary tree: the project's own files never change.

    The work dir keeps the project's name, because bd normalizes the database
    name from the directory it sits in and the copy must validate as the same
    self-contained store.
    """
    total = 0
    entries = 0
    try:
        for parent, dirs, files in os.walk(beads, followlinks=False):
            for name in dirs + files:
                entries += 1
                info = os.lstat(Path(parent) / name)
                if stat.S_ISLNK(info.st_mode):
                    # A link is not a byte of this tracker: refuse, never follow it.
                    return None, ReasonCode.BD_FAILED
                total += info.st_size
                if entries > MAX_TRACKER_COPY_ENTRIES or total > MAX_TRACKER_COPY_BYTES:
                    return None, ReasonCode.TRACKER_TOO_LARGE
        copied = work / directory.name
        shutil.copytree(beads, copied / ".beads", symlinks=True)
        # The copy is ours: Dolt may write to it whatever the source mode was.
        for parent, dirs, files in os.walk(copied):
            for name in dirs + files:
                path = Path(parent) / name
                try:
                    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IRUSR | stat.S_IWUSR)
                except OSError as exc:
                    logger.warning("map: tracker copy of %s is not writable: %s", directory.name, exc)
                    return None, ReasonCode.BD_FAILED
        return copied, None
    except OSError as exc:
        logger.warning("map: could not copy the tracker of %s: %s", directory.name, exc)
        return None, ReasonCode.BD_FAILED


@functools.lru_cache(maxsize=8)
def _container_uid(image: str) -> Optional[int]:
    """The uid the isolation image runs as, or None when it cannot be read.

    A bind mount keeps the host's owners: a root server's copy is root's, and
    the image (``USER grid``, uid 1000) cannot even open it. The image knows its
    own user, so ask it once per image rather than guess a number.
    """
    try:
        done = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "id", image, "-u"],
            capture_output=True, text=True, encoding="utf-8", check=False,
            timeout=IMAGE_USER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("map: the user of image %s could not be read: %s", image, exc)
        return None
    if done.returncode:
        return None
    try:
        return int((done.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        return None


def _host_ids() -> Optional[tuple[int, int]]:
    """This process's uid and gid, or None where the host has no such ids.

    A Windows Docker Desktop bind mount does not carry Unix owners, so there is
    nothing to hand over there and the image's own user reads the copy.
    """
    getuid, getgid = getattr(os, "geteuid", None), getattr(os, "getegid", None)
    if not callable(getuid) or not callable(getgid):
        return None
    return int(getuid()), int(getgid())


def _owner_only(path: Path, uid: int) -> None:
    """Give one path to *uid* and to nobody else: 0700 for a directory, 0600 otherwise."""
    info = os.lstat(path)
    if info.st_uid != uid:
        os.lchown(path, uid, -1)
    if stat.S_ISLNK(info.st_mode):
        return  # a link has no mode of its own; the copy holds none anyway
    os.chmod(path, 0o700 if stat.S_ISDIR(info.st_mode) else 0o600)


def _hand_over_copy(copied: Path, uid: int) -> bool:
    """Make the ephemeral copy *uid*'s own, owner-only, top to bottom.

    The copy lives inside the server's 0700 temporary root, which no other host
    user can enter; inside it, only the uid that bd runs as may read or write.
    False means this process may not give it away (it is not root): the caller
    then runs bd as the copy's own owner instead.
    """
    try:
        _owner_only(copied, uid)
        for parent, dirs, files in os.walk(copied):
            for name in dirs + files:
                _owner_only(Path(parent) / name, uid)
    except OSError as exc:
        logger.warning("map: the tracker copy could not be handed to uid %s: %s", uid, exc)
        return False
    return True


def _answer(result: Any, trace: Optional[ReadTrace] = None) -> dict[str, Any]:
    """bd's exit as a typed answer; the real failure goes to the log, not out."""
    if trace is not None:
        trace.record(None, returncode=result.returncode, stderr=result.stderr or "")
    if result.returncode:
        # bd errors may contain absolute paths or remote credentials.
        logger.warning("map: bd exited %s: %s", result.returncode, _logged(result.stderr))
        if trace is not None:
            trace.reason = ReasonCode.BD_FAILED
        return _failure(ReasonCode.BD_FAILED)
    try:
        return {"success": True, "data": json.loads(result.stdout)}
    except ValueError:
        logger.warning("map: bd answered with what is not JSON: %s", _logged(result.stdout))
        if trace is not None:
            trace.reason = ReasonCode.INVALID_ANSWER
        return _failure(ReasonCode.INVALID_ANSWER)


def _run_on_host(bd: str, cwd: Path, database: Path, args: List[str], home: Path,
                 trace: Optional[ReadTrace] = None) -> dict[str, Any]:
    """One pinned read with the host's bd, against the copy in *cwd*."""
    command = [bd, "--db", str(database), *args]
    if trace is not None:
        trace.command = command
    try:
        result = subprocess.run(command, cwd=cwd, check=False,
                                env=_tracker_env(home, database), capture_output=True, text=True,
                                timeout=BD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning("map: bd %s did not answer in %ss", " ".join(args), BD_TIMEOUT_SECONDS)
        if trace is not None:
            trace.reason = ReasonCode.TIMEOUT
        return _failure(ReasonCode.TIMEOUT)
    except OSError as exc:
        logger.warning("map: bd could not run: %s", exc)
        if trace is not None:
            trace.record(ReasonCode.BD_UNAVAILABLE, stderr=str(exc))
        return _failure(ReasonCode.BD_UNAVAILABLE)
    return _answer(result, trace)


def _run_in_container(image: str, project: Path, cwd: Path, database: Path,
                      args: List[str], trace: Optional[ReadTrace] = None,
                      user: Optional[str] = None) -> dict[str, Any]:
    """One pinned read in the isolation image, when this host has no bd.

    Same command as ``tools.beads_tools._run_bd_via_docker_run`` builds, but
    the database is the temporary copy: the project is mounted read-only as a
    guard, and bd cannot write to it even if it tried. *user* is only given when
    the copy cannot be handed to the image's own user: then bd runs as the
    copy's owner, so owner-only modes still let it in.
    """
    # Docker on Windows generally accepts forward slashes in volume specs.
    pinned = Path(CONTAINER_TRACKER_DIR) / database.relative_to(cwd)
    command = [
        "docker", "run", "--rm", "-i",
        # Only these values reach the container: no ambient BEADS/BD selectors.
        "-e", "BEADS_DAEMON=0",
        "-e", "BD_DISABLE_METRICS=1",
        "-e", "BD_DISABLE_EVENT_FLUSH=1",
        "-e", f"BEADS_DIR={Path(CONTAINER_TRACKER_DIR) / '.beads'}",
        "-e", f"BEADS_DB={pinned}",
        "-e", "HOME=/tmp",
        "-v", f"{str(project).replace(os.sep, '/')}:{CONTAINER_PROJECT_DIR}:ro",
        "-v", f"{str(cwd).replace(os.sep, '/')}:{CONTAINER_TRACKER_DIR}",
        "-w", CONTAINER_TRACKER_DIR,
    ]
    if user:
        command.extend(["--user", user])
    command.extend([
        image,
        # Stopping the client alone would leave bd running, holding the database.
        "timeout", "-k", "5", str(int(BD_TIMEOUT_SECONDS)),
        "/usr/local/bin/bd", "--db", str(pinned), *args,
    ])
    if trace is not None:
        trace.command = command
    try:
        logger.debug("map: reading the tracker via docker: %s", " ".join(command))
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                check=False, timeout=BD_TIMEOUT_SECONDS + 10)
    except subprocess.TimeoutExpired:
        logger.warning("map: bd in the isolation image did not answer in %ss", BD_TIMEOUT_SECONDS)
        if trace is not None:
            trace.reason = ReasonCode.TIMEOUT
        return _failure(ReasonCode.TIMEOUT)
    except OSError as exc:
        logger.warning("map: docker could not run: %s", exc)
        if trace is not None:
            trace.record(ReasonCode.BD_UNAVAILABLE, stderr=str(exc))
        return _failure(ReasonCode.BD_UNAVAILABLE)
    return _answer(result, trace)


def _read_tracker(directory: Path, args: List[str],
                  trace: Optional[ReadTrace] = None) -> dict[str, Any]:
    """Read the project's tracker through bd, writing nothing into the project.

    Only an explicitly pinned read runs, and it runs on a temporary copy:

    Do not use run_bd here: it inherits BEADS/BD selectors and user config.
    --db alone is insufficient: bd 1.3 treats a missing path as a DB *name*.

    *trace*, when given, collects what a terminal diagnostic needs: the runner
    and why it was chosen, the command, the exit, bd's own stderr and the
    reason code. The route never passes one.
    """
    database = _own_tracker(directory)
    if database is None:
        if trace is not None:
            trace.record(ReasonCode.NO_TRACKER)
        return _failure(ReasonCode.NO_TRACKER)
    runner = _resolve_runner()
    if trace is not None:
        trace.runner = runner
    if runner.kind is None:
        logger.warning("map: no bd on this host and no docker fallback for %s", directory.name)
        if trace is not None:
            trace.record(ReasonCode.BD_UNAVAILABLE)
        return _failure(ReasonCode.BD_UNAVAILABLE)
    try:
        with tempfile.TemporaryDirectory(prefix="grid-map-") as root:
            base = Path(root).resolve()
            home, work = base / "home", base / "work"
            home.mkdir()
            work.mkdir()
            copied, code = _tracker_copy(directory, database.parent, work)
            if copied is None:
                logger.warning("map: tracker of %s was not copied (%s)",
                               directory.name, code.value if code else "?")
                if trace is not None:
                    trace.record(code or ReasonCode.BD_FAILED)
                return _failure(code or ReasonCode.BD_FAILED)
            pinned = _own_tracker(copied)
            if pinned is None:
                logger.warning("map: the copy of %s is not a self-contained local database", directory.name)
                if trace is not None:
                    trace.record(ReasonCode.BD_FAILED)
                return _failure(ReasonCode.BD_FAILED)
            if runner.kind == "host":
                return _run_on_host(runner.executable or "", copied, pinned, args, home, trace)
            image = runner.image or ""
            # A bind mount keeps host owners: hand the copy to the image's user,
            # owner-only. If this process cannot (it is neither root nor that
            # user), run bd as the copy's owner instead of widening its modes.
            host = _host_ids()
            if host is None:
                return _run_in_container(image, directory, copied, pinned, args, trace)
            uid = _container_uid(image)
            if (uid is not None and (uid == host[0] or host[0] == 0)
                    and _hand_over_copy(copied, uid)):
                return _run_in_container(image, directory, copied, pinned, args, trace)
            # Still owner-only: bd will run as that owner, never as the world.
            _hand_over_copy(copied, host[0])
            return _run_in_container(
                image, directory, copied, pinned, args, trace,
                user=f"{host[0]}:{host[1]}",
            )
    except OSError as exc:
        logger.warning("map: the tracker read of %s could not start: %s", directory.name, exc)
        if trace is not None:
            trace.record(ReasonCode.BD_FAILED, stderr=str(exc))
        return _failure(ReasonCode.BD_FAILED)


async def _bd_in(directory: Path, args: List[str],
                 trace: Optional[ReadTrace] = None) -> dict[str, Any]:
    """One bd read in *directory*, off the event loop; it never raises."""
    return await asyncio.to_thread(_read_tracker, directory, args, trace)


@dataclass(frozen=True)
class TasksSlice:
    """The tasks section of the map and, beside it, why it is empty.

    The payload is the browser's: prose only. The code stays here, so a
    diagnostic surface can name the cause without parsing that prose.
    """

    payload: dict[str, Any]
    reason: Optional[ReasonCode] = None


def _no_tasks(code: ReasonCode) -> dict[str, Any]:
    return {"available": False, "reason": REASON_TEXT[code], "reason_code": code.value,
            "items": [], "total": 0,
            "truncated": False, "counts": _counts([])}


async def _tasks(space: Any, project_id: str, trace: Optional[ReadTrace] = None) -> TasksSlice:
    """The project's own tracker as the map's task items, bounded and sorted by age."""
    directory = _project_space(space, project_id)
    if directory is None:
        if trace is not None:
            trace.record(ReasonCode.NO_SPACE)
        return TasksSlice(_no_tasks(ReasonCode.NO_SPACE), ReasonCode.NO_SPACE)
    tracker = await asyncio.to_thread(_own_tracker, directory)
    if tracker is None:
        if trace is not None:
            trace.record(ReasonCode.NO_TRACKER)
        return TasksSlice(_no_tasks(ReasonCode.NO_TRACKER), ReasonCode.NO_TRACKER)
    try:
        if trace is None:
            read = _bd_in(directory, list(BD_LIST_ARGS))
        else:
            read = _bd_in(directory, list(BD_LIST_ARGS), trace)
        answer = await asyncio.wait_for(read, BD_TIMEOUT_SECONDS + 1)
    except asyncio.TimeoutError:
        if trace is not None:
            trace.record(ReasonCode.TIMEOUT)
        return TasksSlice(_no_tasks(ReasonCode.TIMEOUT), ReasonCode.TIMEOUT)
    if not answer.get("success"):
        code = answer.get("code")
        if not isinstance(code, ReasonCode):
            code = ReasonCode.BD_FAILED
        if trace is not None:
            trace.reason = code
        return TasksSlice(_no_tasks(code), code)
    try:
        issues = _issues(answer)
    except HTTPException:
        # bd's own words are not a task list; they were logged where bd ran.
        logger.warning("map: bd answered without a task list for %s", project_id)
        if trace is not None:
            trace.reason = ReasonCode.INVALID_ANSWER
        return TasksSlice(_no_tasks(ReasonCode.INVALID_ANSWER), ReasonCode.INVALID_ANSWER)
    # Defense in depth, not discovery isolation: the local database may have
    # been populated with issues belonging to a different project.
    prefix = f"{project_id}-"
    if any(not str(issue.get("id") or "").startswith(prefix) for issue in issues):
        if trace is not None:
            trace.reason = ReasonCode.FOREIGN_TRACKER
        return TasksSlice(_no_tasks(ReasonCode.FOREIGN_TRACKER), ReasonCode.FOREIGN_TRACKER)
    statuses = {issue.get("id"): issue.get("status") for issue in issues}
    mapped = [_task(issue, statuses) for issue in issues]
    # Oldest first: a task keeps its place on the map as newer ones arrive.
    mapped.sort(key=lambda task: (task.get("created_at") or "", task.get("id") or ""))
    items = [{name: task.get(name) for name in TASK_FIELDS} for task in mapped]
    return TasksSlice({
        "available": True,
        "reason": None,
        "reason_code": None,
        "items": items[:TASKS_LIMIT],
        "total": len(items),
        "truncated": len(items) > TASKS_LIMIT,
        # Over every task of the tracker, not over the shown items.
        "counts": _counts(mapped),
    })


def _processes(space: Any, project_id: str) -> dict[str, Any]:
    """The chats of the project: which work now, which wait for the user.

    ``conversation_views`` gives every chat of the space (its metadata copied,
    its message list a shallow copy of references); the title is the user's,
    or the first words of the chat's first user message, as the rail shows it.
    """
    manager = getattr(space, "context_manager", None)
    unavailable = {
        "available": False,
        "reason": REASON_TEXT[ReasonCode.NO_CHATS],
        "reason_code": ReasonCode.NO_CHATS.value,
        "items": [],
        "total": 0,
        "truncated": False,
        "counts": {"active": 0, "awaiting_approval": 0},
    }
    if manager is None:
        return unavailable
    try:
        views = manager().conversation_views()
    except Exception:  # noqa: BLE001 - a map without chats is still a map
        logger.warning("map: the chats of %s could not be listed", project_id)
        return unavailable
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
        "available": True,
        "reason": None,
        "reason_code": None,
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


def _no_files(code: ReasonCode) -> dict[str, Any]:
    return {"available": False, "reason": REASON_TEXT[code], "reason_code": code.value,
            "items": [], "total": 0, "truncated": False,
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
        "reason_code": None,
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
        return _no_files(ReasonCode.NO_SPACE)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        # Nothing written there yet: an empty space, and the map does not make it.
        return _listing([])
    except OSError:
        return _no_files(ReasonCode.UNREADABLE)
    if not stat.S_ISDIR(info.st_mode):
        return _no_files(ReasonCode.NOT_A_FOLDER)
    try:
        listing = await asyncio.wait_for(
            asyncio.to_thread(list_folder, directory, ""), FOLDER_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return _no_files(ReasonCode.FILES_TIMEOUT)
    except Exception:  # noqa: BLE001 - gone meanwhile, refused: no files, not a 500
        return _no_files(ReasonCode.UNREADABLE)
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


def register_map_routes(api: APIRouter, current_space: Any,
                        build: Optional[Callable[[], dict[str, Any]]] = None) -> None:
    """Mount the map route on *api*; it resolves the space itself.

    *build* returns the build identity the server read at startup (a dict from
    :func:`web_chat.build_info.read_build_info`); without it the map reads the
    checkout itself, once per process.
    """
    read_build = build or _build_info

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
            "server": _public_build(read_build()),
            "tasks": tasks.payload,
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
