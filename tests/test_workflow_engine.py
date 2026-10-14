"""Engine tests (C3): a fake invoker drives scheduling, deadlines, budget,
cancellation, init and gates against a temporary journal. No models, no network.

Everything the engine does to the outside world goes through the injected
:data:`StepInvoker`; these tests inject a small fake and assert on its calls,
the journal and the returned :class:`WorkflowRunView`.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core.workflows.engine import WorkflowEngine
from core.workflows.failures import (
    PolicyViolation,
    failure_from_exception,
    make_failure,
)
from core.workflows.journal import ClaimConflict, JournalError, WorkflowJournal
from core.workflows.reconcile import reconcile
from core.workflows.spec import (
    GateOutcome,
    StepResult,
    StepSpec,
    WorkflowSpec,
)


# -- helpers --------------------------------------------------------------------


def spec_of(steps, **kw) -> WorkflowSpec:
    return WorkflowSpec.model_validate({"name": "wf", "goal": "g", "steps": steps, **kw})


def read(step_id, deps=(), **kw):
    return {"id": step_id, "kind": "read", "tool": "read.file",
            "depends_on": list(deps), **kw}


def ok(step, *, effect_status="none", ref=None, gate=None, claims=()):
    return StepResult(step_id=step.id, ok=True, result_ref=ref or f"ref:{step.id}",
                      effect_status=effect_status, gate=gate, unverified_claims=list(claims))


class FakeInvoker:
    """Records calls and the observed concurrency; delegates to *handler*."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[str] = []
        self.inputs: dict[str, dict] = {}
        self.concurrent = 0
        self.max_concurrent = 0

    async def __call__(self, step, inputs):
        self.calls.append(step.id)
        self.inputs[step.id] = dict(inputs)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            return await self._handler(step, inputs)
        finally:
            self.concurrent -= 1


@pytest.fixture()
def journal(tmp_path):
    j = WorkflowJournal(tmp_path / "journal.db")
    yield j
    j.close()


# -- DAG and dependency propagation --------------------------------------------


async def test_dag_order_and_inputs_are_dependency_results(journal):
    spec = spec_of([read("a"), read("b", ["a"]), read("c", ["b"])])
    inv = FakeInvoker(lambda step, inputs: _coro(ok(step)))
    view = await WorkflowEngine(journal=journal, invoke=inv).run(spec)

    assert view.status == "completed"
    assert inv.calls == ["a", "b", "c"]
    assert inv.inputs["b"]["a"].result_ref == "ref:a"
    assert inv.inputs["c"]["b"].result_ref == "ref:b"
    assert all(s.status == "completed" for s in view.steps)


async def _coro(value):
    return value


# -- bounded concurrency -------------------------------------------------------


async def test_per_run_concurrency_is_bounded(journal):
    spec = spec_of([read(f"s{i}") for i in range(6)], max_parallel=2)

    async def handler(step, inputs):
        await asyncio.sleep(0.02)
        return ok(step)

    inv = FakeInvoker(handler)
    view = await WorkflowEngine(journal=journal, invoke=inv).run(spec)
    assert view.status == "completed"
    assert inv.max_concurrent <= 2


async def test_shared_global_semaphore_spans_runs(journal):
    async def handler(step, inputs):
        await asyncio.sleep(0.03)
        return ok(step)

    inv = FakeInvoker(handler)
    engine = WorkflowEngine(journal=journal, invoke=inv, max_global_parallel=2)
    first = spec_of([read(f"a{i}") for i in range(4)], max_parallel=2)
    second = spec_of([read(f"b{i}") for i in range(4)], max_parallel=2)
    views = await asyncio.gather(engine.run(first), engine.run(second))
    assert [v.status for v in views] == ["completed", "completed"]
    assert inv.max_concurrent <= 2


# -- deadlines: work timeout from slot, queue timeout separately ----------------


async def test_work_timeout_counts_from_slot_not_from_queue(journal):
    # one lane: b waits ~0.6s in the queue; its 1s work budget starts at the slot.
    spec = spec_of([read("a", timeout_seconds=1), read("b", timeout_seconds=1)],
                   max_parallel=1)

    async def handler(step, inputs):
        await asyncio.sleep(0.6)
        return ok(step)

    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "completed"
    assert {s.status for s in view.steps} == {"completed"}


async def test_work_timeout_fails_the_step(journal):
    spec = spec_of([read("a", timeout_seconds=1)])

    async def handler(step, inputs):
        await asyncio.sleep(1.3)
        return ok(step)

    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "failed"
    step = view.steps[0]
    assert step.status == "failed"
    assert step.error_code == "TIMEOUT"


async def test_queue_timeout_fails_without_invoking(journal):
    spec = spec_of([read("a"), read("b")], max_parallel=1, queue_timeout_seconds=1)
    called: list[str] = []

    async def handler(step, inputs):
        called.append(step.id)
        assert step.id != "b", "queued-out step must not be invoked"
        await asyncio.sleep(1.2)
        return ok(step)

    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert called == ["a"]
    b = next(s for s in view.steps if s.step_id == "b")
    assert b.status == "failed" and b.error_code == "TIMEOUT"
    assert view.status == "failed"


async def test_run_deadline_interrupts_in_flight(journal):
    spec = spec_of([read("a")], deadline_seconds=1)

    async def handler(step, inputs):
        await asyncio.sleep(5)
        return ok(step)

    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "interrupted"
    assert view.steps[0].status == "interrupted"


# -- budget pause and resume ---------------------------------------------------


async def test_budget_pause_and_resume(journal):
    spec = spec_of([read(f"s{i}") for i in range(4)], max_parallel=1, budget_usd=2.0)

    async def handler(step, inputs):
        return ok(step)

    engine = WorkflowEngine(journal=journal, invoke=FakeInvoker(handler),
                            spend_of=lambda r: 1_000_000)
    view = await engine.run(spec)
    assert view.status == "paused_budget"
    assert view.spent_usd_micros == 2_000_000
    # without a reservation the engine claims no bound it cannot keep
    assert view.final_report["budget_bound_enforced"] is False

    resumed = await engine.run(view.run_id, spec, budget_usd_micros=4_000_000)
    assert resumed.status == "completed"
    assert resumed.spent_usd_micros == 4_000_000


async def test_resume_preserves_completed_step_and_dataflow(journal):
    # a completed step must not be re-run on resume, and its persisted result
    # must still reach a dependent that never got to run before the pause.
    spec = spec_of([read("a"), read("b", ["a"])], max_parallel=1, budget_usd=1.0)
    inv = FakeInvoker(lambda step, inputs: _coro(ok(step)))
    engine = WorkflowEngine(journal=journal, invoke=inv, spend_of=lambda r: 1_000_000)

    view = await engine.run(spec)
    assert view.status == "paused_budget"
    assert inv.calls == ["a"]

    resumed = await engine.run(view.run_id, spec, budget_usd_micros=2_000_000)
    assert resumed.status == "completed"
    assert inv.calls == ["a", "b"]  # a was not executed twice
    assert inv.inputs["b"]["a"].result_ref == "ref:a"  # real dataflow, not just a ref
    a = next(s for s in resumed.steps if s.step_id == "a")
    assert a.status == "completed" and a.attempts == 1


async def test_budget_reservation_prevents_overshoot(journal):
    spec = spec_of([read(f"s{i}") for i in range(4)], max_parallel=1, budget_usd=2.0)

    async def handler(step, inputs):
        return ok(step)

    engine = WorkflowEngine(journal=journal, invoke=FakeInvoker(handler),
                            spend_of=lambda r: 1_000_000,
                            budget_reserve_micros=1_000_000)
    view = await engine.run(spec)
    assert view.status == "paused_budget"
    assert view.spent_usd_micros <= 2_000_000  # never overshot the budget
    assert view.final_report["budget_bound_enforced"] is True


# -- init tools ----------------------------------------------------------------


async def test_required_init_failure_fails_fast(journal):
    spec = spec_of([read("a")], init_tools=[{"name": "init.db", "required": True}])
    inv = FakeInvoker(lambda step, inputs: _coro(ok(step)))

    async def init(call):
        return False

    view = await WorkflowEngine(journal=journal, invoke=inv, init_invoke=init).run(spec)
    assert view.status == "failed"
    assert inv.calls == []  # nothing ran
    assert view.steps[0].status != "completed"


async def test_optional_init_failure_degrades(journal):
    spec = spec_of([read("a")], init_tools=[{"name": "init.opt", "required": False}])
    inv = FakeInvoker(lambda step, inputs: _coro(ok(step)))

    async def init(call):
        return False

    view = await WorkflowEngine(journal=journal, invoke=inv, init_invoke=init).run(spec)
    assert view.status == "completed"
    assert view.final_report["init_degraded"] == ["init.opt"]


async def test_required_init_success_proceeds(journal):
    spec = spec_of([read("a")], init_tools=[{"name": "init.db", "required": True}])
    inv = FakeInvoker(lambda step, inputs: _coro(ok(step)))

    async def init(call):
        return True

    view = await WorkflowEngine(journal=journal, invoke=inv, init_invoke=init).run(spec)
    assert view.status == "completed"
    assert inv.calls == ["a"]


# -- gates ---------------------------------------------------------------------


async def test_gate_pass_unblocks_dependents(journal):
    spec = spec_of([
        read("a"),
        {"id": "g", "kind": "gate", "check": "quality.lint", "depends_on": ["a"]},
        read("b", ["g"]),
    ])

    async def handler(step, inputs):
        if step.kind == "gate":
            return ok(step, gate=GateOutcome(check=step.check, passed=True))
        return ok(step)

    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "completed"
    b = next(s for s in view.steps if s.step_id == "b")
    assert b.status == "completed"
    assert view.final_report["gates"]["g"]["passed"] is True


async def test_gate_failure_skips_dependents(journal):
    spec = spec_of([
        read("a"),
        {"id": "g", "kind": "gate", "check": "quality.lint", "depends_on": ["a"],
         "max_review_rounds": 1},
        read("b", ["g"]),
    ])

    async def handler(step, inputs):
        if step.kind == "gate":
            return ok(step, gate=GateOutcome(check=step.check, passed=False))
        return ok(step)

    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "failed"
    g = next(s for s in view.steps if s.step_id == "g")
    b = next(s for s in view.steps if s.step_id == "b")
    assert g.status == "failed" and g.error_code == "GATE_FAILED"
    assert b.status == "skipped"


async def test_gate_review_is_bounded(journal):
    spec = spec_of([
        {"id": "g", "kind": "gate", "check": "quality.lint", "max_review_rounds": 2},
    ])
    inv = FakeInvoker(lambda step, inputs: _coro(
        ok(step, gate=GateOutcome(check=step.check, passed=False))))

    view = await WorkflowEngine(journal=journal, invoke=inv).run(spec)
    assert inv.calls == ["g", "g"]  # exactly max_review_rounds explicit checks
    assert view.steps[0].error_code == "GATE_FAILED"


# -- retry policy: code decides, never the model -------------------------------


def test_retryable_is_computed_by_code_and_trusted_effect_class():
    read_step = StepSpec(id="r", kind="read", tool="t")
    assert make_failure("r", "TIMEOUT", "x", step=read_step).retryable is True
    # egress has external, unverifiable effects -> never auto-retryable
    egress = StepSpec(id="e", kind="egress", tool="t")
    assert make_failure("e", "TIMEOUT", "x", step=egress).retryable is False
    # a self-declared idempotent write is still not replay authority
    write = StepSpec(id="w", kind="write", tool="t", write_paths=["out/x"], idempotent=True)
    assert make_failure("w", "TIMEOUT", "x", step=write).retryable is False
    agent = StepSpec(id="g", kind="agent", task="do", idempotent=True)
    assert make_failure("g", "TRANSIENT_ERROR", "x", step=agent).retryable is False
    # a forbidden path is never retryable, even for an effect-free step
    assert make_failure("r", "POLICY_VIOLATION", "x", step=read_step).retryable is False


def test_make_failure_defaults_unsafe_without_a_step():
    assert make_failure("s", "TIMEOUT", "x").retryable is False


def test_failure_from_exception_maps_structured_errors():
    read_step = StepSpec(id="r", kind="read", tool="t")
    policy = PolicyViolation("outside zone", allowed_write_paths=["out/x"])
    mapped = failure_from_exception(read_step, policy)
    assert mapped.code == "POLICY_VIOLATION"
    assert mapped.retryable is False
    assert mapped.allowed_write_paths == ["out/x"]
    assert failure_from_exception(read_step, TimeoutError("slow")).code == "TIMEOUT"
    assert failure_from_exception(read_step, ValueError("boom")).code == "MALFORMED"


async def test_engine_does_not_auto_retry_a_failed_step(journal):
    # max_retries is a recorded budget for resume, not a licence for the engine
    spec = spec_of([read("a", max_retries=2)])

    async def handler(step, inputs):
        raise PolicyViolation("nope")

    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "failed"
    attempts = journal.load_attempts(view.run_id, "a")["items"]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "failed"
    assert view.final_report["steps"]["a"]["failure"]["code"] == "POLICY_VIOLATION"


# -- completion rules ----------------------------------------------------------


async def test_completed_run_excludes_failed_required_step(journal):
    async def handler(step, inputs):
        if step.id == "b":
            raise PolicyViolation("nope")
        return ok(step)

    spec = spec_of([read("a"), read("b")])
    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "failed"


async def test_optional_failure_does_not_fail_the_run(journal):
    async def handler(step, inputs):
        if step.id == "a":
            raise PolicyViolation("nope")
        return ok(step)

    spec = spec_of([read("a", required=False), read("c")])
    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "completed"
    a = next(s for s in view.steps if s.step_id == "a")
    assert a.status == "failed"
    c = next(s for s in view.steps if s.step_id == "c")
    assert c.status == "completed"


async def test_completed_run_excludes_unresolved_unknown_effect(journal):
    # a write step that admits its effect is unknown cannot mean "completed" (I10)
    spec = spec_of([
        {"id": "w", "kind": "write", "tool": "write.file", "write_paths": ["out/x"]},
    ])

    async def handler(step, inputs):
        return ok(step, effect_status="unknown")

    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "interrupted"
    assert view.final_report["unknown_effects"] == ["w"]


async def test_verified_write_completes(journal):
    spec = spec_of([
        {"id": "w", "kind": "write", "tool": "write.file", "write_paths": ["out/x"]},
    ])

    async def handler(step, inputs):
        return ok(step, effect_status="confirmed")

    view = await WorkflowEngine(journal=journal, invoke=FakeInvoker(handler)).run(spec)
    assert view.status == "completed"


# -- evidence, cancellation, double execution ----------------------------------


async def test_actual_result_is_persisted_not_only_its_ref(journal):
    spec = spec_of([read("a")])

    async def handler(step, inputs):
        return ok(step, ref="artifact://x", claims=["model thinks it is fine"])

    engine = WorkflowEngine(journal=journal, invoke=FakeInvoker(handler))
    view = await engine.run(spec)
    events = journal.load_events(view.run_id)["items"]
    persisted = [e for e in events if e["event"] == "step_result"]
    assert persisted and persisted[0]["payload"]["result_ref"] == "artifact://x"
    assert persisted[0]["payload"]["unverified_claims"] == ["model thinks it is fine"]


async def test_cancellation_drains_and_leaves_unknown_effect(journal):
    spec = spec_of([
        {"id": "w", "kind": "write", "tool": "write.file", "write_paths": ["out/x"]},
    ], max_parallel=1)

    async def handler(step, inputs):
        await asyncio.sleep(1.0)
        return ok(step, effect_status="confirmed")

    run_id = journal.new_run_id()
    engine = WorkflowEngine(journal=journal, invoke=FakeInvoker(handler),
                            drain_seconds=0.05)
    task = asyncio.create_task(engine.run(run_id, spec))
    await asyncio.sleep(0.05)  # let the write begin (attempt committed before effect)
    view = await engine.cancel(run_id, "user stop")
    await task

    assert view.status == "emergency_stopped"
    assert view.steps[0].status == "interrupted"
    attempts = journal.load_attempts(run_id, "w")["items"]
    assert attempts[-1]["outcome"] == "interrupted"
    assert attempts[-1]["effect_status"] == "unknown"  # never downgraded to none


async def test_second_execution_of_a_finished_run_is_refused(journal):
    spec = spec_of([read("a")])
    inv = FakeInvoker(lambda step, inputs: _coro(ok(step)))
    engine = WorkflowEngine(journal=journal, invoke=inv)
    view = await engine.run(spec)
    assert view.status == "completed"
    with pytest.raises(ClaimConflict):
        await engine.run(view.run_id, spec)


# -- R5 heartbeat and R6 fail-closed resume ------------------------------------


async def test_heartbeat_keeps_a_long_run_live(tmp_path):
    # R5: a run that outlives its own lease must not be reaped mid-flight — the
    # engine renews the lease in the background (reconcile never touches a live
    # run). Without the heartbeat reconcile would interrupt it here.
    journal = WorkflowJournal(tmp_path / "hb.db")
    spec = spec_of([read("a")])

    async def handler(step, inputs):
        await asyncio.sleep(1.0)
        return ok(step)

    engine = WorkflowEngine(
        journal=journal, invoke=FakeInvoker(handler), lease_seconds=0.4)
    task = asyncio.create_task(engine.run(spec))
    await asyncio.sleep(0.6)  # well past the 0.4s lease, but renewed meanwhile
    report = reconcile(journal)
    assert report.reconciled_run_ids == []
    assert report.skipped_live  # seen as live, left alone
    view = await task
    assert view.status == "completed"


async def test_resume_refuses_completed_step_without_evidence(journal):
    # R6 fail closed: a completed step whose result payload is missing must not
    # be turned into a fabricated ok=True — the run is interrupted instead.
    spec = spec_of([read("a")])
    engine = WorkflowEngine(journal=journal, invoke=FakeInvoker(lambda s, i: _coro(ok(s))))
    run_id = journal.new_run_id()
    engine._prepare_run(run_id, spec)
    journal.set_step_status(
        run_id, "a", "completed", expect="pending", result_ref="ref:a",
        error_code=None, error_detail=None)
    assert [e for e in journal.load_events(run_id)["items"]
            if e["event"] == "step_result"] == []

    with pytest.raises(JournalError):
        await engine.run(run_id, spec)
    assert journal.get_run(run_id)["status"] == "interrupted"
