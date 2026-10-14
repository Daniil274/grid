"""Executable DAG scheduler and quality gates (C3 of the durable-workflows plan).

The engine owns *scheduling*, *bounded concurrency*, *deadlines*, *budget
pausing* and *run/step status decisions*; it does **not** own effects. Every
step — including gates — runs through the single injected :data:`StepInvoker`
adapter (``invoke``). That adapter is where the existing ActionGate-mediated
tool call / dynamic agent lives; the engine never reaches into the tool
registry itself, so there is no "unmanaged tool invoke" path.

Design rules enforced here (see docs/durable-workflows-plan.md §8, invariants):

* **Dependencies.** A step becomes ready only once every ``depends_on`` step
  is ``completed``. If any dependency failed or was skipped, the step is
  skipped in turn and continues to propagate — it never runs.
* **Concurrency.** A per-run bound of ``min(max_parallel, max_global_parallel)``
  is enforced by the scheduler's in-flight count; a *shared* semaphore caps
  the total across runs.
* **Deadlines.** The run lifetime is ``deadline_at`` (wall clock); a step's
  ``timeout_seconds`` is counted **from slot acquisition** (P0-2) via
  ``asyncio.wait_for``; waiting for a slot is a *separate* ``queue_timeout``
  and fails with ``TIMEOUT`` without any invoker call.
* **Effects.** ``begin_attempt(effect_status="unknown")`` is committed *before*
  an effectful step runs (I3); an ``unknown`` effect is never downgraded to
  ``none`` (I9); the actual :class:`StepResult` is persisted as evidence, not
  only its ref.
* **Budget.** With a per-step reservation the engine can enforce
  ``spent + in_flight*reserve + reserve <= budget``; without one it pauses
  when ``spent >= budget`` and reports ``budget_bound_enforced=False`` — it
  never claims a bound it cannot keep.
* **Gates.** A gate step is machine-checked (the adapter returns a
  :class:`GateOutcome`); a failing gate is retried at most
  ``max_review_rounds`` *explicit* times, then fails with ``GATE_FAILED`` and
  skips its dependents.
* **Completion.** A run is ``completed`` only when every *required* step is
  completed and no required step left an unresolved ``unknown`` effect (I10);
  otherwise it is ``failed`` or ``interrupted`` — never a false success.
* **Cancellation.** Cancelling stops new launches and *drains* in-flight
  steps; if they outlive the drain window they are cancelled and their
  committed ``unknown`` effects are left unknown (reconcile's job, never
  silently downgraded).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from .failures import MalformedResult, failure_from_exception, make_failure
from .journal import JournalError, NotFoundError, WorkflowJournal
from .spec import (
    EffectStatus,
    FailureCode,
    GateOutcome,
    GateSpec,
    InitToolCall,
    Step,
    StepInputs,
    StepInvoker,
    StepResult,
    StepView,
    WorkflowRunView,
    WorkflowSpec,
    effect_class,
    pin,
)

__all__ = ["WorkflowEngine"]

_log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class _RunControl:
    """Live scheduling state of one run, addressable by :meth:`WorkflowEngine.cancel`."""

    cancel_event: asyncio.Event
    reason: str = ""
    task: Optional[asyncio.Task] = None
    paused: bool = False


class WorkflowEngine:
    """Schedules a validated :class:`WorkflowSpec` as an executable DAG.

    All effectful work goes through *invoke*; the engine is otherwise pure
    scheduling and journal bookkeeping. Dependencies are injected, so tests
    drive it with a fake invoker and a temporary journal and nothing leaves
    the process.
    """

    def __init__(
        self,
        *,
        journal: WorkflowJournal,
        invoke: StepInvoker,
        max_global_parallel: int = 16,
        clock: Callable[[], datetime] = _utcnow,
        init_invoke: Optional[Callable[[InitToolCall], Awaitable[bool]]] = None,
        spend_of: Optional[Callable[[StepResult], int]] = None,
        budget_reserve_micros: Optional[int] = None,
        owner: str = "workflow-engine",
        lease_seconds: float = 300.0,
        drain_seconds: float = 10.0,
    ) -> None:
        if max_global_parallel < 1:
            raise ValueError("max_global_parallel must be >= 1")
        if budget_reserve_micros is not None and budget_reserve_micros < 0:
            raise ValueError("budget_reserve_micros must be non-negative")
        self._journal = journal
        self._invoke = invoke
        self._max_global_parallel = max_global_parallel
        self._clock = clock
        self._init_invoke = init_invoke
        self._spend_of = spend_of
        self._budget_reserve_micros = budget_reserve_micros
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._drain_seconds = drain_seconds
        # a shared semaphore, one per running event loop (a fresh loop in tests
        # must not inherit a semaphore bound to an earlier loop).
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_loop: Optional[asyncio.AbstractEventLoop] = None
        self._controls: dict[str, _RunControl] = {}

    # -- public API -------------------------------------------------------------

    async def run(
        self,
        run_id: Optional[str | WorkflowSpec | dict] = None,
        spec: Optional[WorkflowSpec | dict] = None,
        *,
        deadline_at: Optional[str] = None,
        budget_usd_micros: Optional[int] = None,
    ) -> WorkflowRunView:
        """Execute *spec* to a terminal or paused state and return its view.

        Accepts ``run(spec, run_id=...)`` and ``run(run_id, spec)``; a missing
        run is created, an existing one is claimed (resume from ``interrupted``
        or ``paused_budget``) and double execution is refused by the journal.
        *budget_usd_micros* overrides the stored budget (a resume normally
        raises it); the spec pin is unchanged.
        """
        if spec is None:
            spec, run_id = run_id, None
        parsed = self._as_spec(spec)
        run_id = run_id or self._journal.new_run_id()
        run_row = self._prepare_run(run_id, parsed)
        if budget_usd_micros is not None:
            self._set_budget(run_id, budget_usd_micros)
            run_row = self._journal.get_run(run_id)

        self._journal.claim_run(
            run_id,
            self._owner,
            lease_seconds=self._lease_seconds,
            from_statuses=("queued", "interrupted", "paused_budget"),
            allow_same_owner=True,  # a resume may already hold the run under this owner
        )
        control = _RunControl(cancel_event=asyncio.Event())
        control.task = asyncio.current_task()
        self._controls[run_id] = control
        heartbeat = asyncio.ensure_future(self._heartbeat(run_id))
        try:
            await self._execute_run(run_id, parsed, control, run_row)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            self._controls.pop(run_id, None)
        return self._view(run_id)

    async def _heartbeat(self, run_id: str) -> None:
        """Keep the run's lease alive so reconcile never reaps a live run (R5).

        A best-effort background task: every ``lease_seconds/3`` it renews the
        lease under this engine's owner. Once the lease is lost (taken over or
        the run terminal) ``renew_lease`` raises and the loop stops. Cancelled
        when the run finishes or the drain window ends.
        """
        interval = max(0.01, self._lease_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                self._journal.renew_lease(
                    run_id, self._owner, lease_seconds=self._lease_seconds)
            except JournalError:
                _log.debug("lease heartbeat stopped for run %s", run_id)
                return

    async def cancel(self, run_id: str, reason: str = "cancelled") -> WorkflowRunView:
        """Request cancellation of *run_id* and return its view.

        A run this engine is actively scheduling is asked to stop and drains;
        otherwise the journal transition is applied directly.
        """
        control = self._controls.get(run_id)
        if control is not None:
            control.reason = reason
            control.cancel_event.set()
            if control.task is not None and control.task is not asyncio.current_task():
                try:
                    await control.task
                except asyncio.CancelledError:  # pragma: no cover - defensive
                    pass
            return self._view(run_id)

        run = self._journal.get_run(run_id)
        if run is None:
            raise NotFoundError(f"run not found: {run_id}")
        try:
            if run["status"] == "queued":
                self._journal.set_run_status(run_id, "cancelling", note=reason)
            current = self._journal.get_run(run_id)["status"]
            if current == "cancelling":
                self._journal.set_run_status(run_id, "emergency_stopped", note=reason)
        except JournalError:  # fail closed only about the journal being real
            raise
        return self._view(run_id)

    # -- setup ------------------------------------------------------------------

    @staticmethod
    def _as_spec(spec: WorkflowSpec | dict) -> WorkflowSpec:
        if isinstance(spec, WorkflowSpec):
            return spec
        return WorkflowSpec.model_validate(spec)

    def _prepare_run(self, run_id: str, spec: WorkflowSpec) -> dict[str, Any]:
        run = self._journal.get_run(run_id)
        if run is None:
            deadline_at = None
            if spec.deadline_seconds is not None:
                deadline_at = (
                    self._clock() + timedelta(seconds=spec.deadline_seconds)
                ).isoformat()
            queue_deadline_at = None
            if spec.queue_timeout_seconds is not None:
                queue_deadline_at = (
                    self._clock() + timedelta(seconds=spec.queue_timeout_seconds)
                ).isoformat()
            self._journal.create_run(
                run_id=run_id,
                spec=spec.model_dump(mode="json"),
                spec_sha256=pin(spec),
                deadline_at=deadline_at,
                queue_deadline_at=queue_deadline_at,
                budget_usd_micros=spec.budget_usd_micros(),
            )
        else:
            if run["spec_sha256"] != pin(spec):
                raise ValueError(
                    f"spec for run {run_id} does not match the stored pin; "
                    "refusing to execute a different spec"
                )
        for step in spec.steps:
            self._journal.add_step(run_id, step.id)
        return self._journal.get_run(run_id)  # type: ignore[return-value]

    def _set_budget(self, run_id: str, micros: int) -> None:
        """Update a run's budget (resume), through the journal's own transaction."""
        if not isinstance(micros, int) or isinstance(micros, bool) or micros < 0:
            raise ValueError("budget_usd_micros must be a non-negative integer")
        with self._journal._tx() as db:  # the journal's one-writer transaction
            db.execute(
                "UPDATE workflow_runs SET budget_usd_micros=?, updated_at=?"
                " WHERE run_id=?",
                (micros, self._journal._now(), run_id),
            )
            self._journal._event(db, run_id, "budget_set", {"budget_usd_micros": micros})

    def _get_sem(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(self._max_global_parallel)
            self._sem_loop = loop
        return self._sem

    def _load_step_results(self, run_id: str) -> dict[str, StepResult]:
        """Rebuild completed :class:`StepResult`s from persisted evidence.

        The journal stores every result as a ``step_result`` event carrying the
        full ``StepResult`` payload (evidence, not only a ref). On resume we
        replay those events so a completed step's dependents still see the real
        result object. Oldest first, last write per step wins; a malformed
        payload is skipped rather than aborting the run.
        """
        results: dict[str, StepResult] = {}
        offset = 0
        page = 100
        while True:
            batch = self._journal.load_events(run_id, limit=page, offset=offset)
            for event in batch["items"]:
                if event.get("event") != "step_result" or event.get("payload") is None:
                    continue
                try:
                    result = StepResult.model_validate(event["payload"])
                except Exception:  # a corrupt payload must not abort the resume
                    _log.warning("ignoring malformed step_result evidence in run %s",
                                 run_id, exc_info=True)
                    continue
                results[result.step_id] = result
            offset += page
            if offset >= batch["total"]:
                break
        return results

    # -- run loop ---------------------------------------------------------------

    async def _execute_run(
        self, run_id: str, spec: WorkflowSpec, control: _RunControl, run_row: dict[str, Any]
    ) -> None:
        loop = asyncio.get_running_loop()
        started = self._clock()
        steps = list(spec.steps)
        # Resume: hydrate already-persisted step state so a claimed run never
        # re-executes a completed step (the journal refuses that with
        # ClaimConflict). Completed steps keep their persisted result so
        # dependents still receive real dataflow inputs (not just a ref);
        # interrupted/running steps are put back in the ready set.
        persisted = {row["step_id"]: row for row in self._journal.load_steps(run_id)}
        persisted_results = self._load_step_results(run_id)
        status: dict[str, str] = {}
        results: dict[str, StepResult] = {}
        for s in steps:
            row = persisted.get(s.id)
            st = row["status"] if row is not None else "pending"
            if st == "completed":
                result = persisted_results.get(s.id)
                if result is None:
                    # R6 fail closed: a completed step must carry its persisted
                    # result evidence. Fabricating ok=True from the ref alone
                    # would silently lose the dataflow, so refuse instead.
                    self._journal.set_run_status(
                        run_id, "interrupted",
                        note=f"completed step {s.id!r} lost its result evidence",
                        expect="running")
                    raise JournalError(
                        f"completed step {s.id!r} of run {run_id} has no persisted"
                        " result evidence; refusing a fabricated success")
                status[s.id] = "completed"
                results[s.id] = result
            elif st in ("interrupted", "running"):
                status[s.id] = "pending"  # rescheduled for a fresh attempt on resume
            elif st == "failed":
                status[s.id] = "failed"  # never auto-retried by the engine
            elif st == "skipped":
                status[s.id] = "skipped"
            else:
                status[s.id] = "pending"
        gates: dict[str, GateOutcome] = {}
        unknown_effects: set[str] = set()
        max_concurrent = 0

        deadline_dt = _parse_dt(run_row.get("deadline_at"))
        per_run_limit = max(1, min(spec.max_parallel, self._max_global_parallel))
        queue_timeout = spec.queue_timeout_seconds

        init_degraded = await self._run_init(run_id, spec)
        if init_degraded is None:
            report = self._final_report(
                run_id, spec, status, results, gates, unknown_effects,
                started, max_concurrent, init_degraded=[], cancelled=False,
                timed_out=False, final="failed",
            )
            self._journal.set_run_status(
                run_id, "failed", note="required init tool failed",
                final_report=json.dumps(report, default=str),
            )
            return

        in_flight: dict[str, asyncio.Task] = {}
        ready_at: dict[str, float] = {}
        cancelled = False
        timed_out = False
        paused = False
        drain_deadline: Optional[float] = None

        while True:
            # 1. observe cancellation and the run lifetime.
            if control.cancel_event.is_set() and not cancelled:
                cancelled = True
                drain_deadline = loop.time() + self._drain_seconds
                self._journal.set_run_status(
                    run_id, "cancelling", note=control.reason or "cancelled")
            if not cancelled and not paused and deadline_dt is not None and self._clock() >= deadline_dt:
                if not timed_out:
                    timed_out = True
                    self._journal.append_event(
                        run_id, "run_deadline_exceeded",
                        {"deadline_at": run_row.get("deadline_at")})
            if timed_out and in_flight:
                for task in in_flight.values():
                    task.cancel()
                await asyncio.gather(*in_flight.values(), return_exceptions=True)
                for sid in list(in_flight):
                    status[sid] = "interrupted"
                in_flight.clear()

            # 2. propagate failures/skips to dependents.
            changed = True
            while changed:
                changed = False
                for step in steps:
                    if status[step.id] != "pending":
                        continue
                    if any(status[d] in ("failed", "skipped") for d in step.depends_on):
                        self._skip_step(run_id, step)
                        status[step.id] = "skipped"
                        changed = True

            # 3. ready set.
            ready = [
                step for step in steps
                if status[step.id] == "pending"
                and all(status[d] == "completed" for d in step.depends_on)
            ]
            for step in ready:
                ready_at.setdefault(step.id, loop.time())

            # 4. budget gate.
            if (not cancelled and not timed_out and not paused and ready
                    and self._budget_blocked(run_id, spec, len(in_flight))):
                paused = True
                self._journal.append_event(run_id, "budget_paused", {
                    "budget_usd_micros": run_row.get("budget_usd_micros"),
                    "spent_usd_micros": self._journal.get_run(run_id)["spent_usd_micros"],
                    "reserved": self._budget_reserve_micros is not None,
                })

            # 5. termination.
            if not in_flight and (not ready or cancelled or timed_out or paused):
                break

            # 6. launch up to the per-run bound.
            if not cancelled and not timed_out and not paused:
                for step in ready:
                    if len(in_flight) >= per_run_limit:
                        break
                    deps_results: StepInputs = {
                        d: results[d] for d in step.depends_on if d in results
                    }
                    task = loop.create_task(
                        self._run_step(
                            run_id, step, deps_results, queue_timeout,
                            ready_at.get(step.id)))
                    in_flight[step.id] = task
                    status[step.id] = "running"
                max_concurrent = max(max_concurrent, len(in_flight))
            if not in_flight:
                break

            # 7. wait for one to finish (or for cancellation / the run deadline).
            wait_timeout: Optional[float] = None
            if deadline_dt is not None and not timed_out:
                wait_timeout = max(0.0, (deadline_dt - self._clock()).total_seconds())
            if cancelled and drain_deadline is not None:
                drain_left = max(0.0, drain_deadline - loop.time())
                wait_timeout = drain_left if wait_timeout is None else min(wait_timeout, drain_left)
            waiters: list[asyncio.Task] = list(in_flight.values())
            cancel_waiter: Optional[asyncio.Task] = None
            if not cancelled:
                cancel_waiter = loop.create_task(control.cancel_event.wait())
                waiters.append(cancel_waiter)
            try:
                done, _ = await asyncio.wait(
                    waiters, timeout=wait_timeout, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if cancel_waiter is not None and not cancel_waiter.done():
                    cancel_waiter.cancel()
                    try:
                        await cancel_waiter
                    except asyncio.CancelledError:
                        pass
            for sid, task in list(in_flight.items()):
                if task not in done:
                    continue
                del in_flight[sid]
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    status[sid] = "interrupted"
                    continue
                except JournalError:
                    raise  # the journal is the source of truth: fail closed
                results[sid] = result
                if result.ok:
                    status[sid] = "completed"
                    if result.gate is not None:
                        gates[sid] = result.gate
                else:
                    status[sid] = "failed"
                if result.effect_status == "unknown":
                    unknown_effects.add(sid)

            if (cancelled and drain_deadline is not None
                    and loop.time() > drain_deadline and in_flight):
                for task in in_flight.values():
                    task.cancel()
                await asyncio.gather(*in_flight.values(), return_exceptions=True)
                for sid in list(in_flight):
                    status[sid] = "interrupted"
                in_flight.clear()

        required = [s for s in steps if s.required]
        required_bad = [s.id for s in required if status[s.id] in ("failed", "skipped")]
        required_incomplete = [s.id for s in required if status[s.id] != "completed"]
        unknown_required = sorted(unknown_effects & {s.id for s in required})

        if cancelled:
            final = "emergency_stopped"
        elif timed_out:
            final = "interrupted"
        elif paused:
            final = "paused_budget"
        elif required_bad:
            final = "failed"
        elif required_incomplete:
            final = "failed"
        elif unknown_required:
            final = "interrupted"  # I10: unresolved effects need a coordinator
        else:
            final = "completed"

        report = self._final_report(
            run_id, spec, status, results, gates, unknown_effects, started,
            max_concurrent, init_degraded=init_degraded, cancelled=cancelled,
            timed_out=timed_out, final=final, unknown_required=unknown_required,
        )
        self._journal.set_run_status(
            run_id, final, note=control.reason or None,
            final_report=json.dumps(report, default=str),
        )

    # -- one step ---------------------------------------------------------------

    async def _run_step(
        self,
        run_id: str,
        step: Step,
        deps_results: StepInputs,
        queue_timeout: Optional[int],
        enqueued_at: Optional[float] = None,
    ) -> StepResult:
        loop = asyncio.get_running_loop()
        queued_at = enqueued_at if enqueued_at is not None else loop.time()
        begin_effect: EffectStatus = "none" if effect_class(step) == "none" else "unknown"
        async with self._get_sem():
            waited = loop.time() - queued_at
            if queue_timeout is not None and waited > queue_timeout:
                attempt = self._journal.begin_attempt(
                    run_id, step.id, effect_status="none", owner=self._owner)["attempt"]
                failure = make_failure(
                    step.id, "TIMEOUT",
                    f"waited {waited:.3f}s for a slot > queue timeout {queue_timeout}s;"
                    " no work performed", step=step, attempt=attempt)
                return self._finish_failure(
                    run_id, step, attempt, "none", failure, outcome="timed_out")

            attempt = self._journal.begin_attempt(
                run_id, step.id, effect_status=begin_effect, owner=self._owner)["attempt"]
            try:
                if isinstance(step, GateSpec):
                    result = await self._run_gate_step(run_id, step, attempt, deps_results)
                else:
                    result = await asyncio.wait_for(
                        self._invoke(step, deps_results), timeout=step.timeout_seconds)
                if not isinstance(result, StepResult):
                    raise MalformedResult("invoker did not return a StepResult")
                result = result.model_copy(update={"attempt": attempt})
            except asyncio.CancelledError:
                self._finish_interrupted(run_id, step, attempt, begin_effect)
                raise
            except asyncio.TimeoutError as exc:
                failure = failure_from_exception(step, exc, attempt=attempt)
                return self._finish_failure(
                    run_id, step, attempt, begin_effect, failure, outcome="timed_out")
            except Exception as exc:
                failure = failure_from_exception(step, exc, attempt=attempt)
                return self._finish_failure(
                    run_id, step, attempt, begin_effect, failure, outcome="failed")
        return self._finish_result(run_id, step, attempt, begin_effect, result)

    async def _run_gate_step(
        self, run_id: str, step: GateSpec, attempt: int, deps_results: StepInputs
    ) -> StepResult:
        """Run one gate with at most ``max_review_rounds`` explicit checks."""
        rounds = 0
        last: Optional[StepResult] = None
        while rounds < step.max_review_rounds:
            rounds += 1
            result = await asyncio.wait_for(
                self._invoke(step, deps_results), timeout=step.timeout_seconds)
            if not isinstance(result, StepResult):
                raise MalformedResult("gate invoker did not return a StepResult")
            if not result.ok:
                return result  # the adapter reported its own failure
            last = result
            if result.gate is None:
                raise MalformedResult(f"gate step {step.id!r} returned no GateOutcome")
            if result.gate.passed:
                return result.model_copy(
                    update={"gate": result.gate.model_copy(update={"rounds": rounds})})
        outcome = last.gate if last is not None else None
        failure = make_failure(
            step.id, "GATE_FAILED",
            f"gate {step.check!r} not passed after {rounds} review round(s)",
            step=step, attempt=attempt)
        return StepResult(
            step_id=step.id, ok=False, attempt=attempt, effect_status="none",
            failure=failure,
            gate=outcome.model_copy(update={"rounds": rounds, "passed": False})
            if outcome is not None else None,
        )

    # -- persistence of step outcomes -------------------------------------------

    def _finish_result(
        self,
        run_id: str,
        step: Step,
        attempt: int,
        begin_effect: EffectStatus,
        result: StepResult,
    ) -> StepResult:
        if not result.ok:
            failure = result.failure or make_failure(
                step.id, "MALFORMED",
                "invoker returned ok=False without a failure", step=step, attempt=attempt)
            return self._finish_failure(
                run_id, step, attempt, begin_effect, failure, outcome="failed",
                evidence_result=result)
        effect: Optional[EffectStatus] = result.effect_status
        if begin_effect == "unknown" and effect != "confirmed":
            effect = None  # never downgrade a committed unknown to 'none' (I9)
        payload = result.model_dump(mode="json")
        payload["attempt"] = attempt
        self._journal.finish_attempt(
            run_id, step.id, attempt, "completed",
            effect_status=effect, result_ref=result.result_ref,
            owner=self._owner, result_payload=payload)
        self._record_spend(run_id, result)
        if begin_effect == "unknown":
            result = result.model_copy(
                update={"effect_status": "confirmed" if effect == "confirmed" else "unknown"})
        return result

    def _finish_failure(
        self,
        run_id: str,
        step: Step,
        attempt: int,
        begin_effect: EffectStatus,
        failure,
        *,
        outcome: str = "failed",
        evidence_result: Optional[StepResult] = None,
    ) -> StepResult:
        payload = None
        if evidence_result is not None:
            payload = evidence_result.model_dump(mode="json")
            payload["attempt"] = attempt
        self._journal.finish_attempt(
            run_id, step.id, attempt, outcome,
            effect_status=begin_effect if begin_effect == "unknown" else None,
            error_code=failure.code, error_detail=failure.detail,
            owner=self._owner, result_payload=payload)
        return StepResult(
            step_id=step.id, ok=False, attempt=attempt,
            result_ref=evidence_result.result_ref if evidence_result else None,
            effect_status="unknown" if begin_effect == "unknown" else "none",
            failure=failure,
            gate=evidence_result.gate if evidence_result else None,
            unverified_claims=evidence_result.unverified_claims if evidence_result else [],
        )

    def _finish_interrupted(
        self, run_id: str, step: Step, attempt: int, begin_effect: EffectStatus
    ) -> None:
        try:
            self._journal.finish_attempt(
                run_id, step.id, attempt, "interrupted",
                effect_status=begin_effect if begin_effect == "unknown" else None,
                error_code="INTERRUPTED", error_detail="step interrupted by cancellation",
                owner=self._owner)
            self._journal.append_event(
                run_id, "step_interrupted", {"step_id": step.id, "attempt": attempt})
        except Exception:  # pragma: no cover - best effort while being cancelled
            _log.warning("could not record interrupted attempt %s/%s#%s",
                         run_id, step.id, attempt, exc_info=True)

    def _persist_evidence(
        self, run_id: str, step: Step, attempt: int, result: StepResult
    ) -> None:
        """Persist the actual result, not only its ref (evidence, not a claim)."""
        payload = result.model_dump(mode="json")
        payload["attempt"] = attempt
        self._journal.append_event(run_id, "step_result", payload)

    def _record_spend(self, run_id: str, result: StepResult) -> None:
        if self._spend_of is None:
            return
        micros = self._spend_of(result)
        if micros:
            self._journal.add_spend(run_id, int(micros))

    def _skip_step(self, run_id: str, step: Step) -> None:
        self._journal.set_step_status(
            run_id, step.id, "skipped",
            error_code="DEPENDENCY_FAILED",
            error_detail="a dependency did not complete",
            owner=self._owner)

    # -- init -------------------------------------------------------------------

    async def _run_init(
        self, run_id: str, spec: WorkflowSpec
    ) -> Optional[list[str]]:
        """Run init tools. Returns degraded optional names, or None on fail-fast."""
        degraded: list[str] = []
        for call in spec.init_tools:
            ok = False
            detail = None
            if self._init_invoke is None:
                detail = "no init runner configured"
            else:
                try:
                    ok = bool(await self._init_invoke(call))
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}"
            if ok:
                self._journal.append_event(run_id, "init_ok", {"tool": call.name})
                continue
            if call.required:
                self._journal.append_event(run_id, "init_failed", {
                    "tool": call.name, "detail": detail, "required": True})
                return None
            degraded.append(call.name)
            self._journal.append_event(run_id, "init_degraded", {
                "tool": call.name, "detail": detail})
        return degraded

    # -- budget -----------------------------------------------------------------

    def _budget_blocked(self, run_id: str, spec: WorkflowSpec, n_in_flight: int) -> bool:
        run = self._journal.get_run(run_id)
        budget = run["budget_usd_micros"]
        if budget is None:
            budget = spec.budget_usd_micros()
        if budget is None:
            return False
        spent = run["spent_usd_micros"]
        if self._budget_reserve_micros is not None:
            reserved = self._budget_reserve_micros * (n_in_flight + 1)
            return spent + reserved > budget
        return spent >= budget

    # -- reporting --------------------------------------------------------------

    def _final_report(
        self,
        run_id: str,
        spec: WorkflowSpec,
        status: dict[str, str],
        results: dict[str, StepResult],
        gates: dict[str, GateOutcome],
        unknown_effects: set[str],
        started: datetime,
        max_concurrent: int,
        *,
        init_degraded: list[str],
        cancelled: bool,
        timed_out: bool,
        final: str,
        unknown_required: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        run = self._journal.get_run(run_id)
        steps_report = {}
        for step in spec.steps:
            result = results.get(step.id)
            entry: dict[str, Any] = {
                "status": status.get(step.id, "pending"),
                "kind": step.kind,
                "required": step.required,
                "effect_status": (
                    result.effect_status if result is not None else "none"),
                "result_ref": result.result_ref if result is not None else None,
            }
            if result is not None and result.failure is not None:
                entry["failure"] = result.failure.model_dump(mode="json")
            steps_report[step.id] = entry
        report = {
            "run_id": run_id,
            "status": final,
            "spec_sha256": run["spec_sha256"],
            "spent_usd_micros": run["spent_usd_micros"],
            "budget_usd_micros": run["budget_usd_micros"],
            "budget_bound_enforced": self._budget_reserve_micros is not None,
            "wall_seconds": (self._clock() - started).total_seconds(),
            "max_concurrent": max_concurrent,
            "steps": steps_report,
            "gates": {sid: gates[sid].model_dump(mode="json") for sid in gates},
            "unknown_effects": unknown_required if unknown_required is not None
            else sorted(unknown_effects),
            "init_degraded": init_degraded,
            "cancelled": cancelled,
            "timed_out": timed_out,
        }
        return report

    def _view(self, run_id: str) -> WorkflowRunView:
        run = self._journal.get_run(run_id)
        if run is None:
            raise NotFoundError(f"run not found: {run_id}")
        final_report = run["final_report"]
        if isinstance(final_report, str) and final_report:
            try:
                final_report = json.loads(final_report)
            except ValueError:
                pass
        steps: list[StepView] = []
        for row in self._journal.load_steps(run_id):
            attempts = self._journal.load_attempts(run_id, row["step_id"])
            effect_status: EffectStatus = "none"
            if attempts["items"]:
                effect_status = attempts["items"][-1]["effect_status"]
            steps.append(StepView(
                step_id=row["step_id"],
                status=row["status"],
                attempts=row["attempts"],
                result_ref=row["result_ref"],
                error_code=row["error_code"],
                effect_status=effect_status,
            ))
        return WorkflowRunView(
            run_id=run_id,
            spec_sha256=run["spec_sha256"],
            status=run["status"],
            created_at=run["created_at"],
            updated_at=run["updated_at"],
            deadline_at=run["deadline_at"],
            queue_deadline_at=run["queue_deadline_at"],
            budget_usd_micros=run["budget_usd_micros"],
            spent_usd_micros=run["spent_usd_micros"],
            steps=steps,
            final_report=final_report if isinstance(final_report, dict) else None,
        )
