"""A durable, cross-process journal of the tracker's write operations.

Beads runs as a separate process against an embedded database: GRID asks for a
write and only afterwards learns what became of it. Two gaps follow, and this
module exists to make both honest:

* **A repeat.** The same request arrives twice - a retried tool call, a
  reconnected UI, a caller that did not see the answer. A request id lets the
  second call answer from the journal instead of writing a second plan. A
  request id is bound to the payload it was first used with: the same key with
  different arguments is a conflict, not a repeat.
* **A lost answer.** The process may die, or bd may be stopped on timeout,
  *after* the write reached the database and *before* anyone recorded it.
  Then "nothing was created" is a guess, and repeating the write makes
  duplicates. The honest answer is ``unknown``, and the way out is a marker
  written into the tracker by the same bd transaction as the data: the next
  call looks the marker up and adopts what is already there.

What this module guarantees, and what it does not
-------------------------------------------------

* The journal is a SQLite database at
  ``<project>/.grid/tracker-operations/journal.sqlite3`` - beside the project,
  never in a per-uid temporary directory and never inside ``.beads``. Every
  record is one transaction with ``PRAGMA synchronous=FULL``, so a record that
  a reader sees is a record that survived the crash; there are no half-written
  lines to skip, and an append cannot be glued onto a torn predecessor. The
  directory entry of a newly created journal is fsynced too.
* The journal lives at the *workspace root* the gateway identifies: the parent
  of the ``.beads`` directory bd would open (``identity.beads.parent``), not
  the directory a caller happened to name. Two spellings of one project - a
  symlink, a ``..`` segment - share one journal, and so do a project directory
  and any of its subdirectories, or a ``.beads/redirect`` alias: bd runs both
  against one tracker, so they must share one journal. Two different project
  directories that merely share a ``BEADS_DOLT_DATA_DIR`` do **not**: the
  journal belongs to the project directory, and this module does not look for
  another project's journal.
* A ``started`` record is committed **before** bd runs. A crash leaves it
  there; the next call with the same operation id sees a write that was begun
  and never concluded and refuses to repeat it blindly. A journal that cannot
  be opened or read is a refusal to write, never an empty journal.
* It does **not** make a bd write and a journal record one atomic unit. There
  is a window between bd's commit and the journal's result record. This module
  never closes that window by assumption: it reports ``committed: null``
  (unknown) and leaves the decision to the caller, whose marker lookup is the
  only evidence accepted as proof of a commit. Exactly-once is *not* promised;
  at-most-once per request id, with an honest unknown in between, is. An
  ``unknown`` record is not a dead end: a later lookup may reconcile it to
  ``committed`` when the tracker holds the operation's marker. It never
  becomes ``failed`` - nobody can prove a write that may have happened did
  not.
* Coordination is between processes on **one host** that share the project
  directory. A journal on a network mount, or a writer that bypasses this
  module, is outside it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Where a project keeps GRID's own durable files (see also .grid/memory.sqlite).
DIRNAME = ".grid"
SUBDIR = "tracker-operations"
JOURNAL_FILENAME = "journal.sqlite3"

RECORD_VERSION = 1

#: How long a caller waits for another process to finish a transaction.
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0

#: A caller's own key for a request: conservative, printable, bounded.
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")

#: The prefix of the reference a write leaves in the tracker itself.
MARKER_PREFIX = "grid-op:"

STARTED = "started"
COMMITTED = "committed"
FAILED = "failed"
UNKNOWN = "unknown"
TERMINAL = (COMMITTED, FAILED, UNKNOWN)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    request_id   TEXT,
    fingerprint  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    tracker      TEXT NOT NULL,
    directory    TEXT NOT NULL,
    actor        TEXT,
    executor     TEXT,
    detail       TEXT NOT NULL,
    state        TEXT NOT NULL,
    code         TEXT,
    error        TEXT,
    data         TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

#: The key of a request is the *actor's*: two users of one project may both
#: name their request "retry-1" without one answering for the other's write.
#: The empty string stands in for a caller the host could not name, because
#: SQLite treats NULLs in a unique index as distinct. Written separately from
#: the table so that reading a journal never has to change its schema.
_ACTOR_INDEX = """
DROP INDEX IF EXISTS operations_request;
CREATE UNIQUE INDEX IF NOT EXISTS operations_request_actor
    ON operations(COALESCE(actor, ''), request_id) WHERE request_id IS NOT NULL;
"""


class OperationError(Exception):
    """A refusal of the operation journal, carrying a stable code."""

    def __init__(self, message: str, *, code: str = "tracker_operation", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# -- the marker ---------------------------------------------------------------

def marker(operation_id: str, key: Optional[str] = None) -> str:
    """The reference a write stamps on what it creates.

    bd keeps ``external_ref`` on the issue row, inside the same commit as the
    row itself, so its presence is proof that the write reached the database -
    the one piece of evidence a caller may treat as a commit after a lost
    answer. The plan's task key rides along, so a plan's ids are recovered by
    key rather than guessed from the order bd minted them in.
    """
    return f"{MARKER_PREFIX}{operation_id}" + (f":{key}" if key else "")


def marker_prefix(operation_id: str) -> str:
    """The prefix every marker of one operation starts with (for a lookup)."""
    return f"{MARKER_PREFIX}{operation_id}"


def check_request_id(value: Any) -> Optional[str]:
    """The caller's request id, or None; OperationError when it cannot be a key.

    A request id is optional: an old caller that passes none still gets an
    operation id back and may repeat by it. A value that is not a plain
    identifier is refused before anything is written rather than silently
    replaced, so two requests cannot share a key by accident.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise OperationError(
            f"request_id must be a string, not {type(value).__name__}.",
            code="tracker_request_id",
        )
    text = value.strip()
    if not text:
        return None
    if not REQUEST_ID.match(text):
        raise OperationError(
            "request_id may hold letters, digits and . _ : / - only, "
            "must start with a letter or digit and be at most 120 characters.",
            code="tracker_request_id",
        )
    return text


def _fingerprint(kind: str, detail: Any) -> str:
    """What a request id is bound to: the kind of write and its exact payload."""
    payload = json.dumps(
        {"kind": str(kind), "detail": detail or {}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


# -- where the journal lives --------------------------------------------------

def _identity(directory: "str | Path") -> "Any":
    """The gateway's physical identity of the tracker *directory* names.

    The gateway is the authority on which ``.beads`` bd would open; it is
    imported lazily because this module is also used where no bd is installed.
    There is no ``realpath`` fallback: a caller that cannot be identified is a
    refusal to write, because a journal named from a guess could sit beside a
    different tracker than the one bd opens.
    """
    raw = os.fspath(directory)
    try:
        from core import tracker_gateway

        return tracker_gateway.storage_identity(raw)
    except Exception as exc:  # noqa: BLE001 - any identity failure is a refusal
        raise OperationError(
            f"The tracker directory {raw!r} could not be identified ({exc}); "
            f"refusing to use a journal for a tracker nobody can name.",
            code="tracker_scope",
        ) from exc


def _project_directory(directory: "str | Path") -> Path:
    """The physical workspace root every spelling of one tracker shares.

    ``identity.beads.parent`` - not ``identity.directory``: a caller in a
    subdirectory, or in a directory whose ``.beads`` is a redirect or a
    symlink, names the same tracker bd opens and must share its journal.
    """
    return Path(_identity(directory).beads.parent)


def journal_dir(directory: "str | Path") -> Path:
    """The journal directory of one project (not created by this call)."""
    return _project_directory(directory) / DIRNAME / SUBDIR


def journal_path(directory: "str | Path") -> Path:
    return journal_dir(directory) / JOURNAL_FILENAME


def _tracker_key(directory: str) -> str:
    """The physical tracker this journal belongs to, by the gateway's identity."""
    return _identity(directory).key


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fsync_directory(path: Path) -> None:
    """Make a new directory entry itself durable, where the platform allows it."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:  # pragma: no cover - platform without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - some filesystems refuse
        pass
    finally:
        os.close(fd)


def _migrate(connection: sqlite3.Connection, path: Path) -> None:
    """Give an existing journal the per-actor request index, once per process.

    A journal written before request ids were namespaced carries a global
    unique index; two actors could not both use one key until it is replaced.
    This runs before a *write* only: a reader never changes another project's
    file to read it.
    """
    with _verified_guard:
        if str(path) in _migrated:
            return
    connection.executescript(_ACTOR_INDEX)
    with _verified_guard:
        _migrated.add(str(path))


def _ensure(base: Path) -> None:
    if base.is_dir():
        return
    base.mkdir(parents=True, exist_ok=True)
    # The directory entry itself must be durable, or a reboot can lose the
    # whole journal of a write whose data already reached the tracker.
    _fsync_directory(base.parent)
    _fsync_directory(base)


# Paths whose journal was verified to be a readable database in this process:
# a corrupt file is re-checked, but the common path does not pay for it twice.
_verified: "set[str]" = set()
#: Journals already given the per-actor request index in this process.
_migrated: "set[str]" = set()
_verified_guard = threading.Lock()


def _connect(directory: "str | Path", *, create: bool) -> Optional[sqlite3.Connection]:
    """The journal database, or None when this project never wrote.

    A journal that exists but cannot be opened as a database is a refusal
    (:class:`OperationError`), never silently treated as empty: losing the
    record of a write is exactly what would make a repeat dangerous.
    """
    path = journal_path(directory)
    if not create and not path.exists():
        return None
    if create:
        _ensure(path.parent)
    connection = sqlite3.connect(
        str(path),
        timeout=DEFAULT_LOCK_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(DEFAULT_LOCK_TIMEOUT_SECONDS * 1000)}")
        # FULL makes a committed row durable before the caller continues. The
        # rollback journal (SQLite's default) is kept: switching an existing
        # database to WAL needs a lock of its own and races with a second
        # process opening it at the same moment.
        connection.execute("PRAGMA synchronous = FULL")
        with _verified_guard:
            verified = str(path) in _verified
        if create or not verified:
            connection.executescript(_SCHEMA)
            with _verified_guard:
                _verified.add(str(path))
        else:
            # Still touch the schema: a second process may have created the
            # tables after this one verified an empty database.
            connection.executescript(_SCHEMA)
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise OperationError(
            f"The operation journal {path} cannot be read as a database ({exc}); "
            f"refusing to write without a durable record of the operation.",
            code="tracker_operation_corrupt",
        ) from exc
    return connection


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Optional[str]) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _row_view(row: sqlite3.Row) -> dict:
    """One operation as the module's public view."""
    state = row["state"]
    return {
        "v": RECORD_VERSION,
        "operation_id": row["operation_id"],
        "request_id": row["request_id"],
        "kind": row["kind"],
        "tracker": row["tracker"],
        "directory": row["directory"],
        "actor": row["actor"],
        "executor": row["executor"],
        "detail": _loads(row["detail"]) or {},
        "state": state,
        "committed": {COMMITTED: True, FAILED: False, UNKNOWN: None}.get(state),
        "code": row["code"],
        "error": row["error"],
        "data": _loads(row["data"]),
        "at": row["updated_at"],
    }


# -- one operation ------------------------------------------------------------

@dataclass(frozen=True)
class Operation:
    """One write GRID asked the tracker for, as the journal knows it."""

    operation_id: str
    request_id: Optional[str]
    kind: str
    directory: str
    tracker: str
    state: str
    record: dict
    is_new: bool = True

    @property
    def settled(self) -> bool:
        return self.state in TERMINAL


def lookup(
    directory: "str | Path",
    *,
    request_id: Optional[str] = None,
    operation_id: Optional[str] = None,
    actor: Optional[str] = None,
) -> Optional[dict]:
    """An operation already known here, by request id or by operation id.

    The view holds ``state`` (``started``/``committed``/``failed``/``unknown``),
    ``committed`` (True/False/None), ``code``, ``error`` and ``data``. Reading
    never creates the journal: a project that never wrote has none.

    A request id is namespaced by *actor*: with ``actor`` given only that
    actor's operation is returned, so two users who chose the same key cannot
    read - or answer for - each other's write. ``actor=None`` reads the
    journal as an administrator, across actors; a write tool never does.
    """
    connection = _connect(directory, create=False)
    if connection is None:
        return None
    try:
        if operation_id:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        elif request_id:
            if actor is None:
                row = connection.execute(
                    "SELECT * FROM operations WHERE request_id = ?", (request_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM operations WHERE request_id = ? "
                    "AND COALESCE(actor, '') = COALESCE(?, '')",
                    (request_id, str(actor)),
                ).fetchone()
        else:
            return None
        return _row_view(row) if row is not None else None
    finally:
        connection.close()


def _operation(row: sqlite3.Row, *, is_new: bool) -> Operation:
    view = _row_view(row)
    return Operation(
        operation_id=str(view["operation_id"]),
        request_id=view["request_id"],
        kind=str(view["kind"]),
        directory=str(view["directory"]),
        tracker=str(view["tracker"]),
        state=str(view["state"]),
        record=view,
        is_new=is_new,
    )


def begin(
    directory: "str | Path",
    kind: str,
    *,
    request_id: Optional[str] = None,
    actor: Optional[str] = None,
    executor: Optional[str] = None,
    detail: Optional[dict] = None,
) -> Operation:
    """Start one write, or find the one already begun for this request id.

    The ``started`` row is committed before this returns, so a crash after it
    cannot look like "nothing happened". When *request_id* was already used,
    the existing operation is returned with ``is_new=False`` and the caller
    must replay or reconcile it instead of writing again - including when the
    other process is still running it. A request id reused with a different
    kind or payload is refused (:class:`OperationError`,
    ``tracker_request_conflict``): it cannot be answered as a repeat.

    A request id belongs to the *actor* that chose it: two users of one project
    may both call their request "retry-1" and each gets their own operation,
    while a new run of the same user repeating a key is still the same request.
    ``executor`` is recorded but is not part of the key: idempotency must not
    depend on which run of the user arrives first.
    """
    actor = str(actor) if actor else None
    tracker = _tracker_key(str(directory))
    fingerprint = _fingerprint(kind, detail)
    connection = _connect(directory, create=True)
    if connection is None:  # pragma: no cover - create=True never returns None
        raise OperationError("The operation journal is unavailable.", code="tracker_operation")
    try:
        _migrate(connection, journal_path(directory))
        connection.execute("BEGIN IMMEDIATE")
        try:
            if request_id:
                row = connection.execute(
                    "SELECT * FROM operations WHERE request_id = ? "
                    "AND COALESCE(actor, '') = COALESCE(?, '')",
                    (request_id, actor),
                ).fetchone()
                if row is not None:
                    if row["fingerprint"] != fingerprint:
                        raise OperationError(
                            f"The request id {request_id!r} of this actor was already used for a "
                            f"different request (operation {row['operation_id']}). Use a new "
                            f"request id for a new write.",
                            code="tracker_request_conflict",
                        )
                    connection.execute("COMMIT")
                    return _operation(row, is_new=False)
            operation_id = uuid.uuid4().hex
            now = _now()
            connection.execute(
                "INSERT INTO operations (operation_id, request_id, fingerprint, kind, tracker, "
                "directory, actor, executor, detail, state, code, error, data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                (
                    operation_id,
                    request_id,
                    fingerprint,
                    str(kind),
                    tracker,
                    str(_project_directory(directory)),
                    actor,
                    executor,
                    _json(detail or {}),
                    STARTED,
                    now,
                    now,
                ),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        row = connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        return _operation(row, is_new=True)
    except sqlite3.DatabaseError as exc:
        raise OperationError(
            f"The operation journal could not record the start of the write ({exc}).",
            code="tracker_operation",
        ) from exc
    finally:
        connection.close()


def _conclude(
    operation: Operation,
    state: str,
    *,
    code: str,
    error: Optional[str] = None,
    data: Optional[dict] = None,
) -> dict:
    """Record how an operation ended; the first conclusion wins.

    The only later change allowed is ``unknown`` -> ``committed``: a marker
    found in the tracker proves the write happened, while an unknown that no
    marker answers stays unknown and never turns into a `"did not happen"`.
    """
    if state not in TERMINAL:
        raise OperationError(f"{state!r} is not a conclusion.", code="tracker_operation")
    connection = _connect(operation.directory, create=True)
    if connection is None:  # pragma: no cover - create=True never returns None
        raise OperationError("The operation journal is unavailable.", code="tracker_operation")
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation.operation_id,)
            ).fetchone()
            if row is None:
                raise OperationError(
                    f"The operation {operation.operation_id} is not in the journal.",
                    code="tracker_operation_unknown",
                )
            current = row["state"]
            allowed = current == STARTED or (current == UNKNOWN and state == COMMITTED)
            if allowed:
                connection.execute(
                    "UPDATE operations SET state = ?, code = ?, error = ?, data = ?, updated_at = ? "
                    "WHERE operation_id = ?",
                    (state, code, error, _json(data) if data is not None else None, _now(),
                     operation.operation_id),
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        row = connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation.operation_id,)
        ).fetchone()
        return _row_view(row)
    except sqlite3.DatabaseError as exc:
        raise OperationError(
            f"The operation journal could not record the conclusion ({exc}).",
            code="tracker_operation",
        ) from exc
    finally:
        connection.close()


def commit(operation: Operation, data: Optional[dict] = None, *, code: str = "ok") -> dict:
    """The write is in the tracker: its result is now the answer for its key."""
    return _conclude(operation, COMMITTED, code=code, data=data or {})


def fail(operation: Operation, error: str, *, code: str = "tracker_failed") -> dict:
    """The write certainly did not happen (bd refused, or nothing ever ran)."""
    return _conclude(operation, FAILED, code=code, error=error)


def unknown(operation: Operation, error: str, *, code: str = "tracker_unknown") -> dict:
    """The write may or may not have happened; nobody may repeat it blindly."""
    return _conclude(operation, UNKNOWN, code=code, error=error)
