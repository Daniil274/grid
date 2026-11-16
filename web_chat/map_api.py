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
  that copy is streamed to the isolation image as a bounded tar archive on
  stdin and unpacked there into the container's own writable temporary
  directory: no host path is mounted and the daemon never needs to see the
  server's /tmp (which ``PrivateTmp=true`` makes private to the server). If bd
  explicitly refuses historical SQLite, a bounded read-only SQLite adapter
  reads the admitted
  copy without migrating it. The project is never opened for writing;
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
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Optional
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response

from tools.beads_tools import _docker_fallback_enabled, _find_bd, _get_isolation_image
from web_chat.build_info import SHORT as BUILD_COMMIT_SHORT
from web_chat.build_info import UNKNOWN, read_build_info
from web_chat.explorer_api import list_folder
from web_chat.plan_board import latest_plan
from web_chat.session import (
    ACTIVITY_SUMMARY_LIMIT,
    ACTIVITY_TITLE_LIMIT,
    public_activity,
)
from web_chat.space import SPACE_ID
from web_chat.tasks_api import _counts, _issues, _task
from web_chat.trace import normalize_steps
from web_chat.views import conversation_title

logger = logging.getLogger(__name__)

#: How many tasks, chats and file entries one map shows at most.
TASKS_LIMIT = 300
CHATS_LIMIT = 50
FILES_LIMIT = 100

#: What the map says of a chat's work: the latest steps of its turn, and the
#: tasks the conversation's plan ties it to.
STEPS_LIMIT = 3
STEP_TITLE_LIMIT = 120
TASK_LINKS_LIMIT = 50

#: A chat's detail is loaded lazily, apart from the lightweight map, and each
#: of its sections is bounded on its own: the newest messages and runs, and
#: every operation of them up to a hard cap.
DETAIL_MESSAGES_LIMIT = 30
DETAIL_EXECUTIONS_LIMIT = 30
DETAIL_OPERATIONS_LIMIT = 100
DETAIL_PREVIEW_LIMIT = 180
DETAIL_AGENT_LIMIT = 80
DETAIL_TIME_LIMIT = 40

#: The allowlist of a stored message: its role, its tagged kind and every id.
#: An unknown role is named ``other`` and carries none of its content.
_DETAIL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_DETAIL_ROLES = frozenset({"user", "assistant", "system"})
_DETAIL_KINDS = {
    "continuation": "continuation",
    "context_compacted": "compaction",
    "compaction": "compaction",
    "context": "context",
    "tool_result": "tool_result",
    "agent_response": "agent_response",
}

#: How long bd and the folder listing may take before the map says so.
BD_TIMEOUT_SECONDS = 20.0
FOLDER_TIMEOUT_SECONDS = 10.0

#: What of the server's environment bd may inherit; everything else is dropped.
INHERITED_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMPDIR")

#: The bounds of the tracker copy one read is allowed to make.
MAX_TRACKER_COPY_BYTES = 128 * 1024 * 1024
MAX_TRACKER_COPY_ENTRIES = 20_000

#: Where the streamed copy is unpacked inside the isolation image. The container
#: has a temporary tree of its own; nothing of the host is mounted into it.
CONTAINER_TRACKER_DIR = "/tmp/grid-map-tracker"

#: What the isolation image runs before bd: unpack the archive it is given on
#: stdin into that tree, keep every entry readable only by the image's own user,
#: then exec bd in that directory with the pinned database and the read's args.
#: ``$1`` is the database path inside the container; the rest is bd's argv.
CONTAINER_UNPACK_SCRIPT = """\
set -eu
pinned=$1
shift
root=${pinned%/.beads/*}
mkdir -p "$root"
tar -xf - --no-same-owner -C "$root"
chmod -R u+rwX "$root"
cd "$root"
exec /usr/local/bin/bd --db "$pinned" "$@"
"""

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

#: The relations a chat's task link can carry: a task this turn touched
#: (``turn``) or one its plan holds from earlier (``plan``).
TURN_RELATION = "turn"
PLAN_RELATION = "plan"


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


class Diagnosis(str, Enum):
    """A safe explanation of a failed bd/docker run: a code and fixed prose.

    bd's own stderr may name absolute paths, registry credentials or tokens, so
    it never leaves this process. What the browser gets is one of these members
    and the allowlisted sentence of :data:`DIAGNOSIS_TEXT`; classification reads
    stderr here, the payload never carries it.
    """

    DOCKER_IMAGE_MISSING = "docker_image_missing"
    DOCKER_PERMISSION = "docker_permission"
    DOCKER_UNAVAILABLE = "docker_unavailable"
    BD_MISSING = "bd_missing"
    DATABASE_LOCKED = "database_locked"
    DATABASE_ERROR = "database_error"
    LEGACY_SQLITE = "legacy_sqlite"
    PERMISSION_DENIED = "permission_denied"
    EXIT_UNKNOWN = "exit_unknown"


#: What each diagnosis tells a user who has only the website. Fixed sentences:
#: no stderr, no path, no command and no exit text is ever interpolated here.
DIAGNOSIS_TEXT = {
    Diagnosis.DOCKER_IMAGE_MISSING: (
        "The isolation image is missing on this server; pull or build it, then reload the map."
    ),
    Diagnosis.DOCKER_PERMISSION: (
        "This server's user may not use the Docker daemon; grant Docker access to that user."
    ),
    Diagnosis.DOCKER_UNAVAILABLE: (
        "Docker is not reachable on this server, so the isolation image cannot run."
    ),
    Diagnosis.BD_MISSING: (
        "bd is not installed where the read looks for it; install bd or rebuild the isolation image."
    ),
    Diagnosis.DATABASE_LOCKED: (
        "The tracker database is locked by another process; stop it or retry later."
    ),
    Diagnosis.DATABASE_ERROR: (
        "The tracker database could not be opened. This alone does not establish corruption; check the server diagnostic."
    ),
    Diagnosis.LEGACY_SQLITE: (
        "This bd version cannot open the legacy SQLite tracker, and the map could not read its schema. The tracker has not been migrated."
    ),
    Diagnosis.PERMISSION_DENIED: (
        "The tracker copy cannot be opened by the user bd runs as; check the copy's ownership."
    ),
    Diagnosis.EXIT_UNKNOWN: (
        "bd failed for a cause the map cannot classify; the server log has bd's own message."
    ),
}


#: How a run's stderr is folded into one allowlisted diagnosis, most specific
#: first. Matching is case-insensitive and only decides *which* fixed sentence
#: is used - the matched text itself is never returned.
DIAGNOSIS_PATTERNS = (
    (Diagnosis.DOCKER_IMAGE_MISSING, (
        "unable to find image",
        "no such image",
        "manifest unknown",
        "pull access denied",
        "image not found",
    )),
    (Diagnosis.DOCKER_PERMISSION, (
        "permission denied while trying to connect",
        "docker.sock: permission denied",
        "got permission denied while trying",
    )),
    (Diagnosis.DOCKER_UNAVAILABLE, (
        "cannot connect to the docker daemon",
        "is the docker daemon running",
        "docker daemon is not running",
        "error during connect",
    )),
    (Diagnosis.BD_MISSING, (
        "executable file not found",
        "bd: not found",
        'exec: "bd"',
        "/usr/local/bin/bd: no such file",
        "stat /usr/local/bin/bd",
    )),
    (Diagnosis.DATABASE_LOCKED, (
        "database is locked",
        "database locked",
        "another process is using",
    )),
    (Diagnosis.PERMISSION_DENIED, (
        "permission denied",
        "eacces",
        "operation not permitted",
    )),
    (Diagnosis.LEGACY_SQLITE, (
        "historical sqlite workspace detected",
    )),
    (Diagnosis.DATABASE_ERROR, (
        "failed to open database",
        "error opening database",
        "corrupt",
        "no such table",
        "migration",
    )),
)


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


def _failure(code: ReasonCode,
             diagnosis: Optional[DiagnosisReport] = None) -> dict[str, Any]:
    """A typed refusal: the code branches, the text is all the browser sees.

    *diagnosis*, when a subprocess failed, is the safe cause derived from its
    result; it carries fixed prose, never stderr.
    """
    return {"success": False, "code": code, "error": REASON_TEXT[code],
            "diagnosis": diagnosis}


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
    diagnosis: Optional[DiagnosisReport] = None

    def record(self, code: Optional[ReasonCode], **fields: Any) -> None:
        """Keep the cause of a refusal and what the run reported."""
        self.reason = code
        for name, value in fields.items():
            setattr(self, name, value)


@dataclass(frozen=True)
class DiagnosisReport:
    """One safe diagnosis: an allowlisted code, its fixed sentence, an exit code.

    ``exit_code`` is a bare process status (a small integer), which names no
    path and carries no message; it is omitted when the status is not one.
    """

    code: Diagnosis
    exit_code: Optional[int] = None

    def payload(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": DIAGNOSIS_TEXT[self.code],
                "exit_code": self.exit_code}


def _exit_code(result: Any) -> Optional[int]:
    """The process status when it is a plain small integer, else None."""
    code = getattr(result, "returncode", None)
    if isinstance(code, int) and not isinstance(code, bool) and 0 < code <= 255:
        return code
    return None


def _diagnose(result: Any, runner_kind: Optional[str]) -> DiagnosisReport:
    """Classify a failed run into one allowlisted diagnosis; never echo stderr.

    *result* is the ``subprocess.run`` answer; only its exit status and stderr
    are read, and only to choose a member of :class:`Diagnosis`. The text is
    not returned, logged here, or stored in the report.
    """
    text = str(getattr(result, "stderr", "") or "").lower()
    for code, patterns in DIAGNOSIS_PATTERNS:
        if any(pattern in text for pattern in patterns):
            # A docker-socket refusal is about Docker even when its text also
            # says "permission denied"; the order above keeps that specific.
            if code is Diagnosis.DOCKER_PERMISSION and runner_kind != "docker" and "docker" not in text:
                continue
            return DiagnosisReport(code, _exit_code(result))
    return DiagnosisReport(Diagnosis.EXIT_UNKNOWN, _exit_code(result))


def _os_error_diagnosis(exc: OSError) -> DiagnosisReport:
    """A local ``OSError`` as one allowlisted diagnosis: the exception is not shown."""
    if isinstance(exc, FileNotFoundError):
        return DiagnosisReport(Diagnosis.BD_MISSING)
    if isinstance(exc, PermissionError):
        return DiagnosisReport(Diagnosis.PERMISSION_DENIED)
    return DiagnosisReport(Diagnosis.EXIT_UNKNOWN)


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


def _tracker_archive(copied: Path) -> BinaryIO:
    """The copy as a streamed tar archive, positioned at its first byte.

    The isolation image gets this on stdin and unpacks it itself, so no host
    path goes to the daemon: with ``PrivateTmp=true`` the server's ``/tmp`` is a
    different view from the daemon's, and a bind mount of the copy would point
    at nothing. Streaming keeps the read bounded - the copy is already checked
    against :data:`MAX_TRACKER_COPY_BYTES` - and never builds the archive in
    memory.

    Only what :func:`_tracker_copy` admitted goes in: relative names under the
    copy, no link and no absolute path. Modes are owner-only (0700 directories,
    0600 files) and owners are reset, so the unpacked tree belongs to whatever
    user the image runs as.
    """
    stream = tempfile.TemporaryFile(prefix="grid-map-archive-")
    try:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for parent, dirs, files in os.walk(copied):
                dirs.sort()
                for name in dirs + sorted(files):
                    path = Path(parent) / name
                    arcname = "/".join(path.relative_to(copied).parts)
                    info = archive.gettarinfo(str(path), arcname=arcname)
                    if not info.isdir() and not info.isreg():
                        # _tracker_copy refuses links already; never ship else.
                        raise OSError(f"refusing to archive {name}: not a plain file")
                    info.uid, info.gid = 0, 0
                    info.uname = info.gname = ""
                    info.mode = 0o700 if info.isdir() else 0o600
                    if info.isreg():
                        with open(path, "rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


def _answer(result: Any, trace: Optional[ReadTrace] = None,
            runner_kind: Optional[str] = None) -> dict[str, Any]:
    """bd's exit as a typed answer; the real failure goes to the log, not out.

    A non-zero exit gains a safe :class:`DiagnosisReport`, classified from the
    run's own result. Its sentence is allowlisted; bd's stderr is only logged.
    """
    if trace is not None:
        trace.record(None, returncode=result.returncode, stderr=result.stderr or "")
    if result.returncode:
        # bd errors may contain absolute paths or remote credentials.
        logger.warning("map: bd exited %s: %s", result.returncode, _logged(result.stderr))
        diagnosis = _diagnose(result, runner_kind)
        if trace is not None:
            trace.reason = ReasonCode.BD_FAILED
            trace.diagnosis = diagnosis
        return _failure(ReasonCode.BD_FAILED, diagnosis)
    try:
        return {"success": True, "data": json.loads(result.stdout)}
    except ValueError:
        logger.warning("map: bd answered with what is not JSON: %s", _logged(result.stdout))
        if trace is not None:
            trace.reason = ReasonCode.INVALID_ANSWER
        return _failure(ReasonCode.INVALID_ANSWER)


def _legacy_sqlite_answer(answer: dict[str, Any], database: Path,
                          trace: Optional[ReadTrace]) -> dict[str, Any]:
    """Read only a rejected legacy SQLite *copy*, never migrate the workspace.

    bd 1.3 explicitly rejects historical SQLite even for readonly list. Only
    that specific refusal enables this adapter, not an arbitrary bd failure.
    Unknown schemas fail closed. No descriptions, notes or extension code are
    loaded, and the existing project-prefix check still validates every id.
    """
    diagnosis = answer.get("diagnosis")
    if (database.name != "beads.db" or not isinstance(diagnosis, DiagnosisReport)
            or diagnosis.code is not Diagnosis.LEGACY_SQLITE):
        return answer
    fields = ("id", "title", "status", "priority", "issue_type", "assignee",
              "created_at", "updated_at", "close_reason")
    deadline = time.monotonic() + BD_TIMEOUT_SECONDS
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                                     timeout=1)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            tables = {row[0]: row[1] for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name IN ('issues', 'dependencies')"
            )}
            if ("issues" not in tables or
                    any(not str(sql).lstrip().upper().startswith("CREATE TABLE") for sql in tables.values())):
                return answer
            columns = {row[1] for row in connection.execute("PRAGMA table_info(issues)")}
            if not {"id", "title", "status"} <= columns:
                return answer
            selected = [field for field in fields if field in columns]
            order = '"created_at", "id"' if "created_at" in columns else '"id"'
            rows = connection.execute(
                'SELECT ' + ', '.join(f'"{field}"' for field in selected)
                + f' FROM issues ORDER BY {order} LIMIT ?', (MAX_TRACKER_COPY_ENTRIES + 1,)
            ).fetchall()
            if len(rows) > MAX_TRACKER_COPY_ENTRIES:
                return _failure(ReasonCode.TRACKER_TOO_LARGE)
            issues = [dict(zip(selected, row)) for row in rows]
            if (any(not all(value is None or isinstance(value, (str, int)) for value in issue.values())
                    or not all(isinstance(issue[key], str) for key in ("id", "title", "status"))
                    for issue in issues)
                    or len({issue["id"] for issue in issues}) != len(issues)):
                return answer
            if "dependencies" in tables:
                dep_columns = {row[1] for row in connection.execute("PRAGMA table_info(dependencies)")}
                if not {"issue_id", "depends_on_id", "type"} <= dep_columns:
                    return answer
                deps = connection.execute(
                    'SELECT issue_id, depends_on_id, type FROM dependencies LIMIT ?',
                    (MAX_TRACKER_COPY_ENTRIES + 1,),
                ).fetchall()
                if len(deps) > MAX_TRACKER_COPY_ENTRIES:
                    return _failure(ReasonCode.TRACKER_TOO_LARGE)
                by_id = {issue["id"]: issue for issue in issues}
                for issue_id, depends_on_id, kind in deps:
                    if not all(isinstance(value, str) for value in (issue_id, depends_on_id, kind)):
                        return answer
                    if issue_id in by_id:
                        by_id[issue_id].setdefault("dependencies", []).append(
                            {"depends_on_id": depends_on_id, "type": kind})
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return answer
    if trace is not None:
        # Keep the subprocess exit/stderr as evidence; the adapter resolved it.
        trace.record(None, diagnosis=None)
    return {"success": True, "data": issues}


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
        diagnosis = _os_error_diagnosis(exc)
        if trace is not None:
            trace.record(ReasonCode.BD_UNAVAILABLE, stderr=str(exc), diagnosis=diagnosis)
        return _failure(ReasonCode.BD_UNAVAILABLE, diagnosis)
    answer = _answer(result, trace, "host")
    return _legacy_sqlite_answer(answer, database, trace) if tuple(args) == BD_LIST_ARGS else answer


def _run_in_container(image: str, cwd: Path, database: Path, archive: BinaryIO,
                      args: List[str], trace: Optional[ReadTrace] = None) -> dict[str, Any]:
    """One pinned read in the isolation image, when this host has no bd.

    The command mounts nothing. The admitted copy arrives on stdin as the tar
    archive of :func:`_tracker_archive`; :data:`CONTAINER_UNPACK_SCRIPT` unpacks
    it into the container's own writable temporary directory, narrows the modes
    and execs the pinned bd. So a host ``/tmp`` that the Docker daemon cannot
    see (``PrivateTmp=true``) cannot break the read, and the project is never
    mounted at all.
    """
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
        image,
        # Stopping the client alone would leave bd running, holding the database.
        "timeout", "-k", "5", str(int(BD_TIMEOUT_SECONDS)),
        "/bin/sh", "-c", CONTAINER_UNPACK_SCRIPT, "grid-map", str(pinned), *args,
    ]
    if trace is not None:
        trace.command = command
    try:
        logger.debug("map: reading the tracker via docker: %s", " ".join(command))
        result = subprocess.run(command, stdin=archive, capture_output=True,
                                text=True, encoding="utf-8",
                                check=False, timeout=BD_TIMEOUT_SECONDS + 10)
    except subprocess.TimeoutExpired:
        logger.warning("map: bd in the isolation image did not answer in %ss", BD_TIMEOUT_SECONDS)
        if trace is not None:
            trace.reason = ReasonCode.TIMEOUT
        return _failure(ReasonCode.TIMEOUT)
    except OSError as exc:
        logger.warning("map: docker could not run: %s", exc)
        diagnosis = _os_error_diagnosis(exc)
        if trace is not None:
            trace.record(ReasonCode.BD_UNAVAILABLE, stderr=str(exc), diagnosis=diagnosis)
        return _failure(ReasonCode.BD_UNAVAILABLE, diagnosis)
    answer = _answer(result, trace, "docker")
    return _legacy_sqlite_answer(answer, database, trace) if tuple(args) == BD_LIST_ARGS else answer


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
            # The copy goes in as a stream on stdin, never as a host mount: the
            # daemon sees no path of this server, so PrivateTmp cannot hide it.
            with _tracker_archive(copied) as archive:
                return _run_in_container(image, copied, pinned, archive, args, trace)
    except OSError as exc:
        logger.warning("map: the tracker read of %s could not start: %s", directory.name, exc)
        diagnosis = _os_error_diagnosis(exc)
        if trace is not None:
            trace.record(ReasonCode.BD_FAILED, stderr=str(exc), diagnosis=diagnosis)
        return _failure(ReasonCode.BD_FAILED, diagnosis)


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
    diagnosis: Optional[DiagnosisReport] = None


def _no_tasks(code: ReasonCode,
              diagnosis: Optional[DiagnosisReport] = None) -> dict[str, Any]:
    return {"available": False, "reason": REASON_TEXT[code], "reason_code": code.value,
            "diagnosis": diagnosis.payload() if diagnosis is not None else None,
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
        diagnosis = answer.get("diagnosis")
        if not isinstance(diagnosis, DiagnosisReport):
            diagnosis = None
        if trace is not None:
            trace.reason = code
            trace.diagnosis = diagnosis
        return TasksSlice(_no_tasks(code, diagnosis), code, diagnosis)
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
        "diagnosis": None,
        "items": items[:TASKS_LIMIT],
        "total": len(items),
        "truncated": len(items) > TASKS_LIMIT,
        # Over every task of the tracker, not over the shown items.
        "counts": _counts(mapped),
    })


def _chats(space: Any, project_id: str) -> dict[str, Any]:
    """The chats of the project: which work now, which wait for the user.

    ``conversation_views`` gives every chat of the space (its metadata copied,
    its message list a shallow copy of references); the title is the user's,
    or the first words of the chat's first user message, as the rail shows it.
    Each chat also carries the last steps of its work and the typed links to
    the tasks of this project's plan (:func:`_activity`): what the chat did,
    never what a tool was given or returned.
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
        _, _, links, links_total, link_counts = _activity(
            space, chat_id, project_id, running=running
        )
        items.append(
            {
                "chat_id": chat_id,
                "title": title,
                "updated_at": view.get("updated_at") or metadata.get("updated_at"),
                "active": running,
                "awaiting_approval": bool(awaiting.get(chat_id)),
                "task_links": links,
                "task_links_total": links_total,
                "task_link_counts": link_counts,
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
        "items": items[:CHATS_LIMIT],
        "total": len(items),
        "truncated": len(items) > CHATS_LIMIT,
        "counts": {
            "active": sum(item["active"] for item in items),
            "awaiting_approval": sum(item["awaiting_approval"] for item in items),
        },
    }


def _step_titles(steps: Any) -> List[str]:
    """The last :data:`STEPS_LIMIT` titles of *steps*, normalized and bounded.

    A step of a live trace is a dict and one of a turn's ``recent_steps`` a
    string; only the title leaves this function - never a subtitle, a body or
    a payload - with its whitespace collapsed and cut to
    :data:`STEP_TITLE_LIMIT` characters.
    """
    titles: List[str] = []
    for entry in steps or []:
        title = entry.get("title") if isinstance(entry, dict) else entry
        if not isinstance(title, str):
            continue
        text = " ".join(title.split())
        if not text:
            continue
        titles.append(text[:STEP_TITLE_LIMIT])
    return titles[-STEPS_LIMIT:]


def _stored_trace(messages: List[Any]) -> List[Any]:
    """The newest nonempty stored trace of the conversation, or an empty list."""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") or {}
        trace = metadata.get("trace") or normalize_steps(metadata.get("trace_events")) or []
        if isinstance(trace, list) and trace:
            return trace
    return []


def _no_link_counts() -> dict[str, int]:
    """No task of this project's plan is known to the chat."""
    return {TURN_RELATION: 0, PLAN_RELATION: 0}


def _task_links(
    plan: Optional[dict[str, Any]], project_id: str
) -> tuple[List[dict[str, str]], int, dict[str, int]]:
    """The plan's tasks of this project as typed links; the counts before the bound.

    Only a task id that carries the project's own prefix is a link: a plan that
    names a task of another project (or a free-form id) never reaches the map.
    The relation says whether this turn touched the task, never what the task
    says - no title, description or note of a task leaves here.

    The third answer counts the unique valid task ids of each relation over the
    whole plan, before the :data:`TASK_LINKS_LIMIT` bound: a link is counted
    once, under its stronger relation, whatever the bound shows.
    """
    prefix = f"{project_id}-"
    by_id: Dict[str, dict[str, str]] = {}
    for task in (plan or {}).get("tasks") or []:
        task_id = task.get("id") if isinstance(task, dict) else None
        if not isinstance(task_id, str) or not task_id.startswith(prefix) or len(task_id) <= len(prefix):
            continue
        relation = TURN_RELATION if task.get("turn") is True else PLAN_RELATION
        current = by_id.get(task_id)
        if current is None or relation == TURN_RELATION:
            by_id[task_id] = {
                "task_id": task_id,
                "relation": relation,
            }
    links = list(by_id.values())
    counts = _no_link_counts()
    for link in links:
        counts[link["relation"]] += 1
    return links[:TASK_LINKS_LIMIT], len(links), counts


def _reported_activity(value: Any) -> dict[str, Any]:
    """The live activity a turn reports, with every item re-normalized here.

    The turn's ``recent_activity`` already projects its trace; the map checks
    the shape again so an unknown turn can never widen the contract - its
    contexts and each item's ``context_id`` included. The total is the turn's
    own count before its bound, never smaller than what is shown.
    """
    if isinstance(value, dict):
        return public_activity(value)
    return public_activity(value if isinstance(value, list) else [])


def _live_activity(
    turn: Any, project_id: str
) -> tuple[dict[str, Any], List[str], List[dict[str, str]], int, dict[str, int]]:
    """What a running turn did so far, read through its public methods only."""
    activity = getattr(turn, "recent_activity", None)
    shown = _reported_activity(activity() if callable(activity) else None)
    recent = getattr(turn, "recent_steps", None)
    steps = _step_titles(recent() if callable(recent) else [])
    snapshot = getattr(turn, "plan_snapshot", None)
    links, total, counts = _task_links(snapshot() if callable(snapshot) else None, project_id)
    return shown, steps, links, total, counts


def _stored_activity(
    manager: Any, chat_id: str, project_id: str
) -> tuple[dict[str, Any], List[str], List[dict[str, str]], int, dict[str, int]]:
    """What an idle chat's newest stored turn and plan hold, from one read."""
    snapshot = manager.get_context_messages(chat_id, limit=40)
    messages = list((snapshot or {}).get("messages") or [])
    trace = _stored_trace(messages)
    links, total, counts = _task_links(latest_plan(messages), project_id)
    return public_activity(trace), _step_titles(trace), links, total, counts


def _activity(
    space: Any, chat_id: str, project_id: str, *, running: bool
) -> tuple[dict[str, Any], List[str], List[dict[str, str]], int, dict[str, int]]:
    """A chat's latest steps and task links; none of them worth a failed map.

    A running chat is read from its live turn (``recent_steps`` and the live
    plan snapshot of its board) - the live plan *replaces* the stored one, so
    the turn flags of two different turns are never mixed. An idle chat is
    read from its newest stored trace and the plan its messages left. Any
    trouble reading either is logged and answered as "no activity": the map
    of a project must not fail because one chat's history could not be read.
    """
    try:
        if running:
            turns = getattr(space, "turns", None)
            active = turns.get(chat_id) if turns is not None else None
            if active is not None:
                return _live_activity(active[0], project_id)
        manager = getattr(space, "context_manager", None)
        if callable(manager):
            return _stored_activity(manager(), chat_id, project_id)
    except Exception:  # noqa: BLE001 - activity is a convenience, never a failure
        logger.debug("map: no activity for a chat of %s", project_id, exc_info=True)
    return public_activity([]), [], [], 0, _no_link_counts()


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


def _detail_text(value: Any, limit: int) -> str:
    """*value* as one bounded line, or "" when it is not text."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _detail_id(value: Any) -> Optional[str]:
    """*value* as a safe id, or None: the allowlist every id of a detail meets."""
    text = value.strip() if isinstance(value, str) else ""
    return text if _DETAIL_ID.fullmatch(text) else None


def _detail_number(value: Any) -> Optional[float]:
    """A finite nonnegative number (never a bool), or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _detail_kind(metadata: dict[str, Any]) -> Optional[str]:
    """The public kind a stored message is tagged with, or None."""
    for candidate in (metadata.get("kind"), metadata.get("type")):
        if isinstance(candidate, str) and candidate in _DETAIL_KINDS:
            return _DETAIL_KINDS[candidate]
    return None


def _detail_message(entry: Any, position: int) -> Optional[dict[str, Any]]:
    """One stored message as the detail may show it, or None.

    Only the role, the moment, the tagged kind, the already-sanitized preview
    and whether an image is attached are read; the message's own metadata - a
    raw trace, an agent input - never leaves here as such.
    """
    if not isinstance(entry, dict):
        return None
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
    role = entry.get("role")
    known_role = role if role in _DETAIL_ROLES else None
    item: dict[str, Any] = {
        "id": _detail_id(metadata.get("message_id")) or f"message:{position}",
        "role": known_role or "other",
        "time": _detail_text(entry.get("timestamp"), DETAIL_TIME_LIMIT) or None,
        "kind": None,
        "preview": "",
        "has_image": False,
    }
    if known_role is None:  # a message this contract does not know: no content
        return item
    item["kind"] = _detail_kind(metadata)
    item["preview"] = _detail_text(entry.get("preview"), DETAIL_PREVIEW_LIMIT)
    item["has_image"] = bool(entry.get("has_image"))
    turn_id = _detail_id(metadata.get("turn_id"))
    if turn_id is not None:
        item["turn_id"] = turn_id
    return item


def _detail_section(items: List[dict[str, Any]], total: int, cap: int) -> dict[str, Any]:
    """The newest *cap* items of a section, with its honest total."""
    total = max(total, len(items))
    shown = items[-cap:] if cap >= 0 else list(items)
    return {"items": shown, "total": total, "truncated": total > len(shown)}


def _run_item(entry: dict[str, Any], context_id: str, ordinal: int) -> dict[str, Any]:
    """One stored run as the detail may show it.

    An explicit ``turn_id`` names the run stably; a record stored before turn
    ids existed falls back to its conversation, its ordinal and its start.
    Only a boolean says whether the run failed - never the error's text.
    """
    turn_id = _detail_id(entry.get("turn_id"))
    start = _detail_number(entry.get("start_time"))
    if turn_id is not None:
        run_id = f"turn:{turn_id}"
    else:
        stamp = "%g" % start if start is not None else "na"
        run_id = f"legacy:{context_id}:{ordinal}:{stamp}"
    end = _detail_number(entry.get("end_time"))
    duration = (
        int(round((end - start) * 1000))
        if start is not None and end is not None and end >= start
        else None
    )
    failed = bool(entry.get("has_error"))
    return {
        "id": run_id,
        "turn_id": turn_id,
        "agent": _detail_text(entry.get("agent_name"), DETAIL_AGENT_LIMIT) or "unknown",
        "status": "failed" if failed else ("done" if end is not None else "running"),
        "start_time": start,
        "end_time": end,
        "duration_ms": duration,
        "error": failed,
    }


def _detail_runs(snapshot: dict[str, Any], context_id: str) -> tuple[List[dict[str, Any]], int]:
    """Every stored run of a chat, chronological, and how many the chat holds."""
    entries = snapshot.get("executions") if isinstance(snapshot.get("executions"), list) else []
    total = snapshot.get("count")
    if isinstance(total, bool) or not isinstance(total, int) or total < len(entries):
        total = len(entries)
    offset = total - len(entries)
    runs: List[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        run = _run_item(entry, context_id, offset + index)
        if run["id"] in seen:  # a repeated turn id names one run only once
            continue
        seen.add(run["id"])
        runs.append(run)
    runs.sort(key=lambda run: (run["start_time"] is None, run["start_time"] or 0.0))
    return runs, total


def _trace_steps(metadata: dict[str, Any]) -> List[Any]:
    """The stored trace of a message, in its current or its pre-``Step`` form."""
    trace = metadata.get("trace")
    if isinstance(trace, list):
        return trace
    events = metadata.get("trace_events")
    return normalize_steps(events) if isinstance(events, list) else []


def _public_steps(trace: List[Any]) -> List[tuple[dict[str, Any], Any]]:
    """Each real step of *trace* as its public item and its raw parent id.

    Every step passes through :func:`public_activity` - the same strict
    projector the map's activity uses - so a title, a summary, a file ref or a
    timing here is exactly as bounded and as payload-free as there.
    """
    steps: List[tuple[dict[str, Any], Any]] = []
    for step in trace:
        if not isinstance(step, dict):
            continue
        public = public_activity([step])
        items = public.get("items") or []
        if not items:
            continue
        steps.append((items[-1], step.get("parent_id")))
    return steps


def _message_operations(metadata: dict[str, Any], execution_id: str) -> List[dict[str, Any]]:
    """The operations one assistant message's own trace records."""
    steps = _public_steps(_trace_steps(metadata))
    included = {item["id"] for item, _ in steps}
    operations: List[dict[str, Any]] = []
    seen: set[str] = set()
    for item, raw_parent in steps:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        parent_id = _detail_id(raw_parent)
        operations.append({
            "id": item["id"],
            "execution_id": execution_id,
            # A parent is named only when it is one of the steps shown here.
            "parent_id": parent_id if parent_id in included and parent_id != item["id"] else None,
            "kind": item["kind"],
            "title": item["title"],
            "status": item["status"],
            "at_ms": item["at_ms"],
            "duration_ms": item["duration_ms"],
            "files": item["files"],
            "summary": item["summary"],
        })
    return operations


def _message_run(execution_id: str, turn_id: Optional[str], metadata: dict[str, Any]) -> dict[str, Any]:
    """A run record for an assistant message whose own run is not in the window.

    A legacy trace names no turn; its message is the only honest owner of its
    operations, so the message itself becomes the run (``message:<id>``).
    """
    return {
        "id": execution_id,
        "turn_id": turn_id,
        "agent": _detail_text(metadata.get("agent"), DETAIL_AGENT_LIMIT) or "unknown",
        "status": "done",
        "start_time": None,
        "end_time": None,
        "duration_ms": None,
        "error": False,
    }


def _chat_detail(messages_snapshot: dict[str, Any], executions_snapshot: dict[str, Any],
                 context_id: str) -> dict[str, Any]:
    """The safe messages, runs and operations of one chat, each bounded.

    Operations are read from an assistant message's stored trace only, and are
    tied to a run by that message's own explicit ``turn_id`` - or, when it has
    none, by the message's own fallback run. A run is never guessed from a
    timing or a neighbour.
    """
    runs, runs_total = _detail_runs(executions_snapshot, context_id)
    runs_by_id = {run["id"]: run for run in runs}
    entries = messages_snapshot.get("messages") if isinstance(messages_snapshot.get("messages"), list) else []
    total = messages_snapshot.get("count")
    if isinstance(total, bool) or not isinstance(total, int) or total < len(entries):
        total = len(entries)
    offset = total - len(entries)

    messages: List[dict[str, Any]] = []
    operations: List[dict[str, Any]] = []
    seen_operations: set[str] = set()
    for index, entry in enumerate(entries):
        item = _detail_message(entry, offset + index)
        if item is None:
            continue
        messages.append(item)
        if item["role"] != "assistant" or not isinstance(entry, dict):
            continue
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        if not _trace_steps(metadata):
            continue
        turn_id = _detail_id(metadata.get("turn_id"))
        # Only an explicit turn id joins a message to a run; a legacy message
        # owns its trace through a run of its own, never through a guessed one.
        execution_id = f"turn:{turn_id}" if turn_id is not None else f"message:{item['id']}"
        if execution_id not in runs_by_id:
            runs_by_id[execution_id] = _message_run(execution_id, turn_id, metadata)
            runs.append(runs_by_id[execution_id])
        for operation in _message_operations(metadata, execution_id):
            if operation["id"] in seen_operations:  # one operation id, one row
                continue
            seen_operations.add(operation["id"])
            operations.append(operation)

    operations_total = len(operations)
    shown_operations = operations[-DETAIL_OPERATIONS_LIMIT:]
    kept_operations = {operation["id"] for operation in shown_operations}
    for operation in shown_operations:
        if operation["parent_id"] is not None and operation["parent_id"] not in kept_operations:
            operation["parent_id"] = None

    runs.sort(key=lambda run: (run["start_time"] is None, run["start_time"] or 0.0))
    shown_runs = runs[-DETAIL_EXECUTIONS_LIMIT:]
    shown_run_ids = {run["id"] for run in shown_runs}
    # Every operation names a run the answer holds, even one older than the window.
    for operation in shown_operations:
        run = runs_by_id.get(operation["execution_id"])
        if run is not None and run["id"] not in shown_run_ids:
            shown_runs.append(run)
            shown_run_ids.add(run["id"])
    shown_runs.sort(key=lambda run: (run["start_time"] is None, run["start_time"] or 0.0))
    runs_total = max(runs_total, len(runs))

    return {
        "messages": _detail_section(messages, total, DETAIL_MESSAGES_LIMIT),
        "executions": {
            "items": shown_runs,
            "total": runs_total,
            "truncated": runs_total > len(shown_runs),
        },
        "operations": {
            "items": shown_operations,
            "total": operations_total,
            "truncated": operations_total > len(shown_operations),
        },
    }


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
        chats = _chats(space, project_id)
        payload = {
            "project": _project_payload(project),
            "path": _ancestors(store, project),
            "children": _children(store, project_id),
            "server": _public_build(read_build()),
            "tasks": tasks.payload,
            "chats": chats,
            "files": files,
            "files_url": _files_url(space, project_id, [item["chat_id"] for item in chats["items"]]),
        }
        body = _canonical(payload)
        etag = _etag_of(body)
        # One user's map: no shared cache keeps it, and every use revalidates.
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if _matches(if_none_match, etag):
            return Response(status_code=304, headers=headers)
        return Response(content=body, media_type="application/json", headers=headers)

    @api.get("/api/projects/{project_id}/map/chats/{chat_id}")
    async def project_chat_detail(
        project_id: str,
        chat_id: str,
        space: Any = Depends(current_space),
        if_none_match: Optional[str] = Header(default=None, alias="If-None-Match"),
    ) -> Response:
        """One chat of a project: its messages, its runs and their operations.

        The map stays one lightweight read; this detail is asked for only when
        a chat is selected. A chat of another project - or of the user's other
        space - is not found at all. Nothing raw is answered: a message is a
        role, a moment, a tagged kind and a sanitized preview, a run carries no
        error text, and an operation is a bounded, payload-free trace step.
        """
        if not SPACE_ID.fullmatch(project_id) or not space.project_exists(project_id):
            raise HTTPException(status_code=404, detail="Project not found")
        manager = getattr(space, "context_manager", None)
        if manager is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        manager = manager()
        try:
            views = manager.conversation_views()
        except Exception:  # noqa: BLE001 - an unreadable chat is not shown
            logger.warning("map: the chats of %s could not be listed", project_id)
            raise HTTPException(status_code=404, detail="Chat not found")
        view = next(
            (
                candidate for candidate in views or []
                if isinstance(candidate, dict)
                and candidate.get("id") == chat_id
                and (candidate.get("metadata") or {}).get("project_id") == project_id
            ),
            None,
        )
        if view is None:  # a foreign, deleted or misnamed chat is never read
            raise HTTPException(status_code=404, detail="Chat not found")
        messages = manager.get_context_messages(
            chat_id,
            limit=DETAIL_MESSAGES_LIMIT,
            preview_limit=DETAIL_PREVIEW_LIMIT,
            include_full=False,
        )
        executions = manager.get_context_executions(
            chat_id,
            limit=DETAIL_EXECUTIONS_LIMIT,
            preview_limit=0,
            include_full=False,
        )
        if not isinstance(messages, dict) or not isinstance(executions, dict):
            raise HTTPException(status_code=404, detail="Chat not found")
        try:
            title = conversation_title(view)
        except Exception:  # noqa: BLE001 - the title is never worth a failed detail
            title = (view.get("metadata") or {}).get("title") or chat_id
        payload = {
            "chat": {
                "id": chat_id,
                "title": title,
                "href": f"/?conversation={quote(str(chat_id), safe='')}",
            },
            **_chat_detail(messages, executions, chat_id),
        }
        body = _canonical(payload)
        etag = _etag_of(body)
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if _matches(if_none_match, etag):
            return Response(status_code=304, headers=headers)
        return Response(content=body, media_type="application/json", headers=headers)
