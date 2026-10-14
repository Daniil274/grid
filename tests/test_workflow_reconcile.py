"""C4 tests: crash reconciliation and safe resume preparation (plan §7, §10).

Everything runs locally — SQLite in a tmp dir, a fake clock, no model calls.
Covered: reconcile finalises crashed runs after a reopen (I5); a live lease is
never touched while a dead owner / expired lease is; unknown effects of
effectful steps are recorded and never auto-replayed (I9); safe resume
preserves completed outputs and the attempt history; a blocked write creates no
second attempt (no duplicate write); and the resume decision comes from the
trusted step kind plus a trusted verifier, never from model text or a missing
diff.

"Crash" here means the owner is dead: leases are short and the fake clock is
advanced past them, so a *live* lease (another executor) is never touched.
"""

from datetime import datetime, timedelta

import pytest

from core.workflows.journal import ClaimConflict, InvalidTransition, WorkflowJournal
from core.workflows.reconcile import (
    ACTIVE_RUN_STATUSES,
    ReconcileError,
    is_resumable,
    prepare_resume,
    reconcile,
)


class FakeClock:
    """Deterministic wall clock; advancing it expires leases."""

    def __init__(self, start="2026-10-10T12:00:00+00:00"):
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


LOCK_LEASE = 300.0

SPEC_STEPS = [
    {"id": "a", "kind": "read"},
    {"id": "b", "kind": "write", "write_paths": ["out.txt"], "depends_on": ["a"]},
    {"id": "c", "kind": "egress"},
]


def make_journal(tmp_path, clock=None):
    return WorkflowJournal(tmp_path / "wf.db", clock=clock or FakeClock())


def make_run(journal, steps, *, run_id=None, context_id="ctx-1", user_id="user-1"):
    spec = {"name": "w", "goal": "g", "steps": steps}
    run_id = journal.create_run(
        run_id=run_id, spec=spec, spec_sha256="a" * 64,
        context_id=context_id, user_id=user_id,
    )["run_id"]
    for step in steps:
        journal.add_step(run_id, step["id"])
    return run_id


def crash(journal, clock, run_id, begin, *, owner="owner-1", lease=LOCK_LEASE):
    """Claim the run, start the given attempts, then make the owner dead."""
    journal.claim_run(run_id, owner, lease_seconds=lease)
    for step_id, effect in begin:
        journal.begin_attempt(run_id, step_id, effect_status=effect)
    clock.advance(lease + 1)  # the lease is now expired: the owner is dead


# -- reconcile: reopen, stale vs live, unknown effects ----------------------


def test_reconcile_after_reopen_interrupts_runs_steps_and_attempts(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    run_id = make_run(journal, SPEC_STEPS)
    crash(journal, clock, run_id, [("a", "none"), ("b", "unknown")])
    journal.close()

    # The process restarted: same file, a fresh journal instance.
    journal = make_journal(tmp_path, clock)
    report = reconcile(journal)

    assert report.reconciled_run_ids == [run_id]
    assert report.runs_interrupted == 1
    assert report.steps_interrupted == 2
    assert report.attempts_interrupted == 2
    assert journal.get_run(run_id)["status"] == "interrupted"
    assert journal.get_step(run_id, "a")["status"] == "interrupted"
    assert journal.get_step(run_id, "b")["status"] == "interrupted"
    # the effect-free attempt stays 'none'; the effectful one is 'unknown' (I9)
    assert journal.get_attempt(run_id, "a", 1)["outcome"] == "interrupted"
    assert journal.get_attempt(run_id, "a", 1)["effect_status"] == "none"
    assert journal.get_attempt(run_id, "b", 1)["effect_status"] == "unknown"
    assert report.unknown_effects == ["b"]
    assert len(report.runs[0].interrupted_attempts) == 2

    # A second pass sees no active run left to reconcile (I5, idempotent).
    assert reconcile(journal).runs_interrupted == 0


def test_reconcile_leaves_live_owner_and_reaps_expired_lease(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    make_run(journal, [{"id": "a", "kind": "read"}], run_id="wf-stale")
    make_run(journal, [{"id": "a", "kind": "read"}], run_id="wf-live")

    journal.claim_run("wf-stale", "old-owner", lease_seconds=100.0)
    clock.advance(200.0)  # the stale lease has expired ...
    journal.claim_run("wf-live", "live-owner", lease_seconds=1000.0)  # ... this one has not

    report = reconcile(journal)

    assert report.reconciled_run_ids == ["wf-stale"]
    assert report.skipped_live == ["wf-live"]
    assert journal.get_run("wf-stale")["status"] == "interrupted"
    assert journal.get_run("wf-live")["status"] == "running"
    assert journal.get_run("wf-live")["owner"] == "live-owner"


def test_reconcile_queued_is_interrupted_not_auto_started(tmp_path):
    journal = make_journal(tmp_path)
    make_run(journal, [{"id": "a", "kind": "read"}], run_id="wf-q")
    report = reconcile(journal)
    assert report.reconciled_run_ids == ["wf-q"]
    run = journal.get_run("wf-q")
    # No invoker/lease: reconcile never auto-restarts a queued run.
    assert run["status"] == "interrupted"
    assert run["owner"] is None


def test_reconcile_scopes_by_context(tmp_path):
    journal = make_journal(tmp_path)
    make_run(journal, [{"id": "a", "kind": "read"}], run_id="wf-c1", context_id="ctx-1")
    make_run(journal, [{"id": "a", "kind": "read"}], run_id="wf-c2", context_id="ctx-2")
    report = reconcile(journal, context_id="ctx-2")
    assert report.reconciled_run_ids == ["wf-c2"]
    assert journal.get_run("wf-c1")["status"] == "queued"
    assert set(ACTIVE_RUN_STATUSES) >= {"queued", "running", "cancelling"}


# -- resume: effect-free restart, verification, refusal ---------------------


def test_resume_without_owner_is_readonly_and_does_not_restart(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    run_id = make_run(journal, [{"id": "a", "kind": "read"}])
    crash(journal, clock, run_id, [("a", "none")])
    assert reconcile(journal).runs_interrupted == 1

    # No invoker/lease: a plan is computed, nothing is mutated or claimed.
    plan = prepare_resume(journal, run_id)
    assert plan.owned is False
    assert plan.restart == ["a"]
    assert journal.get_run(run_id)["status"] == "interrupted"
    assert journal.get_step(run_id, "a")["status"] == "interrupted"
    assert journal.load_attempts(run_id, "a")["total"] == 1


def test_safe_resume_restarts_read_and_preserves_completed_and_attempts(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    steps = [{"id": "a", "kind": "read"}, {"id": "b", "kind": "read", "depends_on": ["a"]}]
    run_id = make_run(journal, steps)
    journal.claim_run(run_id, "owner-1")
    journal.begin_attempt(run_id, "a")
    journal.finish_attempt(run_id, "a", 1, "completed", result_ref="res:a")
    journal.begin_attempt(run_id, "b")  # crash mid-read
    clock.advance(LOCK_LEASE + 1)

    reconcile(journal)
    plan = prepare_resume(journal, run_id, owner="owner-2")

    assert plan.owned is True
    assert plan.completed == ["a"]
    assert plan.restart == ["b"]
    # completed output and its single attempt survive untouched
    step_a = journal.get_step(run_id, "a")
    assert step_a["status"] == "completed" and step_a["result_ref"] == "res:a"
    assert journal.load_attempts(run_id, "a")["total"] == 1
    # the interrupted read is prepared for a *new* attempt, old one kept
    assert journal.get_step(run_id, "b")["status"] == "ready"
    assert journal.load_attempts(run_id, "b")["total"] == 1
    assert journal.get_run(run_id)["status"] == "running"
    assert journal.get_run(run_id)["owner"] == "owner-2"


def test_resume_refuses_write_when_policy_is_never(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    run_id = make_run(journal, [{"id": "b", "kind": "write", "write_paths": ["out.txt"]}])
    crash(journal, clock, run_id, [("b", "unknown")])
    reconcile(journal)

    plan = prepare_resume(journal, run_id, owner="owner-2")  # default policy is 'never'

    assert plan.blocked == ["b"]
    assert plan.restart == []


def test_resume_refuses_external_effect_no_duplicate_write(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    run_id = make_run(journal, [{"id": "c", "kind": "egress"}])
    crash(journal, clock, run_id, [("c", "unknown")])
    reconcile(journal)

    plan = prepare_resume(journal, run_id, owner="owner-2")

    # egress effects cannot be verified: the step is refused with UNKNOWN_EFFECT
    assert plan.blocked == ["c"]
    step = journal.get_step(run_id, "c")
    assert step["status"] == "failed" and step["error_code"] == "UNKNOWN_EFFECT"
    # no second attempt was created, and the blocked step cannot be claimed
    assert journal.load_attempts(run_id, "c")["total"] == 1
    with pytest.raises(ClaimConflict):
        journal.begin_attempt(run_id, "c", effect_status="unknown")


def test_resume_verify_requires_trusted_verifier(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    run_id = make_run(journal, [{"id": "b", "kind": "write", "write_paths": ["out.txt"]}])
    crash(journal, clock, run_id, [("b", "unknown")])
    reconcile(journal)

    calls = []

    def verifier(jrnl, rid, sid):
        calls.append((rid, sid))
        return True

    # policy 'verify' without a verifier still refuses (no proof, no replay)
    assert prepare_resume(journal, run_id, write_policy="verify").blocked == ["b"]
    # with a trusted verifier that proves safety, the step is prepared again
    plan = prepare_resume(journal, run_id, write_policy="verify", verifier=verifier, owner="o")
    assert plan.restart == ["b"]
    assert calls == [(run_id, "b")]
    assert journal.get_step(run_id, "b")["status"] == "ready"


def test_resume_verify_false_verifier_still_refuses(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    run_id = make_run(journal, [{"id": "b", "kind": "write", "write_paths": ["out.txt"]}])
    crash(journal, clock, run_id, [("b", "unknown")])
    reconcile(journal)

    plan = prepare_resume(
        journal, run_id, write_policy="verify", verifier=lambda *a: False, owner="o",
    )
    assert plan.blocked == ["b"]
    assert journal.get_step(run_id, "b")["error_code"] == "UNKNOWN_EFFECT"


def test_unparsable_spec_is_treated_as_unknown_effect(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    run_id = journal.create_run(spec="{}", spec_sha256="a" * 64)["run_id"]
    journal.add_step(run_id, "x")
    crash(journal, clock, run_id, [("x", "unknown")])
    reconcile(journal)

    plan = prepare_resume(journal, run_id, write_policy="verify", verifier=lambda *a: True)
    # an unknown class is never auto-replayed even with a permissive verifier
    assert plan.blocked == ["x"]


# -- guards -----------------------------------------------------------------


def test_prepare_resume_requires_interrupted_run(tmp_path):
    journal = make_journal(tmp_path)
    run_id = make_run(journal, [{"id": "a", "kind": "read"}])
    with pytest.raises(ReconcileError):
        prepare_resume(journal, run_id)  # still queued


def test_prepare_resume_ownership_guard(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    run_id = make_run(journal, [{"id": "a", "kind": "read"}])
    crash(journal, clock, run_id, [("a", "none")])
    reconcile(journal)
    with pytest.raises(ReconcileError):
        prepare_resume(journal, run_id, context_id="other-ctx")
    with pytest.raises(ReconcileError):
        prepare_resume(journal, run_id, user_id="other-user")
    assert is_resumable(journal.get_run(run_id)) is True


# -- concurrent resume: claim before reset, no double execution ----------------


def test_concurrent_resume_claims_before_reset(tmp_path):
    # Two executors (two journals on one file) race to resume the same run.
    # prepare_resume claims BEFORE it resets anything, so exactly one wins and
    # the loser mutates nothing — no double attempt, no double run.
    clock = FakeClock()
    db = tmp_path / "wf.db"
    first = WorkflowJournal(db, clock=clock)
    run_id = make_run(first, [{"id": "a", "kind": "read"}, {"id": "b", "kind": "read"}])
    crash(first, clock, run_id, [("a", "none"), ("b", "none")])
    reconcile(first)
    second = WorkflowJournal(db, clock=clock)

    winner = prepare_resume(first, run_id, owner="owner-A")
    assert winner.owned is True
    with pytest.raises((ClaimConflict, ReconcileError)):
        prepare_resume(second, run_id, owner="owner-B")

    run = first.get_run(run_id)
    assert run["status"] == "running" and run["owner"] == "owner-A"
    assert first.get_step(run_id, "a")["status"] == "ready"
    # no second attempt was created by the losing caller
    assert first.load_attempts(run_id, "a")["total"] == 1


def test_reset_step_refuses_another_owners_running_attempt(tmp_path):
    clock = FakeClock()
    journal = make_journal(tmp_path, clock)
    run_id = make_run(journal, [{"id": "a", "kind": "read"}])
    crash(journal, clock, run_id, [("a", "none")])
    reconcile(journal)
    prepare_resume(journal, run_id, owner="owner-A")  # claims; step a -> ready
    journal.begin_attempt(run_id, "a", effect_status="none")  # owner-A starts it

    # a stale/other owner cannot reset the live attempt (fence), and a plain
    # caller cannot either (a running step is not terminal, I4)
    with pytest.raises(ClaimConflict):
        journal.reset_step(run_id, "a", owner="owner-B")
    with pytest.raises(InvalidTransition):
        journal.reset_step(run_id, "a")
    assert journal.get_step(run_id, "a")["status"] == "running"
    assert journal.load_attempts(run_id, "a")["total"] == 2  # crash attempt + the owner's new one
