"""Crash reconciliation and safe resume preparation (C4 of the durable-workflows plan).

Design: docs/durable-workflows-plan.md §7, invariants I5 and I9. This module
builds only on the C2 journal (``core/workflows/journal.py``) and the C1
contracts (``core/workflows/spec.py``); it never touches the engine (C3) and
never executes a step.

Two operations:

* :func:`reconcile` runs on process start and finalises the runs a crash left
  behind. It touches only *stale* runs — a run whose owner is dead or whose
  lease has expired — and never a live one (a live lease belongs to another
  executor). Every ``queued`` / ``running`` / ``cancelling`` stale run becomes
  ``interrupted``; its ``running`` steps become ``interrupted``; and every open
  attempt of an effectful step is closed as ``interrupted`` while its
  ``effect_status`` is kept (or forced) at ``unknown``. After reconcile no run
  is ``queued`` / ``running`` / ``cancelling`` (I5). Reconcile restarts
  *nothing*: a queued run without an invoker/lease is never auto-started.

* :func:`prepare_resume` computes (and, only when an owner/lease is supplied,
  applies) the resumable state of one interrupted run. It is deliberately
  conservative (secure defaults):

  - A **new attempt is prepared only for effect-free steps** (``read`` / ``gate``
    — their kind is the trusted classification, never a model claim).
  - ``write`` / ``exec`` (workspace effects) may be prepared **only** with
    ``write_policy="verify"`` *and* a caller-supplied **trusted verifier**
    callable that returns a bool. The verifier is trusted code, not an LLM
    answer: the fact that no diff shows up under ``write_paths`` does **not**
    prove the absence of external effects, so the default policy is ``never``
    and the absence of a diff is never a universal permission to replay.
  - ``egress`` (external effects) and ``agent`` (unknown effects) are **never**
    auto-replayed; the step is refused with ``UNKNOWN_EFFECT``.
  - Without an ``owner`` (invoker/lease) nothing is mutated and the run is not
    claimed — only a read-only plan is returned.
  - ``completed`` and ``skipped`` steps, and the whole attempt history, are
    preserved: a restart is always a *new* attempt (reset to ``ready``), never
    a rewrite of the past.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Optional, Sequence

from .journal import (
    ClaimConflict,
    JournalError,
    NotFoundError,
    WorkflowJournal,
)
from .spec import EffectClass, Step, WorkflowSpec, effect_class

#: The effect class of a step with no side effects (read/gate). It is the
#: trusted classification, not the step's declared kind.
EFFECT_FREE_CLASS: EffectClass = "none"

#: Reconcile candidates: a crash can leave any of these behind (I5).
ACTIVE_RUN_STATUSES = ("queued", "running", "cancelling")

#: Resume policy for workspace (write/exec) effects.
WriteVerifyPolicy = Literal["never", "verify"]

#: A trusted verifier: ``(journal, run_id, step_id) -> bool``. It runs
#: caller-owned code (e.g. a read-only checkpoint diff plus a policy check);
#: its return value is trusted precisely because it is code, not model text.
TrustedVerifier = Callable[[WorkflowJournal, str, str], bool]

_RESTART = "restart"
_BLOCKED = "blocked_unknown_effect"


class ReconcileError(JournalError):
    """Reconcile/resume refused: the run is not in a state that allows it."""


# -- reconcile report ---------------------------------------------------------


@dataclass
class RunReconciliation:
    """What :func:`reconcile` did to one crashed run."""

    run_id: str
    previous_status: str
    status: str
    interrupted_steps: list[str] = field(default_factory=list)
    interrupted_attempts: list[tuple[str, int]] = field(default_factory=list)
    unknown_effects: list[str] = field(default_factory=list)


@dataclass
class ReconcileReport:
    """Summary of one :func:`reconcile` pass over the whole journal."""

    runs: list[RunReconciliation] = field(default_factory=list)
    skipped_live: list[str] = field(default_factory=list)

    @property
    def reconciled_run_ids(self) -> list[str]:
        return [r.run_id for r in self.runs]

    @property
    def runs_interrupted(self) -> int:
        return len(self.runs)

    @property
    def runs_skipped_live(self) -> int:
        return len(self.skipped_live)

    @property
    def steps_interrupted(self) -> int:
        return sum(len(r.interrupted_steps) for r in self.runs)

    @property
    def attempts_interrupted(self) -> int:
        return sum(len(r.interrupted_attempts) for r in self.runs)

    @property
    def unknown_effects(self) -> list[str]:
        return sorted({s for r in self.runs for s in r.unknown_effects})


# -- resume plan --------------------------------------------------------------


@dataclass
class StepPlan:
    """The resume decision for one step."""

    step_id: str
    action: str
    effect_class: EffectClass
    effect_status: str = "none"
    detail: str = ""


@dataclass
class ResumePlan:
    """The outcome of :func:`prepare_resume`.

    ``owned`` is True only when an owner/lease was supplied and the run was
    claimed; then the restart/refusal decisions have also been applied to the
    journal. Otherwise it is a read-only plan.
    """

    run_id: str
    previous_status: str
    policy: WriteVerifyPolicy
    owned: bool
    owner: Optional[str] = None
    steps: list[StepPlan] = field(default_factory=list)

    @property
    def restart(self) -> list[str]:
        return [s.step_id for s in self.steps if s.action == _RESTART]

    @property
    def blocked(self) -> list[str]:
        return [s.step_id for s in self.steps if s.action == _BLOCKED]

    @property
    def completed(self) -> list[str]:
        return [s.step_id for s in self.steps if s.action == "completed"]

    @property
    def skipped(self) -> list[str]:
        return [s.step_id for s in self.steps if s.action == "skipped"]

    @property
    def unknown_effects(self) -> list[str]:
        return sorted({s.step_id for s in self.steps if s.effect_status == "unknown"})


# -- helpers ------------------------------------------------------------------


def _now(journal: WorkflowJournal, now: Optional[datetime]) -> datetime:
    """Reconcile's notion of "now": explicit, else the journal's own clock.

    Sharing the journal clock matters for lease expiry: claim/lease decisions
    inside the journal use that same clock.
    """
    if now is not None:
        return now
    clock = getattr(journal, "_clock", None)
    return clock() if callable(clock) else datetime.now(timezone.utc)


def _lease_live(run: dict, now: datetime) -> bool:
    lease = run.get("lease_expires_at")
    if not lease:
        return False
    try:
        expires = datetime.fromisoformat(lease)
    except (TypeError, ValueError):
        return False
    return expires > now


def _is_live(run: dict, now: datetime) -> bool:
    """A run another executor still owns: never touched by reconcile/resume."""
    return (
        run.get("status") in ("running", "cancelling")
        and bool(run.get("owner"))
        and _lease_live(run, now)
    )


def _spec_steps(run: dict) -> Optional[dict[str, Step]]:
    """Parse the frozen spec into ``{step_id: Step}``; None if it is unusable.

    A spec we cannot parse leaves every effect class at ``unknown`` (the
    secure default), so its steps are never auto-replayed.
    """
    raw = run.get("spec_json")
    if not raw:
        return None
    try:
        spec = WorkflowSpec.model_validate_json(raw)
    except Exception:  # noqa: BLE001 - any parse problem means "unknown effects"
        return None
    return {step.id: step for step in spec.steps}


def _class_of(spec_steps: Optional[dict[str, Step]], step_id: str) -> EffectClass:
    if spec_steps is None:
        return "unknown"
    step = spec_steps.get(step_id)
    if step is None:
        return "unknown"
    return effect_class(step)


def _open_attempts(journal: WorkflowJournal, run_id: str, step_id: str) -> list[dict]:
    return [a for a in journal.load_attempts(run_id, step_id)["items"] if a["outcome"] == "started"]


# -- reconcile ----------------------------------------------------------------


def _candidate_runs(
    journal: WorkflowJournal,
    *,
    run_ids: Optional[Sequence[str]],
    context_id: Optional[str],
    user_id: Optional[str],
) -> list[dict]:
    if run_ids is not None:
        found = [journal.get_run(rid) for rid in run_ids]
        return [r for r in found if r is not None]
    runs: list[dict] = []
    offset = 0
    limit = 200
    while True:
        page = journal.load_runs(
            statuses=list(ACTIVE_RUN_STATUSES),
            context_id=context_id,
            user_id=user_id,
            limit=limit,
            offset=offset,
        )
        runs.extend(page["items"])
        if len(page["items"]) < limit or len(runs) >= page["total"]:
            break
        offset += limit
    return runs


def _reconcile_run(journal: WorkflowJournal, run: dict) -> RunReconciliation:
    run_id = run["run_id"]
    previous = run["status"]
    owner = run.get("owner")
    spec_steps = _spec_steps(run)
    result = RunReconciliation(run_id=run_id, previous_status=previous, status="interrupted")

    for step in journal.load_steps(run_id):
        if step["status"] != "running":
            continue
        step_id = step["step_id"]
        cls = _class_of(spec_steps, step_id)
        effectful = cls != EFFECT_FREE_CLASS
        for attempt in _open_attempts(journal, run_id, step_id):
            recorded = attempt["effect_status"]
            # Secure: an effectful step is 'unknown' until proven otherwise;
            # a value already 'confirmed' by a verifier is left alone (I9).
            new_effect = "unknown" if effectful and recorded != "confirmed" else recorded
            try:
                journal.finish_attempt(
                    run_id, step_id, attempt["attempt"], "interrupted",
                    effect_status=new_effect, owner=owner,
                )
            except ClaimConflict:
                raise  # the run changed owner under us: it is live now
            except JournalError:
                # Another reconciler won the race; the step is handled below.
                pass
            result.interrupted_attempts.append((step_id, attempt["attempt"]))
            if new_effect == "unknown":
                result.unknown_effects.append(step_id)
        current = journal.get_step(run_id, step_id)
        if current is not None and current["status"] == "running":
            # A running step without an open attempt (should not happen, but a
            # crash window exists): interrupt it explicitly.
            journal.set_step_status(run_id, step_id, "interrupted", owner=owner)
        result.interrupted_steps.append(step_id)

    # Atomic, lease-aware CAS: a run a new owner just claimed is never touched
    # even though the stale snapshot above said it was dead (reconcile race).
    journal.interrupt_run(run_id, expect=previous, owner=owner)
    return result


def reconcile(
    journal: WorkflowJournal,
    *,
    now: Optional[datetime] = None,
    context_id: Optional[str] = None,
    user_id: Optional[str] = None,
    run_ids: Optional[Sequence[str]] = None,
) -> ReconcileReport:
    """Finalise crashed runs; leave live ones untouched.

    Only stale runs (dead owner / expired lease) are reconciled. Every stale
    ``queued`` / ``running`` / ``cancelling`` run becomes ``interrupted`` with
    its running steps, and every open attempt of an effectful step is closed
    with ``effect_status='unknown'``. Nothing is restarted — a queued run is
    not auto-started without an invoker/lease. *context_id* / *user_id*
    additionally scope the pass (the API-level ownership check builds on this).
    """
    report = ReconcileReport()
    current = _now(journal, now)
    for run in _candidate_runs(
        journal, run_ids=run_ids, context_id=context_id, user_id=user_id,
    ):
        if run["status"] not in ACTIVE_RUN_STATUSES:
            continue
        if _is_live(run, current):
            report.skipped_live.append(run["run_id"])
            continue
        try:
            report.runs.append(_reconcile_run(journal, run))
        except JournalError:
            # A concurrent reconciler already moved this run; skip it.
            report.skipped_live.append(run["run_id"])
    return report


# -- resume preparation -------------------------------------------------------


def _decide_interrupted(
    journal: WorkflowJournal,
    run_id: str,
    step_id: str,
    cls: EffectClass,
    policy: WriteVerifyPolicy,
    verifier: Optional[TrustedVerifier],
) -> tuple[str, str]:
    """Classify one interrupted step: ``restart`` or ``blocked`` (with reason).

    The decision is class-based (the trusted classification from the spec
    kind), never derived from the absence of a diff or from model text.
    """
    if cls == EFFECT_FREE_CLASS:
        return _RESTART, f"effect-free {cls} step: a new attempt is safe"
    if cls == "workspace":
        if policy == "verify" and verifier is not None:
            try:
                safe = bool(verifier(journal, run_id, step_id))
            except Exception:  # noqa: BLE001 - a failing verifier means "not proven"
                safe = False
            if safe:
                return _RESTART, "trusted verifier confirmed no unverified effect"
            return _BLOCKED, "trusted verifier did not confirm the workspace step is safe"
        return _BLOCKED, (
            f"policy={policy!r}: workspace effect is unverified;"
            " a missing diff under write_paths is not proof of no external effect"
        )
    # egress (external) / unknown (agent, unparsable spec)
    return _BLOCKED, f"{cls} effects cannot be verified: refusal (UNKNOWN_EFFECT)"


def _last_effect_status(journal: WorkflowJournal, run_id: str, step_id: str) -> str:
    attempts = journal.load_attempts(run_id, step_id)["items"]
    if not attempts:
        return "none"
    statuses = [a["effect_status"] for a in attempts]
    if "unknown" in statuses:
        return "unknown"
    if "confirmed" in statuses:
        return "confirmed"
    return "none"


def prepare_resume(
    journal: WorkflowJournal,
    run_id: str,
    *,
    write_policy: WriteVerifyPolicy = "never",
    verifier: Optional[TrustedVerifier] = None,
    owner: Optional[str] = None,
    lease_seconds: float = 300.0,
    context_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> ResumePlan:
    """Prepare the safe resumable state of an ``interrupted`` run.

    Always returns a :class:`ResumePlan`. Mutations (claiming the run, resetting
    safe steps for a new attempt, marking refused steps ``UNKNOWN_EFFECT``)
    happen only when *owner* (the invoker/lease capability) is supplied;
    without it the plan is read-only and the run is untouched.

    *write_policy* is ``"never"`` by default: a workspace step is refused unless
    the caller passes ``"verify"`` **and** a trusted *verifier*. ``egress`` and
    ``agent`` steps are always refused. ``completed`` / ``skipped`` steps and
    all attempts are preserved; a restart is a new attempt via ``reset_step``.
    """
    if write_policy not in ("never", "verify"):
        raise ValueError("write_policy must be 'never' or 'verify'")
    run = journal.get_run(run_id)
    if run is None:
        raise NotFoundError(f"run not found: {run_id}")
    if run["status"] != "interrupted":
        raise ReconcileError(
            f"run {run_id} is {run['status']!r}; only an interrupted run can be resumed")
    # Ownership is enforced by the API; this is a defensive scope guard.
    if context_id is not None and run.get("context_id") not in (None, context_id):
        raise ReconcileError(f"run {run_id} belongs to another context")
    if user_id is not None and run.get("user_id") not in (None, user_id):
        raise ReconcileError(f"run {run_id} belongs to another user")

    spec_steps = _spec_steps(run)
    plan = ResumePlan(
        run_id=run_id,
        previous_status=run["status"],
        policy=write_policy,
        owned=owner is not None,
        owner=owner,
    )

    for step in journal.load_steps(run_id):
        step_id = step["step_id"]
        status = step["status"]
        cls = _class_of(spec_steps, step_id)
        effect_status = _last_effect_status(journal, run_id, step_id) if status == "interrupted" else "none"
        if status == "interrupted":
            action, detail = _decide_interrupted(journal, run_id, step_id, cls, write_policy, verifier)
            plan.steps.append(StepPlan(step_id, action, cls, effect_status, detail))
        else:
            plan.steps.append(StepPlan(step_id, status, cls, effect_status))

    if owner is None:
        return plan

    # Claim BEFORE applying: an owner is the capability to mutate the run, so a
    # concurrent resume loses the claim (ClaimConflict) and mutates nothing —
    # two callers can never both reset the same steps and double-run them.
    # allow_same_owner makes the engine's later re-claim under the same owner a
    # no-op (the API passes the engine's own owner).
    journal.claim_run(
        run_id, owner, lease_seconds=lease_seconds,
        from_statuses=("interrupted",), allow_same_owner=True)
    for step_plan in plan.steps:
        if step_plan.action == _RESTART:
            current = journal.get_step(run_id, step_plan.step_id)
            if current is not None and current["status"] == "interrupted":
                journal.reset_step(run_id, step_plan.step_id, to="ready", owner=owner)
        elif step_plan.action == _BLOCKED:
            current = journal.get_step(run_id, step_plan.step_id)
            if current is not None and current["status"] == "interrupted":
                # Refusal is durable and non-claimable: begin_attempt only
                # accepts pending/ready/interrupted, so a blocked effectful step
                # cannot be re-run (no duplicate write). CAS on the current
                # status authorises the interrupted -> failed move; a
                # coordinator may reset the failed step explicitly later.
                journal.set_step_status(
                    run_id, step_plan.step_id, "failed", expect="interrupted",
                    error_code="UNKNOWN_EFFECT", error_detail=step_plan.detail,
                    owner=owner,
                )
    return plan


def is_resumable(run: dict, *, now: Optional[datetime] = None) -> bool:
    """Whether a run may be offered to :func:`prepare_resume` (interrupted, not live)."""
    if run.get("status") != "interrupted":
        return False
    if now is None:
        return True
    return not _is_live(run, now)
