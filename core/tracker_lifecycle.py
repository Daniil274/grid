"""A durable binding between a confirmed tracker claim and the run that made it.

A task claimed by an executor is owned by a *run*: one ``run_agent_object``, one
sub-agent call of the ``Agent`` tool, or one top-level turn. The tracker's own
``assignee`` names the person (``grid:<user>``), so it cannot tell two runs of
that person apart; and bd has no notion of "interrupted" or "needs review"
(its statuses are ``open``/``in_progress``/``blocked``/``closed``/``deferred``,
and inventing another one would be a lie). This module keeps both facts
honestly, in a journal of its own:

* **ownership** - for every claim GRID confirmed, which run holds it, under a
  lease, with checkpoints;
* **how the run actually ended** - written by the runtime from what happened
  (timeout, attempt limit, cancellation, error, the executor's own close), not
  from the model's last message.

What this module guarantees, and what it does not
-------------------------------------------------

* The journal is a SQLite database at
  ``<workspace root>/.grid/tracker-lifecycle/bindings.sqlite3`` - beside the
  project (the same workspace root the operation journal uses), one record per
  task, one transaction per change with ``PRAGMA synchronous=FULL``. A record
  a reader sees survived the crash.
* A binding is owned by a **run id**, never by a user id. A second run cannot
  take a task whose binding is live (state ``claimed``/``running`` and lease
  unexpired): :func:`bind` and :func:`finish` refuse with
  ``tracker_claimed_elsewhere`` / ``tracker_not_owner``. Once the lease has
  expired the binding is stale and a new run may take it over; the takeover is
  recorded (``takeover_from``), and later writes started by the old run are
  refused (``tracker_not_owner``): it can no longer report or close the task.
  A bd command that was already in flight when the lease ran out is **not**
  fenced by this journal; reconcile marker and tracker state before resuming.
* :func:`finish` is **owner-only**, and only an outcome the runtime or the tool
  observed itself reaches it. A run that ends without the executor closing the
  task leaves ``needs_review``; a run stopped by the runtime leaves
  ``interrupted``. Neither is ever recorded as ``completed``: GRID does not
  close a task because a model said it was done.
* It does **not** change bd's status. The binding is GRID's own record; the
  tracker's task keeps a valid bd status (``in_progress`` while claimed, and
  whatever bd itself holds after a confirmed close).
* A run's own :class:`LeaseKeeper` refreshes the lease of every task it holds
  while the run lives, so a long model turn without tracker writes does not
  make a live owner look stale. It is a daemon thread, not a guarantee: a
  SIGKILL stops it, the lease runs out, and the next run takes the task over -
  the takeover is recorded and later finish calls from the killed run are
  refused. GRID does not drive bd's own (node-local) lease; bd's assignee
  guard remains the tracker's own check.
* Coordination is between processes on **one host** that share the workspace
  directory. A journal on a network mount, or a writer that bypasses these
  functions, is outside it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from core import tracker_operations

logger = logging.getLogger(__name__)

#: Where the bindings of one workspace live (see also .grid/tracker-operations).
SUBDIR = "tracker-lifecycle"
DB_FILENAME = "bindings.sqlite3"

RECORD_VERSION = 1

#: How long a caller waits for another process to finish a transaction.
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0

#: How long a claim stays live without a heartbeat. Generous on purpose: a run
#: that works for a while without touching the tracker must not be robbed of
#: its task, and a stale binding is only taken over when another run wants it.
DEFAULT_LEASE_SECONDS = 900.0

#: States of a binding. Only the first two are "live" (owned, lease running).
CLAIMED = "claimed"          # reserved for a run; the claim is being/just confirmed
RUNNING = "running"          # the owning run has started its model
INTERRUPTED = "interrupted"  # the run stopped early (timeout, limit, cancel, error)
NEEDS_REVIEW = "needs_review"  # the run ended; the task was not closed by its executor
COMPLETED = "completed"      # the executor's own close was confirmed

OWNED = (CLAIMED, RUNNING)
TERMINAL = (COMPLETED,)

#: How a run ended, as the runtime observed it (never inferred from text).
OUTCOME_CLAIMED = "claimed"
OUTCOME_AGENT_FINISHED = "agent_finished"
OUTCOME_CLOSED = "closed"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ATTEMPT_LIMIT = "attempt_limit"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_ERROR = "error"
OUTCOME_NOT_STARTED = "not_started"
OUTCOME_CLAIM_FAILED = "claim_failed"
OUTCOME_CLAIM_UNKNOWN = "claim_unknown"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    bead_id          TEXT PRIMARY KEY,
    tracker          TEXT NOT NULL,
    directory        TEXT NOT NULL,
    owner_run        TEXT NOT NULL,
    actor            TEXT NOT NULL,
    agent            TEXT,
    context_id       TEXT,
    parent_run       TEXT,
    state            TEXT NOT NULL,
    outcome          TEXT,
    detail           TEXT,
    takeover_from    TEXT,
    claimed_at       TEXT NOT NULL,
    heartbeat_at     REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    finished_at      TEXT,
    revision         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS bindings_owner ON bindings(owner_run, state);
CREATE TABLE IF NOT EXISTS checkpoints (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    bead_id  TEXT NOT NULL,
    run_id   TEXT NOT NULL,
    at       TEXT NOT NULL,
    phase    TEXT NOT NULL,
    note     TEXT,
    data     TEXT
);
CREATE INDEX IF NOT EXISTS checkpoints_task ON checkpoints(bead_id, id);
"""


class LifecycleError(Exception):
    """A refusal of the task lifecycle, carrying a stable code."""

    def __init__(self, message: str, *, code: str = "tracker_lifecycle", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# -- where the journal lives --------------------------------------------------

def journal_dir(directory: "str | Any") -> Any:
    """The bindings directory of one project (not created by this call).

    Named from the same workspace root as the operation journal
    (``<workspace>/.grid``), so every spelling of one project shares it.
    """
    return tracker_operations.journal_dir(directory).parent / SUBDIR


def journal_path(directory: "str | Any") -> Any:
    return journal_dir(directory) / DB_FILENAME


def _now_iso(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _fsync_directory(path: Any) -> None:
    import os

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


def _ensure(base: Any) -> None:
    if base.is_dir():
        return
    base.mkdir(parents=True, exist_ok=True)
    _fsync_directory(base.parent)
    _fsync_directory(base)


_verified: "set[str]" = set()
_verified_guard = threading.Lock()


def _connect(directory: "str | Any", *, create: bool) -> Optional[sqlite3.Connection]:
    """The bindings database, or None when this project never bound a task.

    A journal that exists but cannot be read is a refusal
    (:class:`LifecycleError`), never silently empty: losing the record of an
    owner is exactly what would let a stale run finish another's task.
    """
    path = journal_path(directory)
    if not create and not path.exists():
        return None
    if create:
        _ensure(path.parent)
    connection = sqlite3.connect(
        str(path), timeout=DEFAULT_LOCK_TIMEOUT_SECONDS, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            f"PRAGMA busy_timeout = {int(DEFAULT_LOCK_TIMEOUT_SECONDS * 1000)}"
        )
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(_SCHEMA)
        with _verified_guard:
            _verified.add(str(path))
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise LifecycleError(
            f"The task binding journal {path} cannot be read as a database ({exc}); "
            f"refusing to decide who owns a task from an unreadable journal.",
            code="tracker_lifecycle_corrupt",
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


def _view(row: sqlite3.Row) -> Dict[str, Any]:
    """One binding as the module's public view."""
    return {
        "v": RECORD_VERSION,
        "bead_id": row["bead_id"],
        "tracker": row["tracker"],
        "directory": row["directory"],
        "owner_run_id": row["owner_run"],
        "actor": row["actor"],
        "agent": row["agent"],
        "context_id": row["context_id"],
        "parent_run_id": row["parent_run"],
        "state": row["state"],
        "outcome": row["outcome"],
        "detail": row["detail"],
        "takeover_from": row["takeover_from"],
        "claimed_at": row["claimed_at"],
        "heartbeat_at": row["heartbeat_at"],
        "lease_expires_at": row["lease_expires_at"],
        "finished_at": row["finished_at"],
        "revision": row["revision"],
    }


def _project_directory(directory: str) -> str:
    """The physical workspace root every spelling of one tracker shares."""
    try:
        return str(tracker_operations.journal_dir(directory).parent.parent)
    except tracker_operations.OperationError as exc:
        raise LifecycleError(str(exc), code=exc.code) from exc


def _tracker_key(directory: str) -> str:
    """The physical tracker this workspace owns, by the gateway's identity."""
    try:
        from core import tracker_gateway

        return tracker_gateway.storage_identity(directory).key
    except LifecycleError:
        raise
    except Exception as exc:  # noqa: BLE001 - an identity failure is a refusal
        raise LifecycleError(
            f"the tracker directory {directory!r} could not be identified ({exc})",
            code="tracker_scope",
        ) from exc


def is_live(binding: Optional[Dict[str, Any]], *, now: Optional[float] = None) -> bool:
    """Whether *binding* is owned and its lease has not expired."""
    if not binding or binding.get("state") not in OWNED:
        return False
    now = time.time() if now is None else now
    return float(binding.get("lease_expires_at") or 0) > now


def state_for_outcome(outcome: str) -> str:
    """The binding state an observed run outcome leaves.

    Only the executor's own confirmed close is ``completed``. A runtime stop is
    ``interrupted`` (recoverable: the run can be continued, the task re-claimed
    after the lease). A run that ended on its own without closing its task is
    ``needs_review`` - GRID will not decide from its text that the work is done.
    """
    if outcome == OUTCOME_CLOSED:
        return COMPLETED
    if outcome in (
        OUTCOME_TIMEOUT,
        OUTCOME_ATTEMPT_LIMIT,
        OUTCOME_CANCELLED,
        OUTCOME_ERROR,
        OUTCOME_NOT_STARTED,
    ):
        return INTERRUPTED
    return NEEDS_REVIEW


# -- writing the binding -------------------------------------------------------

def bind(
    directory: str,
    bead_id: str,
    *,
    run_id: str,
    actor: str,
    agent: Optional[str] = None,
    context_id: Optional[str] = None,
    parent_run_id: Optional[str] = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: Optional[float] = None,
    note: str = "",
) -> Dict[str, Any]:
    """Reserve *bead_id* for *run_id*; the claim is confirmed right after.

    Called before the tracker write, so two runs racing for one task settle in
    the journal before either touches bd: the loser is refused here with
    ``tracker_claimed_elsewhere`` (retryable) and makes no write. A stale
    binding (lease expired) is taken over, and the takeover is recorded.

    Raises :class:`LifecycleError` for a live foreign owner, for an unusable
    journal, or for a missing run id.
    """
    if not run_id:
        raise LifecycleError(
            "a tracker claim belongs to a run: refusing to bind it to nobody",
            code="tracker_lifecycle_args",
        )
    now = time.time() if now is None else now
    directory = _project_directory(directory)
    tracker = _tracker_key(directory)
    connection = _connect(directory, create=True)
    assert connection is not None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM bindings WHERE bead_id = ?", (bead_id,)
        ).fetchone()
        takeover_from = None
        if row is not None:
            current = _view(row)
            if row["owner_run"] == run_id:
                if current["state"] in TERMINAL:
                    connection.execute("COMMIT")
                    return current
                connection.execute(
                    "UPDATE bindings SET state = ?, heartbeat_at = ?, "
                    "lease_expires_at = ?, revision = revision + 1 WHERE bead_id = ?",
                    (CLAIMED, now, now + lease_seconds, bead_id),
                )
                _checkpoint(connection, bead_id, run_id, "claimed", note or "resumed by its owner", now)
                connection.execute("COMMIT")
                return lookup(directory, bead_id)  # type: ignore[return-value]
            if is_live(current, now=now):
                connection.execute("ROLLBACK")
                raise LifecycleError(
                    f"{bead_id} is held by run {current['owner_run_id']} "
                    f"(state {current['state']}, lease until "
                    f"{_now_iso(current['lease_expires_at'])}); not taking it from a live run",
                    code="tracker_claimed_elsewhere",
                    retryable=True,
                )
            takeover_from = f"{current['owner_run_id']}@{current['state']}"
            connection.execute(
                "UPDATE bindings SET owner_run = ?, actor = ?, agent = ?, context_id = ?, "
                "parent_run = ?, state = ?, outcome = NULL, detail = NULL, "
                "takeover_from = ?, claimed_at = ?, heartbeat_at = ?, "
                "lease_expires_at = ?, finished_at = NULL, revision = revision + 1 "
                "WHERE bead_id = ?",
                (
                    run_id, actor, agent, context_id, parent_run_id, CLAIMED,
                    takeover_from, _now_iso(now), now, now + lease_seconds, bead_id,
                ),
            )
        else:
            connection.execute(
                "INSERT INTO bindings (bead_id, tracker, directory, owner_run, actor, "
                "agent, context_id, parent_run, state, claimed_at, heartbeat_at, "
                "lease_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bead_id, tracker, directory, run_id, actor, agent, context_id,
                    parent_run_id, CLAIMED, _now_iso(now), now, now + lease_seconds,
                ),
            )
        detail = note or (f"taken over from {takeover_from}" if takeover_from else "claim reserved")
        _checkpoint(connection, bead_id, run_id, "claimed", detail, now, {"takeover_from": takeover_from})
        connection.execute("COMMIT")
    except LifecycleError:
        raise
    except sqlite3.DatabaseError as exc:
        raise LifecycleError(
            f"the task binding journal could not record the claim of {bead_id} ({exc})",
            code="tracker_lifecycle_write",
        ) from exc
    finally:
        connection.close()
    return lookup(directory, bead_id)  # type: ignore[return-value]


def start(
    directory: str,
    bead_id: str,
    run_id: str,
    *,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """Mark the owning run as started (it passed preparation)."""
    return heartbeat(directory, bead_id, run_id, lease_seconds=lease_seconds, now=now, phase="started")


def heartbeat(
    directory: str,
    bead_id: str,
    run_id: str,
    *,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: Optional[float] = None,
    phase: Optional[str] = None,
    note: str = "",
) -> bool:
    """Refresh the lease of a task *run_id* owns; False when it owns nothing.

    It never raises for a missing binding or a foreign owner: a heartbeat is a
    courtesy, not a write, and the owner's actual finish is checked on its own.
    """
    now = time.time() if now is None else now
    try:
        directory = _project_directory(directory)
        connection = _connect(directory, create=True)
    except LifecycleError:
        logger.warning("could not open the task binding journal for a heartbeat", exc_info=True)
        return False
    assert connection is not None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner_run, state FROM bindings WHERE bead_id = ?", (bead_id,)
        ).fetchone()
        if row is None or row["owner_run"] != run_id or row["state"] in TERMINAL:
            connection.execute("ROLLBACK")
            return False
        connection.execute(
            "UPDATE bindings SET state = ?, heartbeat_at = ?, lease_expires_at = ?, "
            "revision = revision + 1 WHERE bead_id = ?",
            (RUNNING, now, now + lease_seconds, bead_id),
        )
        if phase:
            _checkpoint(connection, bead_id, run_id, phase, note, now)
        connection.execute("COMMIT")
        return True
    except sqlite3.DatabaseError:
        logger.warning("the task binding of %s could not be refreshed", bead_id, exc_info=True)
        return False
    finally:
        connection.close()


def checkpoint(
    directory: str,
    bead_id: str,
    run_id: str,
    phase: str,
    note: str = "",
    data: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[float] = None,
) -> bool:
    """Append a checkpoint for a task *run_id* owns; False for anyone else.

    A stale run cannot write into the history of another run's task: the
    checkpoint is the record a human or the next run reads to know where the
    work stood.
    """
    now = time.time() if now is None else now
    try:
        directory = _project_directory(directory)
        connection = _connect(directory, create=True)
    except LifecycleError:
        logger.warning("could not open the task binding journal for a checkpoint", exc_info=True)
        return False
    assert connection is not None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner_run, state FROM bindings WHERE bead_id = ?", (bead_id,)
        ).fetchone()
        if row is None or row["owner_run"] != run_id:
            connection.execute("ROLLBACK")
            return False
        _checkpoint(connection, bead_id, run_id, phase, note, now, data)
        connection.execute("COMMIT")
        return True
    except sqlite3.DatabaseError:
        logger.warning("the checkpoint of %s could not be recorded", bead_id, exc_info=True)
        return False
    finally:
        connection.close()


def _checkpoint(
    connection: sqlite3.Connection,
    bead_id: str,
    run_id: str,
    phase: str,
    note: str,
    now: float,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    connection.execute(
        "INSERT INTO checkpoints (bead_id, run_id, at, phase, note, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (bead_id, run_id, _now_iso(now), phase, note or None, _json(data) if data else None),
    )


def release(directory: str, bead_id: str, run_id: str, *, reason: str = "") -> bool:
    """Forget a reservation whose tracker write definitively did not happen.

    Only a binding still in state ``claimed`` and owned by *run_id* is removed:
    a claim that bd refused left no task for anyone to own, and keeping the
    record would make a later run "take over" work that never existed. A
    binding after a *lost* answer is never released here - that claim may
    exist, and the run keeps owning it.
    """
    try:
        directory = _project_directory(directory)
        connection = _connect(directory, create=True)
    except LifecycleError:
        logger.warning("could not open the task binding journal to release a reservation", exc_info=True)
        return False
    assert connection is not None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT owner_run, state FROM bindings WHERE bead_id = ?", (bead_id,)
        ).fetchone()
        if row is None or row["owner_run"] != run_id or row["state"] != CLAIMED:
            connection.execute("ROLLBACK")
            return False
        # Reservations are disposable; their history is not. In particular a
        # short UI action may have taken over a stopped run: releasing that
        # action must not erase the previous owner's recovery checkpoints.
        _checkpoint(
            connection, bead_id, run_id, "released",
            reason or "reservation released", time.time(),
        )
        connection.execute("DELETE FROM bindings WHERE bead_id = ?", (bead_id,))
        connection.execute("COMMIT")
        logger.info("Released the unconfirmed claim of %s by %s (%s)", bead_id, run_id, reason or "no write")
        return True
    except sqlite3.DatabaseError:
        logger.warning("the reservation of %s could not be released", bead_id, exc_info=True)
        return False
    finally:
        connection.close()


# -- ending the run -------------------------------------------------------------

def finish(
    directory: str,
    bead_id: str,
    run_id: str,
    *,
    outcome: str,
    detail: str = "",
    steal_if_stale: bool = False,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Record how the owning run ended, from what the host observed.

    Owner-only: a run whose task was taken over after its lease expired is
    refused with ``tracker_not_owner`` - a report or close started by a stale
    executor is refused (a bd command already in flight is not fenced; see
    the module docstring). ``steal_if_stale`` lets a *confirmed tracker
    write by this run* (a close) adopt a binding nobody live holds, recording
    the takeover; it is never a way around a live owner.

    Returns the resulting binding, or None when the task was never bound.
    """
    now = time.time() if now is None else now
    directory = _project_directory(directory)
    connection = _connect(directory, create=True)
    assert connection is not None
    takeover_from = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM bindings WHERE bead_id = ?", (bead_id,)
        ).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            return None
        current = _view(row)
        if current["owner_run_id"] != run_id:
            if not steal_if_stale or is_live(current, now=now):
                connection.execute("ROLLBACK")
                raise LifecycleError(
                    f"{bead_id} belongs to run {current['owner_run_id']} "
                    f"(state {current['state']}); this run ({run_id}) may not finish it",
                    code="tracker_not_owner",
                    retryable=True,
                )
            takeover_from = f"{current['owner_run_id']}@{current['state']}"
        if current["state"] in TERMINAL:
            connection.execute("COMMIT")
            return current
        state = state_for_outcome(outcome)
        connection.execute(
            "UPDATE bindings SET state = ?, outcome = ?, detail = ?, owner_run = ?, "
            "takeover_from = COALESCE(?, takeover_from), finished_at = ?, "
            "heartbeat_at = ?, lease_expires_at = ?, revision = revision + 1 "
            "WHERE bead_id = ?",
            (
                state, outcome, detail or None, run_id, takeover_from,
                _now_iso(now), now, now, bead_id,
            ),
        )
        _checkpoint(
            connection, bead_id, run_id, "finished", detail or outcome, now,
            {"outcome": outcome, "state": state, "takeover_from": takeover_from},
        )
        connection.execute("COMMIT")
    except LifecycleError:
        raise
    except sqlite3.DatabaseError as exc:
        raise LifecycleError(
            f"the ending of {bead_id} could not be recorded ({exc})",
            code="tracker_lifecycle_write",
        ) from exc
    finally:
        connection.close()
    return lookup(directory, bead_id)


def settle_bindings(
    bindings: "Any",
    *,
    run_id: Optional[str],
    outcome: str,
    detail: str = "",
) -> List[Dict[str, Any]]:
    """Finish every binding a run made, from the run's actual outcome.

    The runtime calls this in its ``finally``: whatever ended the run - a
    normal answer, a timeout, the turn limit, a cancellation, an exception -
    the tasks it held are left with that outcome instead of staying "running"
    forever. It never raises: a runtime that is already failing must not fail
    harder, and the journal it cannot write is what a human would read.
    """
    written: List[Dict[str, Any]] = []
    for entry in list(bindings or []):
        if not isinstance(entry, dict):
            continue
        directory = entry.get("directory")
        bead_id = entry.get("bead_id")
        if not directory or not bead_id or not run_id:
            continue
        try:
            result = finish(
                directory, bead_id, str(run_id), outcome=outcome, detail=detail
            )
            if result:
                written.append(result)
        except LifecycleError as exc:
            logger.warning(
                "task %s of run %s kept by another owner: %s", bead_id, run_id, exc
            )
        except Exception:  # noqa: BLE001 - a failing journal must not break the run
            logger.warning("could not record the end of task %s", bead_id, exc_info=True)
    return written


def settle_run(run_context: Any, *, outcome: str, detail: str = "") -> List[Dict[str, Any]]:
    """Finish the bindings carried by a run context (see :func:`settle_bindings`)."""
    if run_context is None:
        return []
    return settle_bindings(
        getattr(run_context, "task_bindings", None),
        run_id=getattr(run_context, "run_id", None),
        outcome=outcome,
        detail=detail,
    )


def remember(run_context: Any, directory: str, bead_id: str) -> None:
    """Note on the run that it now owns *bead_id*, so its ``finally`` settles it."""
    bindings = getattr(run_context, "task_bindings", None)
    if bindings is None:
        return
    entry = {"directory": _project_directory(directory), "bead_id": bead_id}
    if entry not in bindings:
        bindings.append(entry)


def refresh_run(
    run_context: Any,
    *,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: Optional[float] = None,
) -> int:
    """Refresh the lease of every task the run context holds; returns how many.

    Both bindings made by the run's model and ones made by its preparation
    (an executor's init_tools) are on the same list, so one call keeps them
    all live. A task taken over by another run is skipped (the heartbeat never
    raises for a foreign owner).
    """
    bindings = getattr(run_context, "task_bindings", None)
    run_id = getattr(run_context, "run_id", None)
    if not bindings or not run_id:
        return 0
    refreshed = 0
    for entry in list(bindings):
        if not isinstance(entry, dict):
            continue
        directory = entry.get("directory")
        bead_id = entry.get("bead_id")
        if not directory or not bead_id:
            continue
        if heartbeat(
            directory, bead_id, str(run_id), lease_seconds=lease_seconds, now=now
        ):
            refreshed += 1
    return refreshed


class LeaseKeeper:
    """A lightweight background heartbeat for one run's task bindings.

    The runtime starts it when a run starts and stops it in the run's
    ``finally``: while the run lives, the tasks it claimed stay live even if
    the model works for hours without touching the tracker. It is a daemon
    thread on purpose - a runtime that is already ending must not wait for a
    journal write - and a SIGKILL cannot reach it, which is why the lease is
    still a real bound: a killed run stops refreshing and becomes stale after
    the lease, so another run may take its task over (recording the takeover).
    It is not a correctness oracle: the owner check on every write and in
    :func:`finish` is what decides, the heartbeat only keeps a live owner live.
    """

    def __init__(
        self,
        run_context: Any,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        interval: Optional[float] = None,
    ) -> None:
        self._run_context = run_context
        self._lease_seconds = float(lease_seconds)
        # A third of the lease, bounded: often enough never to let a live run
        # expire, rare enough to cost nothing (one indexed UPDATE per task).
        self._interval = float(
            interval if interval is not None else max(1.0, min(60.0, self._lease_seconds / 3.0))
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "LeaseKeeper":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._loop,
            name=f"task-lease-{getattr(self._run_context, 'run_id', 'run')}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                refresh_run(self._run_context, lease_seconds=self._lease_seconds)
            except Exception:  # noqa: BLE001 - a heartbeat must never kill the run
                logger.warning("a task lease heartbeat failed", exc_info=True)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(self._interval + 2.0, 5.0))

    def __enter__(self) -> "LeaseKeeper":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()


# -- reading ---------------------------------------------------------------------

def lookup(directory: str, bead_id: str) -> Optional[Dict[str, Any]]:
    """The binding of one task, or None when it was never bound here."""
    connection = _connect(_project_directory(directory), create=False)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT * FROM bindings WHERE bead_id = ?", (bead_id,)
        ).fetchone()
        return _view(row) if row is not None else None
    except sqlite3.DatabaseError as exc:
        raise LifecycleError(
            f"the task binding journal could not be read ({exc})",
            code="tracker_lifecycle_corrupt",
        ) from exc
    finally:
        connection.close()


def bindings_for_run(directory: str, run_id: str) -> List[Dict[str, Any]]:
    """Every task bound to one run, in the order it took them."""
    connection = _connect(_project_directory(directory), create=False)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT * FROM bindings WHERE owner_run = ? ORDER BY claimed_at", (run_id,)
        ).fetchall()
        return [_view(row) for row in rows]
    finally:
        connection.close()


def checkpoints(directory: str, bead_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    """The checkpoint trail of one task, newest last."""
    connection = _connect(_project_directory(directory), create=False)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT * FROM checkpoints WHERE bead_id = ? ORDER BY id DESC LIMIT ?",
            (bead_id, int(limit)),
        ).fetchall()
    finally:
        connection.close()
    entries = [
        {
            "at": row["at"],
            "run_id": row["run_id"],
            "phase": row["phase"],
            "note": row["note"],
            "data": _loads(row["data"]),
        }
        for row in rows
    ]
    entries.reverse()
    return entries
