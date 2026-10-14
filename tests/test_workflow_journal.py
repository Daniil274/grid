"""C2 tests: the durable workflow journal (docs/durable-workflows-plan.md §10).

Everything here runs locally — SQLite in a tmp dir, a fake clock, no model
calls. Covered: reopen/durability, atomic finish (attempt+step+event), the
write-ahead of effects (I3), compare-and-swap claims and leases (no double
execution), the status transition map (I4), integer micro-USD spend, scoped
and paginated queries, and unknown effects that survive a reopen and are
never downgraded or restarted by the journal itself (I9).
"""

from datetime import datetime, timedelta

import pytest

from core.workflows.journal import (
    ATTEMPT_OUTCOMES,
    RUN_STATUSES,
    STEP_STATUSES,
    ClaimConflict,
    InvalidTransition,
    JournalError,
    NotFoundError,
    RunExistsError,
    WorkflowJournal,
)


class FakeClock:
    """Deterministic wall clock; advancing it expires leases."""

    def __init__(self, start="2026-10-10T12:00:00+00:00"):
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_run(journal: WorkflowJournal, **overrides) -> dict:
    spec = {"name": "w", "goal": "g", "steps": [{"id": "s1", "kind": "read"}]}
    kwargs = dict(
        spec=spec,
        spec_sha256="a" * 64,
        context_id="ctx-1",
        user_id="user-1",
        budget_usd_micros=1_000_000,
    )
    kwargs.update(overrides)
    return journal.create_run(**kwargs)


# -- create / get ----------------------------------------------------------


def test_create_run_roundtrip_and_frozen_spec(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    spec = {"name": "deploy", "goal": "ship", "steps": [{"id": "a"}]}
    run = journal.create_run(
        spec=spec, spec_sha256="0f" * 32, context_id="c", user_id="u",
        budget_usd_micros=250, deadline_at="2026-10-11T00:00:00+00:00",
    )
    assert run["run_id"].startswith("wf-")
    assert run["status"] == "queued"
    assert run["spent_usd_micros"] == 0
    assert run["budget_usd_micros"] == 250
    assert run["context_id"] == "c" and run["user_id"] == "u"
    assert run["deadline_at"] == "2026-10-11T00:00:00+00:00"
    # the spec is stored as JSON text, verbatim enough to reload
    import json
    assert json.loads(run["spec_json"]) == spec
    # mutating the caller's dict afterwards changes nothing frozen
    spec["steps"].append({"id": "b"})
    assert len(json.loads(journal.get_run(run["run_id"])["spec_json"])["steps"]) == 1


def test_run_ids_unique_and_duplicate_run_id_refused(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    first = make_run(journal)
    assert len({first["run_id"], journal.new_run_id(), journal.new_run_id()}) == 3
    with pytest.raises(RunExistsError):
        journal.create_run(
            run_id=first["run_id"], spec="{}", spec_sha256="b" * 64,
        )


def test_get_run_missing_is_none(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    assert journal.get_run("wf-nothere") is None
    with pytest.raises(NotFoundError):
        journal.append_event("wf-nothere", "x")


# -- reopen / durability ----------------------------------------------------


def test_reopen_preserves_runs_steps_attempts_spend_and_events(tmp_path):
    db = tmp_path / "wf.db"
    clock = FakeClock()
    journal = WorkflowJournal(db, clock=clock)
    run = make_run(journal)
    journal.add_step(run["run_id"], "s1")
    journal.claim_run(run["run_id"], "engine-1")
    journal.set_step_status(run["run_id"], "s1", "ready")
    journal.begin_attempt(run["run_id"], "s1", effect_status="unknown", idempotency_key="k1")
    journal.add_spend(run["run_id"], 321)
    journal.close()  # "crash"

    reopened = WorkflowJournal(db, clock=clock)
    run2 = reopened.get_run(run["run_id"])
    assert run2["status"] == "running"
    assert run2["owner"] == "engine-1"
    assert run2["spent_usd_micros"] == 321
    assert run2["spec_sha256"] == "a" * 64
    steps = reopened.load_steps(run["run_id"])
    assert steps[0]["status"] == "running" and steps[0]["attempts"] == 1
    attempt = reopened.get_attempt(run["run_id"], "s1", 1)
    assert attempt["outcome"] == "started"      # the crash left it open
    assert attempt["effect_status"] == "unknown"  # the write-ahead of effects
    events = reopened.load_events(run["run_id"])
    names = [e["event"] for e in events["items"]]
    assert names == ["run_created", "run_claimed", "step_status_changed",
                     "attempt_started", "spend_added"]
    # the log is append-only: the next event continues the same sequence
    seq_before = events["items"][-1]["seq"]
    reopened.append_event(run["run_id"], "reopened", {"ok": True})
    assert reopened.load_events(run["run_id"])["items"][-1]["seq"] == seq_before + 1


def test_second_journal_on_the_same_file_sees_the_same_state(tmp_path):
    db = tmp_path / "wf.db"
    with WorkflowJournal(db) as first:
        run = make_run(first)
    second = WorkflowJournal(db)
    assert second.get_run(run["run_id"])["status"] == "queued"
    second.close()


# -- run status transitions ----------------------------------------------------


def test_run_status_directed_transitions_and_cas(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    run = make_run(journal)
    rid = run["run_id"]
    journal.set_run_status(rid, "running")
    journal.set_run_status(rid, "cancelling")
    journal.set_run_status(rid, "emergency_stopped")
    # terminal: nothing moves any more
    with pytest.raises(InvalidTransition):
        journal.set_run_status(rid, "queued")
    # forbidden jump on a fresh run
    other = make_run(journal)
    with pytest.raises(InvalidTransition):
        journal.set_run_status(other["run_id"], "completed")
    # CAS: the move only happens from the status the caller believes
    with pytest.raises(InvalidTransition):
        journal.set_run_status(other["run_id"], "running", expect="running")


def test_pause_and_resume_via_expected_status(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    run = make_run(journal)
    rid = run["run_id"]
    journal.set_run_status(rid, "running")
    journal.set_run_status(rid, "paused_budget", note="over budget")
    journal.set_run_status(rid, "running", note="budget raised")
    assert journal.get_run(rid)["status"] == "running"
    event = journal.load_events(rid)["items"][-1]
    assert event["event"] == "run_status_changed"
    assert event["payload"]["note"] == "budget raised"


def test_terminal_run_clears_owner_and_lease(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    run = make_run(journal)
    rid = run["run_id"]
    journal.claim_run(rid, "engine-1")
    journal.set_run_status(rid, "failed", final_report='{"failed": true}')
    row = journal.get_run(rid)
    assert row["owner"] is None and row["lease_expires_at"] is None
    assert row["final_report"] == '{"failed": true}'


# -- claims / leases -----------------------------------------------------------


def test_claim_run_once_then_conflict_then_takeover_after_expiry(tmp_path):
    clock = FakeClock()
    journal = WorkflowJournal(tmp_path / "wf.db", clock=clock)
    rid = make_run(journal)["run_id"]
    claimed = journal.claim_run(rid, "engine-1", lease_seconds=300)
    assert claimed["status"] == "running" and claimed["owner"] == "engine-1"
    # a second engine loses the compare-and-swap while the lease is alive
    with pytest.raises(ClaimConflict):
        journal.claim_run(rid, "engine-2", lease_seconds=300)
    # ... even with the same owner (idempotence is not assumed)
    with pytest.raises(ClaimConflict):
        journal.claim_run(rid, "engine-1", lease_seconds=300)
    # the lease expires; the takeover is allowed and renews it
    clock.advance(301)
    takeover = journal.claim_run(rid, "engine-2", lease_seconds=60)
    assert takeover["owner"] == "engine-2"
    assert takeover["lease_expires_at"] == (clock() + timedelta(seconds=60)).isoformat()


def test_claim_run_refused_from_terminal_status(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.set_run_status(rid, "running")
    journal.set_run_status(rid, "completed")
    with pytest.raises(ClaimConflict):
        journal.claim_run(rid, "engine-1")


def test_renew_lease_only_for_the_owner(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.claim_run(rid, "engine-1", lease_seconds=10)
    with pytest.raises(ClaimConflict):
        journal.renew_lease(rid, "engine-2", lease_seconds=10)
    renewed = journal.renew_lease(rid, "engine-1", lease_seconds=30)
    assert renewed["owner"] == "engine-1"
    journal.set_run_status(rid, "completed")
    with pytest.raises(ClaimConflict):  # no lease to renew on a dead run
        journal.renew_lease(rid, "engine-1", lease_seconds=30)


# -- steps -------------------------------------------------------------------


def test_step_status_transitions(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    journal.set_step_status(rid, "s1", "ready")
    journal.set_step_status(rid, "s1", "running")
    journal.set_step_status(rid, "s1", "completed", result_ref="evidence://1")
    step = journal.get_step(rid, "s1")
    assert step["status"] == "completed"
    assert step["result_ref"] == "evidence://1"
    assert step["started_at"] and step["finished_at"]
    # terminal step: no jumps, no re-running by status
    with pytest.raises(InvalidTransition):
        journal.set_step_status(rid, "s1", "running")
    # pending -> completed is not a status move the plan allows
    journal.add_step(rid, "s2")
    with pytest.raises(InvalidTransition):
        journal.set_step_status(rid, "s2", "completed")
    # pending -> skipped is (cancelled / failed dependency / optional)
    journal.set_step_status(rid, "s2", "skipped")
    with pytest.raises(InvalidTransition):
        journal.set_step_status(rid, "s2", "ready")


def test_step_cas_expect(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    journal.set_step_status(rid, "s1", "ready")
    with pytest.raises(InvalidTransition):
        journal.set_step_status(rid, "s1", "running", expect="pending")
    journal.set_step_status(rid, "s1", "running", expect="ready")


def test_add_step_is_idempotent_per_run_and_scoped(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    journal.add_step(rid, "s1")  # no error, still one row
    assert [s["step_id"] for s in journal.load_steps(rid)] == ["s1"]
    other = make_run(journal)["run_id"]
    assert journal.load_steps(other) == []
    with pytest.raises(NotFoundError):
        journal.add_step("wf-missing", "s1")


# -- attempts: the write-ahead of effects ---------------------------------------


def test_begin_attempt_committed_before_effect_and_blocks_double_execution(tmp_path):
    # two journals on one file: whoever commits first wins the CAS
    db = tmp_path / "wf.db"
    first = WorkflowJournal(db)
    rid = make_run(first)["run_id"]
    first.add_step(rid, "s1")
    first.set_step_status(rid, "s1", "ready")
    second = WorkflowJournal(db)

    winner = first.begin_attempt(rid, "s1", effect_status="unknown", idempotency_key="idem-1")
    assert winner["outcome"] == "started"
    assert winner["effect_status"] == "unknown"
    assert winner["started_at"] and winner["finished_at"] is None
    step = first.get_step(rid, "s1")
    assert step["status"] == "running" and step["attempts"] == 1

    with pytest.raises(ClaimConflict):  # the second executor loses
        second.begin_attempt(rid, "s1", effect_status="unknown")

    # a crash happens *after* begin but *before* the effect: the reopened
    # journal still shows the open unknown attempt (I3, the whole point)
    second.close()
    first.close()
    reopened = WorkflowJournal(db)
    attempt = reopened.get_attempt(rid, "s1", 1)
    assert attempt["outcome"] == "started" and attempt["effect_status"] == "unknown"
    with pytest.raises(ClaimConflict):  # still refused after the reopen
        reopened.begin_attempt(rid, "s1", effect_status="unknown")


def test_begin_attempt_rejects_confirmed_and_allocates_numbers(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    with pytest.raises(ValueError):
        journal.begin_attempt(rid, "s1", effect_status="confirmed")
    journal.begin_attempt(rid, "s1")  # non-effectful: effect_status stays none
    assert journal.get_attempt(rid, "s1", 1)["effect_status"] == "none"
    journal.finish_attempt(rid, "s1", 1, "interrupted")  # reconcile's move
    second = journal.begin_attempt(rid, "s1")  # resume: a *new* attempt
    assert second["attempt"] == 2
    assert journal.get_step(rid, "s1")["attempts"] == 2


def test_finish_attempt_is_atomic_attempt_step_and_event(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    journal.begin_attempt(rid, "s1", effect_status="unknown")
    before = journal.load_events(rid)["total"]

    row = journal.finish_attempt(
        rid, "s1", 1, "completed", effect_status="confirmed",
        result_ref="evidence://42",
    )
    assert row["outcome"] == "completed"
    assert row["effect_status"] == "confirmed"
    assert row["finished_at"]
    step = journal.get_step(rid, "s1")
    assert step["status"] == "completed"
    assert step["result_ref"] == "evidence://42"
    assert step["finished_at"] == row["finished_at"]
    # exactly one event landed, in the same transaction
    events = journal.load_events(rid)
    assert events["total"] == before + 1
    assert events["items"][-1]["event"] == "attempt_finished"
    assert events["items"][-1]["payload"]["effect_status"] == "confirmed"

    # finishing a finished attempt is refused, loudly
    with pytest.raises(InvalidTransition):
        journal.finish_attempt(rid, "s1", 1, "failed")
    with pytest.raises(NotFoundError):
        journal.finish_attempt(rid, "s1", 9, "completed")


def test_finish_attempt_outcomes_map_to_step_status(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    journal.begin_attempt(rid, "s1")
    journal.finish_attempt(rid, "s1", 1, "timed_out", error_code="TIMEOUT",
                           error_detail="slot deadline passed")
    step = journal.get_step(rid, "s1")
    assert step["status"] == "failed"      # timed_out attempt -> failed step
    assert step["error_code"] == "TIMEOUT"
    with pytest.raises(ValueError):
        journal.finish_attempt(rid, "s2", 1, "started")  # not a finish outcome


def test_unknown_effect_survives_reopen_and_is_never_downgraded(tmp_path):
    db = tmp_path / "wf.db"
    journal = WorkflowJournal(db)
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    journal.begin_attempt(rid, "s1", effect_status="unknown")
    # a crash mid-write: reconcile marks the attempt interrupted, unknown stays
    journal.finish_attempt(rid, "s1", 1, "interrupted")
    journal.set_run_status(rid, "interrupted")
    journal.close()

    reopened = WorkflowJournal(db)
    attempt = reopened.get_attempt(rid, "s1", 1)
    assert attempt["outcome"] == "interrupted"
    assert attempt["effect_status"] == "unknown"  # no automatic restart, no reset
    # the journal refuses to downgrade unknown -> none (I9)
    with pytest.raises(InvalidTransition):
        reopened.finish_attempt(rid, "s1", 1, "completed", effect_status="none")
    # verify is the only way out, and it is the caller's explicit call
    reopened.begin_attempt(rid, "s1")  # resume path: new attempt, old stays unknown
    reopened.finish_attempt(rid, "s1", 2, "completed", effect_status="confirmed")
    assert reopened.get_attempt(rid, "s1", 1)["effect_status"] == "unknown"
    assert reopened.get_attempt(rid, "s1", 2)["effect_status"] == "confirmed"


def test_load_attempts_pagination(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    for _ in range(3):
        journal.begin_attempt(rid, "s1", effect_status="unknown")
        journal.finish_attempt(rid, "s1", journal.get_step(rid, "s1")["attempts"],
                               "failed", error_code="TRANSIENT_ERROR")
        journal.reset_step(rid, "s1")  # the engine's explicit retry decision
    page = journal.load_attempts(rid, "s1", limit=2, offset=1)
    assert page["total"] == 3
    assert [a["attempt"] for a in page["items"]] == [2, 3]
    only = journal.load_attempts(rid, "s1", limit=10)
    assert only["total"] == 3 and len(only["items"]) == 3
    whole_run = journal.load_attempts(rid, limit=10)
    assert whole_run["total"] == 3
    assert all(a["step_id"] == "s1" for a in whole_run["items"])
    journal.add_step(rid, "s2")  # a step with no attempts yet
    assert journal.load_attempts(rid, "s2")["items"] == []


def test_reset_step_is_explicit_and_journaled(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    journal.set_step_status(rid, "s1", "ready")
    # a non-terminal step cannot be "reset" — it moves through the normal map
    with pytest.raises(InvalidTransition):
        journal.reset_step(rid, "s1")
    journal.begin_attempt(rid, "s1")
    journal.finish_attempt(rid, "s1", 1, "failed", error_code="TRANSIENT_ERROR",
                           error_detail="boom", result_ref="evidence://1")
    reset = journal.reset_step(rid, "s1")
    assert reset["status"] == "ready"
    assert reset["result_ref"] is None and reset["error_code"] is None
    assert reset["finished_at"] is None
    # completed and skipped steps are never resettable (I4: an effect stands)
    journal.begin_attempt(rid, "s1")
    journal.finish_attempt(rid, "s1", 2, "completed")
    with pytest.raises(InvalidTransition):
        journal.reset_step(rid, "s1")
    journal.add_step(rid, "s2")
    journal.set_step_status(rid, "s2", "skipped")
    with pytest.raises(InvalidTransition):
        journal.reset_step(rid, "s2")
    names = [e["event"] for e in journal.load_events(rid)["items"]]
    assert names.count("step_reset") == 1  # the reset is on the record too


# -- spend ---------------------------------------------------------------------


def test_add_spend_integer_micros_only(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    assert journal.add_spend(rid, 1_500_000) == 1_500_000
    assert journal.add_spend(rid, 500_000) == 2_000_000
    assert journal.get_run(rid)["spent_usd_micros"] == 2_000_000
    for bad in (1.5, "100", -1, True, None):
        with pytest.raises(ValueError):
            journal.add_spend(rid, bad)
    # a rejected add changes nothing
    assert journal.get_run(rid)["spent_usd_micros"] == 2_000_000
    with pytest.raises(NotFoundError):
        journal.add_spend("wf-none", 1)


def test_budget_must_be_integer_micros_or_absent(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    with pytest.raises(ValueError):
        journal.create_run(spec="{}", spec_sha256="0" * 64, budget_usd_micros=10.5)
    with pytest.raises(ValueError):
        journal.create_run(spec="{}", spec_sha256="0" * 64, budget_usd_micros=-5)
    assert journal.create_run(spec="{}", spec_sha256="0" * 64)["budget_usd_micros"] is None


# -- scoped, paginated queries ---------------------------------------------------


def test_load_runs_scoping_statuses_and_pagination(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    ids = []
    for i in range(5):
        run = make_run(
            journal,
            context_id="ctx-1" if i < 3 else "ctx-2",
            user_id="alice" if i % 2 == 0 else "bob",
        )
        ids.append(run["run_id"])
    journal.set_run_status(ids[0], "running")
    journal.set_run_status(ids[2], "running")
    journal.set_run_status(ids[2], "failed")

    all_runs = journal.load_runs()
    assert all_runs["total"] == 5 and len(all_runs["items"]) == 5
    alice = journal.load_runs(user_id="alice")
    assert alice["total"] == 3
    assert all(r["user_id"] == "alice" for r in alice["items"])
    ctx = journal.load_runs(context_id="ctx-2")
    assert [r["run_id"] for r in ctx["items"]] == [ids[4], ids[3]]
    live = journal.load_runs(statuses=["queued", "running"])
    assert live["total"] == 4
    page = journal.load_runs(limit=2, offset=2)
    assert page["total"] == 5
    assert len(page["items"]) == 2
    assert [r["run_id"] for r in page["items"]] == \
        [r["run_id"] for r in all_runs["items"][2:4]]
    with pytest.raises(ValueError):
        journal.load_runs(statuses=["nope"])


def test_load_events_pagination_and_payloads(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    for i in range(4):
        journal.append_event(rid, "tick", {"i": i})
    page = journal.load_events(rid, limit=2, offset=2)
    assert page["total"] == 5  # run_created + 4 ticks
    assert [e["payload"]["i"] for e in page["items"]] == [1, 2]  # after run_created and tick 0
    first = journal.load_events(rid)["items"][0]
    assert first["event"] == "run_created"
    assert first["payload"]["spec_sha256"] == "a" * 64
    # non-JSON payloads stay strings
    journal.append_event(rid, "note", "plain text")
    last = journal.load_events(rid, limit=1, offset=5)["items"][0]
    assert last["payload"] == "plain text"


# -- concurrency -----------------------------------------------------------------


def test_concurrent_writers_serialize_without_loss(tmp_path):
    import threading

    db = tmp_path / "wf.db"
    with WorkflowJournal(db) as journal:
        rid = make_run(journal)["run_id"]
    errors = []

    def spend_loop(which: int) -> None:
        journal = None
        try:
            journal = WorkflowJournal(db)  # one writer process: its own connection
            for _ in range(20):
                journal.add_spend(rid, 1)
        except Exception as error:  # pragma: no cover - surfaced via errors
            errors.append(error)
        finally:
            if journal is not None:
                journal.close()

    threads = [threading.Thread(target=spend_loop, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert WorkflowJournal(db).get_run(rid)["spent_usd_micros"] == 60  # all increments survived

    def event_loop(which: int) -> None:
        journal = None
        try:
            journal = WorkflowJournal(db)
            for _ in range(10):
                journal.append_event(rid, "concurrent", {"w": which})
        except Exception as error:  # pragma: no cover
            errors.append(error)
        finally:
            if journal is not None:
                journal.close()

    threads = [threading.Thread(target=event_loop, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    events = WorkflowJournal(db).load_events(rid, limit=100)
    assert events["total"] == 1 + 60 + 30  # run_created + spend + concurrent events
    seqs = [e["seq"] for e in events["items"]]
    assert seqs == sorted(seqs)  # append-only order holds
    assert WorkflowJournal(db).get_run(rid)["spent_usd_micros"] == 60


def test_status_maps_are_the_planned_ones():
    # guards the row vocabulary against accidental drift (§5)
    assert RUN_STATUSES == ("queued", "running", "paused_budget", "cancelling",
                            "interrupted", "completed", "failed", "emergency_stopped")
    assert STEP_STATUSES == ("pending", "ready", "running", "completed",
                             "failed", "interrupted", "skipped")
    assert ATTEMPT_OUTCOMES == ("started", "completed", "failed", "timed_out", "interrupted")


def test_all_journal_errors_share_one_base(tmp_path):
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    for error in (ClaimConflict, InvalidTransition, NotFoundError, RunExistsError):
        assert issubclass(error, JournalError)
    with pytest.raises(JournalError):
        journal.claim_run(rid, "x", from_statuses=("completed",))


# -- R6 atomic finish, owner fencing, same-owner reclaim, live reconcile -------


def test_finish_attempt_with_evidence_is_atomic_on_fault_injection(tmp_path, monkeypatch):
    # R6: a fault while writing the evidence must roll back the whole finish.
    # finish_attempt(result_payload=...) commits the attempt, the step row and
    # the step_result evidence in ONE transaction: a fault in the middle cannot
    # leave a completed step without its dataflow payload.
    journal = WorkflowJournal(tmp_path / "wf.db")
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    journal.begin_attempt(rid, "s1", effect_status="unknown")
    real_event = journal._event

    def boom(db, run_id, event, payload):
        if event == "step_result":
            raise RuntimeError("disk full while writing evidence")
        return real_event(db, run_id, event, payload)

    monkeypatch.setattr(journal, "_event", boom)
    with pytest.raises(RuntimeError):
        journal.finish_attempt(
            rid, "s1", 1, "completed", effect_status="confirmed",
            result_ref="artifact://x",
            result_payload={"step_id": "s1", "ok": True, "result_ref": "artifact://x"})

    # everything rolled back together: no half-finished attempt, no evidence
    assert journal.get_attempt(rid, "s1", 1)["outcome"] == "started"
    assert journal.get_step(rid, "s1")["status"] == "running"
    assert [e for e in journal.load_events(rid)["items"] if e["event"] == "step_result"] == []

    # once the fault is gone the same finish succeeds atomically
    monkeypatch.undo()
    journal.finish_attempt(
        rid, "s1", 1, "completed", effect_status="confirmed",
        result_ref="artifact://x",
        result_payload={"step_id": "s1", "ok": True, "result_ref": "artifact://x"})
    assert journal.get_step(rid, "s1")["status"] == "completed"
    events = journal.load_events(rid)["items"]
    assert [e["event"] for e in events].count("step_result") == 1


def test_stale_owner_write_is_refused(tmp_path):
    # Fencing: after a takeover the old owner's step/attempt writes fail loudly.
    clock = FakeClock()
    journal = WorkflowJournal(tmp_path / "wf.db", clock=clock)
    rid = make_run(journal)["run_id"]
    journal.add_step(rid, "s1")
    journal.claim_run(rid, "engine-1", lease_seconds=10.0)
    journal.begin_attempt(rid, "s1", effect_status="unknown", owner="engine-1")
    clock.advance(11.0)  # engine-1's lease is dead: engine-2 takes over
    journal.claim_run(rid, "engine-2", lease_seconds=10.0)

    with pytest.raises(ClaimConflict):
        journal.finish_attempt(rid, "s1", 1, "completed", owner="engine-1")
    with pytest.raises(ClaimConflict):
        journal.reset_step(rid, "s1", owner="engine-1")
    # the current owner still writes
    journal.finish_attempt(rid, "s1", 1, "completed", owner="engine-2")
    assert journal.get_step(rid, "s1")["status"] == "completed"


def test_claim_run_same_owner_reclaim_is_idempotent(tmp_path):
    clock = FakeClock()
    journal = WorkflowJournal(tmp_path / "wf.db", clock=clock)
    rid = make_run(journal)["run_id"]
    journal.claim_run(rid, "engine-1", lease_seconds=10.0)
    # default stays strict (existing behaviour): a second claim conflicts
    with pytest.raises(ClaimConflict):
        journal.claim_run(rid, "engine-1", lease_seconds=10.0)
    # the resume path opts in: the same live owner refreshes instead of failing
    reclaimed = journal.claim_run(
        rid, "engine-1", lease_seconds=30.0, allow_same_owner=True)
    assert reclaimed["owner"] == "engine-1"
    assert reclaimed["lease_expires_at"] == (clock() + timedelta(seconds=30)).isoformat()
    # a different owner is still refused even with the flag
    with pytest.raises(ClaimConflict):
        journal.claim_run(rid, "engine-2", lease_seconds=30.0, allow_same_owner=True)


def test_interrupt_run_never_touches_a_live_run(tmp_path):
    clock = FakeClock()
    journal = WorkflowJournal(tmp_path / "wf.db", clock=clock)
    rid = make_run(journal)["run_id"]
    journal.claim_run(rid, "live-owner", lease_seconds=100.0)
    with pytest.raises(ClaimConflict):
        journal.interrupt_run(rid, expect="running", owner="live-owner")
    assert journal.get_run(rid)["status"] == "running"
    # once the lease is dead the same call with the expected owner succeeds
    clock.advance(200.0)
    row = journal.interrupt_run(rid, expect="running", owner="live-owner")
    assert row["status"] == "interrupted" and row["owner"] is None
