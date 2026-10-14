"""Transactional durable journal for workflow runs (C2 of the durable-workflows plan).

Design: docs/durable-workflows-plan.md §4–§5, invariants I3–I5, I9.
This module knows nothing about the spec types of C1: runs carry a frozen
``spec_json`` string (dicts in, JSON text stored), and every row is a plain
``dict``. The engine (C3) and reconcile/resume (C4) build on these primitives:

* ``begin_attempt`` commits the attempt row — ``effect_status='unknown'`` for
  effectful steps — *before* any effect runs (write-ahead of effects, I3).
* A step or run is claimed compare-and-swap: a second executor on the same
  step/run fails loudly instead of double-running (lease/owner on runs).
* ``finish_attempt`` updates the attempt, the step row (status, result,
  error) and appends one event in a single transaction.
* An ``unknown`` effect status is never downgraded back to ``none`` by the
  journal (I9), and the journal restarts nothing by itself — resuming an
  unknown effect is a decision for reconcile/resume (C4), never automatic.
* On reopen after a crash every recorded state is still there: durability
  of statuses, attempts, spend and the append-only event log.

SQLite calls are synchronous, WAL, short — the accepted compromise (§11).
One writer at a time: every mutation runs inside ``BEGIN IMMEDIATE`` under
the instance lock; a second journal on the same file serialises through
``busy_timeout``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Run statuses (§5): queued -> running -> terminal; paused_budget/resume;
#: cancelling -> emergency_stopped from any active status.
RUN_STATUSES = (
    "queued", "running", "paused_budget", "cancelling", "interrupted",
    "completed", "failed", "emergency_stopped",
)
#: A run that will never move again.
TERMINAL_RUN_STATUSES = ("completed", "failed", "emergency_stopped")
#: Allowed directed run transitions; anything else raises InvalidTransition.
RUN_TRANSITIONS: Dict[str, frozenset] = {
    "queued": frozenset({"running", "cancelling", "interrupted"}),
    "running": frozenset({"completed", "failed", "interrupted", "paused_budget", "cancelling"}),
    "paused_budget": frozenset({"running", "cancelling", "interrupted"}),
    "cancelling": frozenset({"emergency_stopped", "interrupted"}),
    "interrupted": frozenset({"running"}),  # resume only; never silently
}

#: Step statuses (§5).
STEP_STATUSES = ("pending", "ready", "running", "completed", "failed", "interrupted", "skipped")
#: A step that will never move again.
TERMINAL_STEP_STATUSES = ("completed", "failed", "interrupted", "skipped")
#: Allowed directed step transitions.
STEP_TRANSITIONS: Dict[str, frozenset] = {
    "pending": frozenset({"ready", "skipped"}),
    "ready": frozenset({"running", "skipped"}),
    "running": frozenset({"completed", "failed", "interrupted"}),
    # terminal statuses move nowhere; a restart is a *new* attempt via
    # begin_attempt after reconcile/resume set the step back, never a status jump
}

#: Attempt outcomes.
ATTEMPT_OUTCOMES = ("started", "completed", "failed", "timed_out", "interrupted")
#: Effect statuses (I9): 'unknown' until verified, 'confirmed' only via verify.
EFFECT_STATUSES = ("none", "unknown", "confirmed")
#: Step statuses that begin_attempt may claim (pending/ready normally;
#: interrupted after reconcile/resume restarted the step for a new attempt).
CLAIMABLE_STEP_STATUSES = ("pending", "ready", "interrupted")
#: An attempt outcome maps onto the step status this way.
OUTCOME_TO_STEP_STATUS: Dict[str, str] = {
    "completed": "completed",
    "failed": "failed",
    "timed_out": "failed",
    "interrupted": "interrupted",
}


class JournalError(RuntimeError):
    """Base error of the workflow journal."""


class RunExistsError(JournalError):
    """create_run: the run_id is already taken."""


class NotFoundError(JournalError, LookupError):
    """A run, step or attempt that the journal does not have."""


class InvalidTransition(JournalError):
    """A status move the plan forbids (§5, I4), or a CAS conflict."""


class ClaimConflict(JournalError):
    """A claim refused: lease/owner or an already-open attempt forbids it."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowJournal:
    """SQLite-backed durable journal of workflow runs.

    All writes are transactions (``BEGIN IMMEDIATE``); every mutating call
    also appends its event inside the same transaction, so a reader that saw
    the effect also saw its journal record.
    """

    def __init__(self, db_path: Path | str, *, clock: Callable[[], datetime] = _utcnow) -> None:
        """Open (and migrate/create) the journal at *db_path*.

        *clock* exists for tests; it stamps every row and drives lease expiry.
        """
        self._path = Path(db_path)
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self._path), timeout=5.0, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the connection; the journal keeps its state on disk."""
        with self._lock:
            self._db.close()

    def __enter__(self) -> "WorkflowJournal":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def new_run_id() -> str:
        """:class:`str` — a fresh run id, ``'wf-' + uuid4 hex[:12]``."""
        return "wf-" + uuid.uuid4().hex[:12]

    # -- helpers ------------------------------------------------------------

    def _now(self) -> str:
        return self._clock().isoformat()

    @contextmanager
    def _tx(self):
        """One writer transaction; the instance lock keeps re-entry honest."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    @staticmethod
    def _row(run: sqlite3.Row) -> Dict[str, Any]:
        return dict(run)

    def _event(self, db: sqlite3.Connection, run_id: str, event: str, payload: Any) -> None:
        text = None if payload is None else (
            payload if isinstance(payload, str) else json.dumps(payload, default=str)
        )
        db.execute(
            "INSERT INTO workflow_events (run_id, at, event, payload) VALUES (?,?,?,?)",
            (run_id, self._now(), event, text),
        )

    def _run_locked(self, db: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run not found: {run_id}")
        return row

    def _step_locked(self, db: sqlite3.Connection, run_id: str, step_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM workflow_steps WHERE run_id=? AND step_id=?", (run_id, step_id)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"step not found: {run_id}/{step_id}")
        return row

    def _fence(self, db: sqlite3.Connection, run_id: str, owner: Optional[str]) -> None:
        """Reject a write from an owner that no longer holds the run.

        Fencing is opt-in: ``owner=None`` (the plain journal API and most
        existing callers) is unfenced. When an owner is given, the run's
        current ``owner`` column must equal it — a lease that was taken over
        (or a terminal run that cleared its owner) refuses the stale write.
        """
        if owner is None:
            return
        run = self._run_locked(db, run_id)
        if run["owner"] != owner:
            raise ClaimConflict(
                f"run {run_id} is not owned by {owner!r} (owner={run['owner']!r});"
                " stale owner write refused")

    def _lease_live_row(self, row: sqlite3.Row) -> bool:
        """Whether *row* still holds a live lease (its owner is alive)."""
        lease = row["lease_expires_at"]
        if not lease or row["status"] not in ("running", "cancelling"):
            return False
        try:
            return datetime.fromisoformat(lease) > self._clock()
        except (TypeError, ValueError):
            return False

    # -- runs ----------------------------------------------------------------

    def create_run(
        self,
        *,
        run_id: Optional[str] = None,
        spec: dict | str,
        spec_sha256: str,
        context_id: Optional[str] = None,
        user_id: Optional[str] = None,
        deadline_at: Optional[str] = None,
        queue_deadline_at: Optional[str] = None,
        budget_usd_micros: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Insert a run in ``queued`` with its frozen spec (dict or JSON text).

        Raises RunExistsError for a taken run_id.
        """
        spec_json = spec if isinstance(spec, str) else json.dumps(spec, sort_keys=True)
        if not spec_sha256:
            raise ValueError("spec_sha256 is required")
        if budget_usd_micros is not None and (
            not isinstance(budget_usd_micros, int) or isinstance(budget_usd_micros, bool)
            or budget_usd_micros < 0
        ):
            raise ValueError("budget_usd_micros must be a non-negative integer")
        run_id = run_id or self.new_run_id()
        now = self._now()
        with self._tx() as db:
            try:
                db.execute(
                    "INSERT INTO workflow_runs (run_id, spec_sha256, spec_json, context_id,"
                    " user_id, status, created_at, updated_at, deadline_at, queue_deadline_at,"
                    " budget_usd_micros) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, spec_sha256, spec_json, context_id, user_id, "queued", now, now,
                     deadline_at, queue_deadline_at, budget_usd_micros),
                )
            except sqlite3.IntegrityError as error:
                raise RunExistsError(f"run already exists: {run_id}") from error
            self._event(db, run_id, "run_created", {"spec_sha256": spec_sha256})
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The run row as a dict, or None."""
        row = self._db.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else self._row(row)

    def load_runs(
        self,
        *,
        statuses: Optional[Sequence[str]] = None,
        context_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Scoped, paginated runs, newest first: ``items``, ``total``, ``limit``, ``offset``."""
        clauses: List[str] = []
        args: List[Any] = []
        if statuses:
            bad = [s for s in statuses if s not in RUN_STATUSES]
            if bad:
                raise ValueError(f"unknown run statuses: {bad}")
            clauses.append("status IN (%s)" % ",".join("?" * len(statuses)))
            args.extend(statuses)
        if context_id is not None:
            clauses.append("context_id=?")
            args.append(context_id)
        if user_id is not None:
            clauses.append("user_id=?")
            args.append(user_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self._db.execute(
            "SELECT COUNT(*) FROM workflow_runs " + where, args
        ).fetchone()[0]
        rows = self._db.execute(
            "SELECT * FROM workflow_runs " + where
            + " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return {
            "items": [self._row(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        note: Optional[str] = None,
        expect: Optional[str] = None,
        final_report: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move a run to *status*; atomic with its event.

        *expect* makes the move compare-and-swap: the journal refuses unless
        the run is exactly in that status now (a lost race raises
        InvalidTransition). Without *expect* the move is checked against
        RUN_TRANSITIONS instead (§5, I4). A terminal move clears owner/lease.
        """
        if status not in RUN_STATUSES:
            raise ValueError(f"unknown run status: {status!r}")
        with self._tx() as db:
            row = self._run_locked(db, run_id)
            current = row["status"]
            if expect is not None:
                if current != expect:
                    raise InvalidTransition(
                        f"run {run_id} CAS conflict: expected {expect!r}, found {current!r}")
            elif status != current and status not in RUN_TRANSITIONS.get(current, frozenset()):
                raise InvalidTransition(f"run {run_id}: {current!r} -> {status!r} is forbidden")
            terminal = status in TERMINAL_RUN_STATUSES
            db.execute(
                "UPDATE workflow_runs SET status=?, updated_at=?, final_report=COALESCE(?,"
                " final_report), owner=?, lease_expires_at=? WHERE run_id=?",
                (status, self._now(), final_report,
                 None if terminal else row["owner"],
                 None if terminal else row["lease_expires_at"], run_id),
            )
            self._event(db, run_id, "run_status_changed", {"from": current, "to": status, "note": note})
        return self.get_run(run_id)  # type: ignore[return-value]

    def interrupt_run(
        self,
        run_id: str,
        *,
        expect: Optional[str] = None,
        owner: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reconcile-only: interrupt a run **only if it is not live**, atomically.

        Re-checks the lease inside the transaction, so a run a new owner just
        claimed (or whose owner is still alive) is never touched even if a
        stale read said otherwise (the reconcile race). *expect* is a status
        CAS; *owner* fences the recorded stale owner. On success the run is
        ``interrupted`` with its owner/lease cleared.
        """
        with self._tx() as db:
            row = self._run_locked(db, run_id)
            current = row["status"]
            if expect is not None and current != expect:
                raise InvalidTransition(
                    f"run {run_id} CAS conflict: expected {expect!r}, found {current!r}")
            if owner is not None and row["owner"] not in (None, owner):
                raise ClaimConflict(
                    f"run {run_id} changed owner to {row['owner']!r}; reconcile refused")
            if self._lease_live_row(row):
                raise ClaimConflict(f"run {run_id} is live ({row['owner']!r}); reconcile refused")
            db.execute(
                "UPDATE workflow_runs SET status='interrupted', owner=NULL,"
                " lease_expires_at=NULL, updated_at=? WHERE run_id=?",
                (self._now(), run_id),
            )
            self._event(db, run_id, "run_status_changed",
                        {"from": current, "to": "interrupted", "note": note})
        return self.get_run(run_id)  # type: ignore[return-value]

    # -- claims / leases ------------------------------------------------------

    def claim_run(
        self, run_id: str, owner: str, *, lease_seconds: float = 300.0,
        from_statuses: Iterable[str] = ("queued",),
        allow_same_owner: bool = False,
    ) -> Dict[str, Any]:
        """CAS-claim a run for *owner* (status -> ``running``) with a lease.

        Refused (ClaimConflict) when the run is not in *from_statuses*, when
        another live owner holds it, or when the lease has not expired. An
        expired lease may be taken over. Succeeding claims refresh the lease.

        *allow_same_owner* makes a re-claim by the run's current, live owner
        idempotent: it just refreshes the lease. The API pre-claims a resume
        under the engine's own owner, so the engine's later claim must be a
        no-op rather than a second-execution conflict.
        """
        if not owner:
            raise ValueError("owner is required")
        allowed = tuple(from_statuses)
        with self._tx() as db:
            row = self._run_locked(db, run_id)
            current = row["status"]
            lease = row["lease_expires_at"]
            expired = (
                lease is None
                or datetime.fromisoformat(lease) <= self._clock()
            ) if current == "running" else None
            same_owner = (
                allow_same_owner and current == "running"
                and not expired and row["owner"] == owner
            )
            if not same_owner:
                if current not in allowed and not (current == "running" and expired):
                    raise ClaimConflict(
                        f"run {run_id} is {current!r}; claimable from {list(allowed)}")
                if current == "running" and not expired and row["owner"] != owner:
                    raise ClaimConflict(
                        f"run {run_id} is claimed by {row['owner']!r} until {lease}")
            lease_until = (self._clock() + timedelta(seconds=lease_seconds)).isoformat()
            db.execute(
                "UPDATE workflow_runs SET status='running', owner=?, lease_expires_at=?,"
                " updated_at=? WHERE run_id=?",
                (owner, lease_until, self._now(), run_id),
            )
            self._event(db, run_id, "run_claimed", {
                "owner": owner, "lease_expires_at": lease_until, "reclaim": same_owner,
            })
        return self.get_run(run_id)  # type: ignore[return-value]

    def renew_lease(self, run_id: str, owner: str, *, lease_seconds: float = 300.0) -> Dict[str, Any]:
        """Extend the lease of the run *owner* holds; others get ClaimConflict."""
        with self._tx() as db:
            row = self._run_locked(db, run_id)
            if row["status"] not in ("running", "cancelling") or row["owner"] != owner:
                raise ClaimConflict(
                    f"run {run_id} is not leased to {owner!r}"
                    f" (status={row['status']!r}, owner={row['owner']!r})")
            lease_until = (self._clock() + timedelta(seconds=lease_seconds)).isoformat()
            db.execute(
                "UPDATE workflow_runs SET lease_expires_at=?, updated_at=? WHERE run_id=?",
                (lease_until, self._now(), run_id),
            )
            self._event(db, run_id, "run_lease_renewed", {"owner": owner, "lease_expires_at": lease_until})
        return self.get_run(run_id)  # type: ignore[return-value]

    # -- steps ----------------------------------------------------------------

    def load_steps(self, run_id: str) -> List[Dict[str, Any]]:
        """The run's steps, in insertion order."""
        self._require_run(run_id)
        rows = self._db.execute(
            "SELECT * FROM workflow_steps WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def get_step(self, run_id: str, step_id: str) -> Optional[Dict[str, Any]]:
        """The step row as a dict, or None."""
        row = self._db.execute(
            "SELECT * FROM workflow_steps WHERE run_id=? AND step_id=?", (run_id, step_id)
        ).fetchone()
        return None if row is None else self._row(row)

    def add_step(self, run_id: str, step_id: str) -> Dict[str, Any]:
        """Register a step of the run in ``pending`` (idempotent per run)."""
        with self._tx() as db:
            self._run_locked(db, run_id)
            now = self._now()
            db.execute(
                "INSERT OR IGNORE INTO workflow_steps (run_id, step_id, status, attempts)"
                " VALUES (?,?, 'pending', 0)", (run_id, step_id),
            )
        return self.get_step(run_id, step_id)  # type: ignore[return-value]

    def set_step_status(
        self,
        run_id: str,
        step_id: str,
        status: str,
        *,
        expect: Optional[str] = None,
        result_ref: Optional[str] = None,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move a step to *status*; atomic with its event (CAS via *expect*)."""
        if status not in STEP_STATUSES:
            raise ValueError(f"unknown step status: {status!r}")
        now = self._now()
        with self._tx() as db:
            self._fence(db, run_id, owner)
            row = self._step_locked(db, run_id, step_id)
            current = row["status"]
            if expect is not None:
                if current != expect:
                    raise InvalidTransition(
                        f"step {run_id}/{step_id} CAS conflict:"
                        f" expected {expect!r}, found {current!r}")
            elif status != current and status not in STEP_TRANSITIONS.get(current, frozenset()):
                raise InvalidTransition(
                    f"step {run_id}/{step_id}: {current!r} -> {status!r} is forbidden")
            started = row["started_at"] or (now if status == "running" else None)
            finished = now if status in TERMINAL_STEP_STATUSES else row["finished_at"]
            db.execute(
                "UPDATE workflow_steps SET status=?, result_ref=COALESCE(?, result_ref),"
                " error_code=COALESCE(?, error_code), error_detail=COALESCE(?, error_detail),"
                " started_at=?, finished_at=? WHERE run_id=? AND step_id=?",
                (status, result_ref, error_code, error_detail, started, finished,
                 run_id, step_id),
            )
            self._event(db, run_id, "step_status_changed", {
                "step_id": step_id, "from": current, "to": status,
                "result_ref": result_ref, "error_code": error_code,
            })
        return self.get_step(run_id, step_id)  # type: ignore[return-value]

    def reset_step(
        self, run_id: str, step_id: str, *, to: str = "ready",
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Put a *terminal* step back where ``begin_attempt`` can claim it again.

        A restart is never the journal's own decision (I4): the engine calls
        this for an authorised retry, reconcile/resume for a verified restart.
        'skipped' steps are not resettable (an optional step stayed optional);
        'completed' ones are not either (an effect already stands). The reset
        clears the last result/error and finished_at: the *new* attempt
        writes fresh ones, while every past attempt stays in step_attempts.
        Appends a ``step_reset`` event; a step that is not terminal raises
        InvalidTransition.
        """
        if to not in ("pending", "ready"):
            raise ValueError("a step may be reset to 'pending' or 'ready' only")
        with self._tx() as db:
            self._fence(db, run_id, owner)
            row = self._step_locked(db, run_id, step_id)
            current = row["status"]
            if current not in ("failed", "interrupted"):
                raise InvalidTransition(
                    f"step {run_id}/{step_id} is {current!r};"
                    " only failed or interrupted steps are reset for a new attempt")
            db.execute(
                "UPDATE workflow_steps SET status=?, result_ref=NULL, error_code=NULL,"
                " error_detail=NULL, finished_at=NULL WHERE run_id=? AND step_id=?",
                (to, run_id, step_id),
            )
            self._event(db, run_id, "step_reset", {
                "step_id": step_id, "from": current, "to": to,
            })
        return self.get_step(run_id, step_id)  # type: ignore[return-value]

    # -- attempts (the write-ahead of effects) ---------------------------------

    def begin_attempt(
        self,
        run_id: str,
        step_id: str,
        attempt: Optional[int] = None,
        *,
        idempotency_key: Optional[str] = None,
        effect_status: str = "none",
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically claim the step and commit its attempt row — before any effect.

        One ``BEGIN IMMEDIATE`` transaction moves the step to ``running``
        (from pending/ready, or interrupted after a restart decision), bumps
        ``attempts`` and inserts the ``step_attempts`` row with
        ``outcome='started'``. The caller commits *before* running the tool:
        a crash after this call leaves an open, ``effect_status='unknown'``
        attempt for effectful steps (I3), which the journal never restarts by
        itself. A second executor loses the compare-and-swap and gets
        ClaimConflict — the step cannot be double-run.

        *effect_status* is ``'unknown'`` for write/exec (the caller decides);
        never ``'confirmed'`` at begin (I9). *attempt* defaults to the next
        number; an explicit one lets the caller resume deterministically.
        """
        if effect_status not in ("none", "unknown"):
            raise ValueError("effect_status at begin must be 'none' or 'unknown'")
        now = self._now()
        with self._tx() as db:
            self._fence(db, run_id, owner)
            row = self._step_locked(db, run_id, step_id)
            current = row["status"]
            if current not in CLAIMABLE_STEP_STATUSES:
                raise ClaimConflict(
                    f"step {run_id}/{step_id} is {current!r};"
                    f" claimable from {list(CLAIMABLE_STEP_STATUSES)}")
            open_attempt = db.execute(
                "SELECT attempt FROM step_attempts WHERE run_id=? AND step_id=?"
                " AND outcome='started'", (run_id, step_id),
            ).fetchone()
            if open_attempt is not None:
                raise ClaimConflict(
                    f"step {run_id}/{step_id} has an open attempt"
                    f" #{open_attempt['attempt']}; double execution refused")
            number = attempt if attempt is not None else row["attempts"] + 1
            try:
                db.execute(
                    "INSERT INTO step_attempts (run_id, step_id, attempt, outcome,"
                    " effect_status, idempotency_key, started_at)"
                    " VALUES (?,?,?, 'started', ?, ?, ?)",
                    (run_id, step_id, number, effect_status, idempotency_key, now),
                )
            except sqlite3.IntegrityError as error:
                raise ClaimConflict(
                    f"attempt {number} of step {run_id}/{step_id} already exists") from error
            db.execute(
                "UPDATE workflow_steps SET status='running', attempts=?,"
                " started_at=COALESCE(started_at, ?), finished_at=NULL"
                " WHERE run_id=? AND step_id=?",
                (number, now, run_id, step_id),
            )
            self._event(db, run_id, "attempt_started", {
                "step_id": step_id, "attempt": number,
                "effect_status": effect_status, "idempotency_key": idempotency_key,
            })
        return self.get_attempt(run_id, step_id, number)  # type: ignore[return-value]

    def finish_attempt(
        self,
        run_id: str,
        step_id: str,
        attempt: int,
        outcome: str,
        *,
        effect_status: Optional[str] = None,
        result_ref: Optional[str] = None,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
        owner: Optional[str] = None,
        result_payload: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Atomically finish an attempt: its row, the step row and one event.

        The attempt goes to *outcome* ('completed'|'failed'|'timed_out'|
        'interrupted'); the step moves accordingly (timed_out -> failed) and
        takes result/error columns; one ``attempt_finished`` event lands in
        the same transaction. An attempt that is not open raises
        InvalidTransition. ``effect_status`` may raise 'unknown' to
        'confirmed' (verify passed) — the journal itself never moves an
        'unknown' back to 'none' (I9): that is resume's decision, never
        automatic.

        *result_payload* is the full serialized :class:`StepResult`. When
        given it is appended as a ``step_result`` event **in this same
        transaction**, so the finished step can never be observed without its
        evidence: a crash between the two is impossible (R6). *owner* fences a
        stale owner's write (see :meth:`_fence`).
        """
        if outcome not in ATTEMPT_OUTCOMES or outcome == "started":
            raise ValueError(f"finish outcome must be one of {ATTEMPT_OUTCOMES[1:]}")
        if effect_status is not None and effect_status not in EFFECT_STATUSES:
            raise ValueError(f"effect_status must be one of {EFFECT_STATUSES}")
        now = self._now()
        step_status = OUTCOME_TO_STEP_STATUS[outcome]
        with self._tx() as db:
            self._fence(db, run_id, owner)
            arow = db.execute(
                "SELECT * FROM step_attempts WHERE run_id=? AND step_id=? AND attempt=?",
                (run_id, step_id, attempt),
            ).fetchone()
            if arow is None:
                raise NotFoundError(f"attempt not found: {run_id}/{step_id}#{attempt}")
            if arow["outcome"] != "started":
                raise InvalidTransition(
                    f"attempt {run_id}/{step_id}#{attempt} is already {arow['outcome']!r}")
            new_effect = effect_status if effect_status is not None else arow["effect_status"]
            if arow["effect_status"] == "unknown" and new_effect == "none":
                raise InvalidTransition(
                    f"attempt {run_id}/{step_id}#{attempt} has an unknown effect;"
                    " the journal never downgrades it to 'none' (verify or decide, I9)")
            db.execute(
                "UPDATE step_attempts SET outcome=?, effect_status=?, finished_at=?"
                " WHERE run_id=? AND step_id=? AND attempt=?",
                (outcome, new_effect, now, run_id, step_id, attempt),
            )
            db.execute(
                "UPDATE workflow_steps SET status=?, result_ref=COALESCE(?, result_ref),"
                " error_code=COALESCE(?, error_code), error_detail=COALESCE(?, error_detail),"
                " finished_at=? WHERE run_id=? AND step_id=?",
                (step_status, result_ref, error_code, error_detail, now, run_id, step_id),
            )
            self._event(db, run_id, "attempt_finished", {
                "step_id": step_id, "attempt": attempt, "outcome": outcome,
                "effect_status": new_effect, "result_ref": result_ref,
                "error_code": error_code,
            })
            if result_payload is not None:
                self._event(db, run_id, "step_result", result_payload)
        return self.get_attempt(run_id, step_id, attempt)  # type: ignore[return-value]

    def get_attempt(self, run_id: str, step_id: str, attempt: int) -> Optional[Dict[str, Any]]:
        """The attempt row as a dict, or None."""
        row = self._db.execute(
            "SELECT * FROM step_attempts WHERE run_id=? AND step_id=? AND attempt=?",
            (run_id, step_id, attempt),
        ).fetchone()
        return None if row is None else self._row(row)

    def load_attempts(
        self, run_id: str, step_id: Optional[str] = None, *, limit: int = 100, offset: int = 0,
    ) -> Dict[str, Any]:
        """Attempts of a run (optionally one step), paginated and in order."""
        clauses, args = ["run_id=?"], [run_id]
        if step_id is not None:
            clauses.append("step_id=?")
            args.append(step_id)
        where = "WHERE " + " AND ".join(clauses)
        total = self._db.execute(
            f"SELECT COUNT(*) FROM step_attempts {where}", args
        ).fetchone()[0]
        rows = self._db.execute(
            f"SELECT * FROM step_attempts {where} ORDER BY rowid LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return {"items": [self._row(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    # -- spend and events -------------------------------------------------------

    def add_spend(self, run_id: str, micro_usd: int) -> int:
        """Add integer micro-USD to the run's ``spent_usd_micros``; the new total.

        Floats, bools, negatives and non-numbers raise ValueError — the budget
        is counted in whole micro-dollars only. Atomic, with one audit event.
        """
        if not isinstance(micro_usd, int) or isinstance(micro_usd, bool) or micro_usd < 0:
            raise ValueError("micro_usd must be a non-negative integer")
        with self._tx() as db:
            row = self._run_locked(db, run_id)
            total = row["spent_usd_micros"] + micro_usd
            db.execute(
                "UPDATE workflow_runs SET spent_usd_micros=?, updated_at=? WHERE run_id=?",
                (total, self._now(), run_id),
            )
            self._event(db, run_id, "spend_added", {"micro_usd": micro_usd, "total_micro_usd": total})
        return total

    def append_event(self, run_id: str, event: str, payload: Any = None) -> Dict[str, Any]:
        """Append one event to the run's append-only log; its row, seq included."""
        with self._tx() as db:
            self._run_locked(db, run_id)
            self._event(db, run_id, event, payload)
        row = self._db.execute(
            "SELECT seq FROM workflow_events WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return {"seq": row["seq"], "run_id": run_id, "event": event}

    def load_events(
        self, run_id: str, *, limit: int = 50, offset: int = 0,
    ) -> Dict[str, Any]:
        """The run's events, oldest first, paginated; payloads are parsed JSON when they are."""
        self._require_run(run_id)
        total = self._db.execute(
            "SELECT COUNT(*) FROM workflow_events WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        rows = self._db.execute(
            "SELECT * FROM workflow_events WHERE run_id=? ORDER BY seq LIMIT ? OFFSET ?",
            (run_id, limit, offset),
        ).fetchall()
        items = []
        for r in rows:
            item = self._row(r)
            if item.get("payload") is not None:
                try:
                    item["payload"] = json.loads(item["payload"])
                except (TypeError, ValueError):
                    pass
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _require_run(self, run_id: str) -> None:
        """Raise NotFoundError unless the run exists (plain read, no transaction)."""
        if self.get_run(run_id) is None:
            raise NotFoundError(f"run not found: {run_id}")
