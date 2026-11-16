"""The task lifecycle: who owns a claimed task, and how the run actually ended.

These tests hold the guarantees the runtime now makes around a *confirmed*
tracker claim (core.tracker_lifecycle):

* a binding belongs to a concrete run, never to a user;
* a live foreign owner is refused (claim, update, close), a stale one is taken
  over in the journal before bd is awaited, and later requests by the stale
  run cannot finish what it lost (not a fence for already in-flight bd writes);
* a run's own heartbeat keeps a long run live, and stops with the run;
* the runtime's ``finally`` records timeout / attempt limit / cancellation /
  not-started honestly - never ``completed`` from a model's text;
* an executor's preparation (init_tools) is bound to the executor's run and
  settled by the same finally as the model's own claims;
* an early failure of that preparation still settles what it claimed.

Most tests use a temporary directory and the real journal. The integration
tests at the end create a real temporary bd tracker and drive the same journal
around real bd claims/closes (skipped when ``bd`` is not installed).
"""

import asyncio
import json
import shutil
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import agents
import pytest
import yaml
from agents.exceptions import MaxTurnsExceeded
from agents import SQLiteSession

from core import tracker_lifecycle
from core.agent_factory import AgentFactory
from core.config import Config
from core.factory.run_context import GridRunContext
from core.factory.turns import new_run_id


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def entry(directory):
    return {"directory": str(directory), "bead_id": "t-1"}


DEFAULT_LEASE = 900.0


def bind(directory, run_id="run-a", bead_id="t-1", lease=DEFAULT_LEASE, now=None):
    return tracker_lifecycle.bind(
        str(directory),
        bead_id,
        run_id=run_id,
        actor="grid:alice",
        agent="worker",
        context_id="ctx-1",
        lease_seconds=lease,
        now=now,
    )
def run_ctx(directory, run_id="run-a", bead_id="t-1"):
    return SimpleNamespace(
        run_id=run_id,
        task_bindings=[{"directory": str(directory), "bead_id": bead_id}],
    )


def binder_ctx(directory, run_id="run-b", bead_id="t-1"):
    """The context a tracker tool sees: the host's raw run context."""
    raw = SimpleNamespace(
        factory=SimpleNamespace(container_id=None),
        user_id="alice",
        run_id=run_id,
        parent_run_id=None,
        agent_id="worker",
        context_id="ctx-b",
        task_bindings=[],
    )
    return SimpleNamespace(context=raw, tool_call_id="call-1")


# ---------------------------------------------------------------------------
# ownership: one run, never a user
# ---------------------------------------------------------------------------


def test_a_live_foreign_claim_cannot_be_taken_or_resumed(tmp_path):
    first = bind(tmp_path, run_id="run-a")

    with pytest.raises(tracker_lifecycle.LifecycleError) as refused:
        bind(tmp_path, run_id="run-b")

    assert refused.value.code == "tracker_claimed_elsewhere"
    assert refused.value.retryable
    # The owner is unchanged: the loser made no journal write.
    assert tracker_lifecycle.lookup(str(tmp_path), "t-1")["owner_run_id"] == "run-a"
    assert first["state"] == tracker_lifecycle.CLAIMED


def test_an_expired_binding_is_taken_over_and_the_takeover_is_recorded(tmp_path):
    started = time.time() - 1000
    bind(tmp_path, run_id="run-a", lease=10, now=started)
    assert not tracker_lifecycle.is_live(
        tracker_lifecycle.lookup(str(tmp_path), "t-1")
    )

    taken = bind(tmp_path, run_id="run-b", now=time.time())

    assert taken["owner_run_id"] == "run-b"
    assert taken["takeover_from"] == "run-a@claimed"
    # A report or close started by the stale executor is refused.
    with pytest.raises(tracker_lifecycle.LifecycleError) as refused:
        tracker_lifecycle.finish(
            str(tmp_path), "t-1", "run-a", outcome=tracker_lifecycle.OUTCOME_CLOSED
        )
    assert refused.value.code == "tracker_not_owner"


def test_only_the_owner_finishes_and_only_an_observed_outcome_is_recorded(tmp_path):
    bind(tmp_path, run_id="run-a", lease=60)

    # run-b owns nothing here: it is not allowed to finish run-a's task.
    with pytest.raises(tracker_lifecycle.LifecycleError):
        tracker_lifecycle.finish(str(tmp_path), "t-1", "run-b", outcome="timeout")

    for outcome, state in (
        ("timeout", tracker_lifecycle.INTERRUPTED),
        ("attempt_limit", tracker_lifecycle.INTERRUPTED),
        ("cancelled", tracker_lifecycle.INTERRUPTED),
        ("not_started", tracker_lifecycle.INTERRUPTED),
        ("completed", tracker_lifecycle.NEEDS_REVIEW),  # a text answer is not a close
        ("closed", tracker_lifecycle.COMPLETED),
    ):
        # Each case gets its own task so the previous terminal state is not reused.
        bead = f"t-{outcome}"
        bind(tmp_path, run_id="run-a", bead_id=bead)
        result = tracker_lifecycle.finish(
            str(tmp_path), bead, "run-a", outcome=outcome
        )
        assert result["state"] == state
        assert result["outcome"] == outcome


def test_a_terminal_binding_is_not_finished_twice(tmp_path):
    bind(tmp_path, run_id="run-a")
    tracker_lifecycle.finish(
        str(tmp_path), "t-1", "run-a", outcome=tracker_lifecycle.OUTCOME_CLOSED
    )
    again = tracker_lifecycle.finish(
        str(tmp_path), "t-1", "run-a", outcome=tracker_lifecycle.OUTCOME_TIMEOUT
    )
    assert again["state"] == tracker_lifecycle.COMPLETED
    assert again["outcome"] == "closed"


def test_a_run_identity_is_unique_per_real_executor():
    ids = {new_run_id("exec") for _ in range(500)}
    assert len(ids) == 500
    assert all(value.startswith("exec-") for value in ids)


# ---------------------------------------------------------------------------
# the run's heartbeat
# ---------------------------------------------------------------------------


def test_the_lease_keeper_keeps_a_long_run_live_and_stops_with_it(tmp_path):
    started = time.time()
    bind(tmp_path, run_id="run-a", lease=0.6, now=started)
    assert tracker_lifecycle.is_live(tracker_lifecycle.lookup(str(tmp_path), "t-1"))

    keeper = tracker_lifecycle.LeaseKeeper(
        run_ctx(tmp_path), lease_seconds=0.6, interval=0.05
    ).start()
    try:
        time.sleep(1.0)  # well past the original lease
        view = tracker_lifecycle.lookup(str(tmp_path), "t-1")
        assert view["state"] == tracker_lifecycle.RUNNING
        assert view["lease_expires_at"] > started + 0.6
        assert tracker_lifecycle.is_live(view)
        assert keeper.alive
    finally:
        keeper.stop()
    assert not keeper.alive
    # No heartbeat after the run ended: the binding ages out instead of
    # pretending a dead run is still working.
    time.sleep(0.8)
    assert not tracker_lifecycle.is_live(
        tracker_lifecycle.lookup(str(tmp_path), "t-1")
    )


def test_the_lease_keeper_never_touches_a_task_taken_over(tmp_path):
    bind(tmp_path, run_id="run-a", lease=60)
    keeper = tracker_lifecycle.LeaseKeeper(
        run_ctx(tmp_path, run_id="run-b"), lease_seconds=60, interval=0.05
    ).start()
    try:
        time.sleep(0.15)
    finally:
        keeper.stop()
    # run-b owns nothing here: no update to run-a's binding.
    assert tracker_lifecycle.lookup(str(tmp_path), "t-1")["owner_run_id"] == "run-a"


def test_a_run_without_identity_has_nothing_to_keep_alive():
    from core.factory.turns import start_lease_keeper

    assert start_lease_keeper(SimpleNamespace(run_id=None)) is None


# ---------------------------------------------------------------------------
# the runtime's finally: how the run ended
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, output="Done.", failure=None):
        self.final_output = output
        self._failure = failure

    async def stream_events(self):
        if self._failure is not None:
            raise self._failure
        if False:  # pragma: no cover - makes this an async generator
            yield None


class FakeRunner:
    """A scripted SDK runner: first result as given, later calls a report."""

    def __init__(self, first):
        self.first = first
        self.calls = []

    def run_streamed(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return self.first
        return FakeResult("the report of the work so far")


@pytest.fixture
def factory(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    config = {
        "settings": {
            "default_agent": "worker",
            "max_turns": 5,
            "agent_timeout": 30,
            "working_directory": str(work),
            "logs_directory": str(tmp_path / "logs"),
            "mcp_enabled": False,
            "agent_logging": {"enabled": False},
        },
        "providers": {"p": {"name": "p", "base_url": "https://llm.test/v1", "api_key": "test"}},
        "models": {"m": {"name": "m", "provider": "p", "context_window": 100000}},
        "prompt_templates": {"base": "You help."},
        "agents": {
            "worker": {"name": "Worker", "model": "m", "tools": [], "base_prompt": "base"},
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    factory = AgentFactory(Config(str(path)), tracing_level=None)
    sessions = {}
    factory._get_agent_session = lambda key, context_id: sessions.setdefault(
        (key, context_id), SQLiteSession(f"{key}-{context_id}")
    )
    return factory


def fake_agent():
    return SimpleNamespace(
        name="Worker",
        model_settings=SimpleNamespace(),
        clone=lambda **kwargs: fake_agent(),
    )


def use_runner(monkeypatch, runner):
    monkeypatch.setattr("core.factory.turns.get_runner", lambda: runner)


def observer():
    return SimpleNamespace(handle_event=lambda *args, **kwargs: "")


async def test_an_answer_without_a_confirmed_close_is_needs_review(factory, monkeypatch, tmp_path):
    bind(tmp_path, run_id="exec-1", lease=60)
    use_runner(monkeypatch, FakeRunner(FakeResult("I did it, trust me.")))

    await factory.run_agent_object(
        fake_agent(),
        "do it",
        context_id="ctx-1",
        run_id="exec-1",
        task_bindings=[entry(tmp_path)],
        stream_observer=observer(),
        timeout=30,
    )

    view = tracker_lifecycle.lookup(str(tmp_path), "t-1")
    assert view["state"] == tracker_lifecycle.NEEDS_REVIEW
    assert view["outcome"] == "completed"
    assert view["finished_at"]
    # The model's text never becomes a tracker close.
    assert view["state"] != tracker_lifecycle.COMPLETED


async def test_a_cancelled_run_leaves_the_task_interrupted(factory, monkeypatch, tmp_path):
    bind(tmp_path, run_id="exec-1", lease=60)

    class Hanging(FakeResult):
        async def stream_events(self):
            await asyncio.Event().wait()
            if False:  # pragma: no cover
                yield None

    use_runner(monkeypatch, FakeRunner(Hanging()))
    task = asyncio.create_task(
        factory.run_agent_object(
            fake_agent(),
            "do it",
            context_id="ctx-1",
            run_id="exec-1",
            task_bindings=[entry(tmp_path)],
            stream_observer=observer(),
            timeout=30,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    view = tracker_lifecycle.lookup(str(tmp_path), "t-1")
    assert view["state"] == tracker_lifecycle.INTERRUPTED
    assert view["outcome"] == "cancelled"


async def test_an_attempt_limit_is_recorded_as_interrupted(factory, monkeypatch, tmp_path):
    bind(tmp_path, run_id="exec-1", lease=60)
    use_runner(monkeypatch, FakeRunner(FakeResult(failure=MaxTurnsExceeded("turn limit"))))

    outcome = await factory.run_agent_object(
        fake_agent(),
        "do it",
        context_id="ctx-1",
        run_id="exec-1",
        task_bindings=[entry(tmp_path)],
        stream_observer=observer(),
        timeout=30,
    )

    assert outcome.stopped is not None
    view = tracker_lifecycle.lookup(str(tmp_path), "t-1")
    assert view["state"] == tracker_lifecycle.INTERRUPTED
    assert view["outcome"] == "attempt_limit"


async def test_a_top_level_turn_settles_what_it_claimed(factory, monkeypatch, tmp_path):
    """The turn's finally settles a claim made while its model ran."""

    class ClaimingRunner(FakeRunner):
        def _claim(self, ctx):
            # What a tracker claim inside the turn does: bind to the turn run.
            tracker_lifecycle.bind(
                str(tmp_path), "t-1", run_id=ctx.run_id, actor="grid:alice"
            )
            tracker_lifecycle.remember(ctx, str(tmp_path), "t-1")

        def run_streamed(self, **kwargs):
            self._claim(kwargs["context"])
            return FakeResult("I did it.")

        async def run(self, agent, run_input, *, context, max_turns, session, run_config=None):
            self._claim(context)
            return SimpleNamespace(final_output="I did it.", new_items=[])

    use_runner(monkeypatch, ClaimingRunner(None))
    answer = await factory.run_agent("worker", "do it", context_id="ctx-1")
    assert "I did it." in answer

    view = tracker_lifecycle.lookup(str(tmp_path), "t-1")
    assert view["owner_run_id"].startswith("turn-")
    assert view["state"] == tracker_lifecycle.NEEDS_REVIEW
    assert view["outcome"] == "completed"


# ---------------------------------------------------------------------------
# the executor's preparation shares its run identity
# ---------------------------------------------------------------------------


def test_init_tools_run_under_the_executor_run_and_share_its_bindings():
    factory = object.__new__(AgentFactory)
    factory.container_id = None
    factory._stream_observer = None
    factory.get_active_context_id = lambda: "ctx-conversation"
    parent_bindings = [{"directory": "/ws", "bead_id": "t-1"}]
    parent = GridRunContext(
        factory=factory,
        context_id="ctx-conversation",
        user_id="alice",
        run_id="exec-1",
        parent_run_id="turn-1",
        task_bindings=parent_bindings,
    )

    ctx = factory._init_tool_context("executor", None, parent)

    assert ctx.run_id == "exec-1"
    assert ctx.parent_run_id == "turn-1"
    # The same list, not a copy: one finally settles preparation and model.
    assert ctx.task_bindings is parent_bindings
    # Everything else still comes from the caller.
    assert ctx.user_id == "alice"
    assert ctx.context_id == "ctx-conversation"


async def test_a_preparation_failure_still_settles_what_it_claimed(factory, tmp_path):
    """The early return of the Agent tool must not leave a claim running."""
    from core.factory.preparation import FAILED, Step, StepOutcome
    from core.factory.auto_run import PreparationResult

    async def partial_preparation(init_tools, pool, run_ctx):
        # A first step really claimed the task, then a later step failed.
        tracker_lifecycle.bind(
            str(tmp_path), "t-1", run_id=run_ctx.run_id, actor="grid:alice"
        )
        tracker_lifecycle.remember(run_ctx, str(tmp_path), "t-1")
        failed = StepOutcome(Step(index=1, name="beads_show", spec={"name": "beads_show"}), FAILED, detail="no database")
        return PreparationResult("", [failed], phase="init_tools", required=True)

    factory._execute_init_tools = partial_preparation
    sub_agent = SimpleNamespace(name="worker", tools=[], instructions="x")
    tool = factory._create_context_aware_agent_tool(
        "worker", sub_agent, "call_worker", "hand a task to worker"
    )
    raw = SimpleNamespace(
        factory=factory,
        context_id="ctx-conversation",
        user_id="alice",
        pipeline_id=None,
        step_id=None,
        action_state=None,
        action_depth=0,
        system_access_active=False,
        stream_observer=None,
        run_control=None,
        run_id="turn-1",
        parent_run_id=None,
        task_bindings=[],
    )
    ctx = SimpleNamespace(context=raw, tool_call_id="call-1")

    out = await tool.on_invoke_tool(
        ctx,
        json.dumps(
            {
                "input": "do the task",
                "init_tools": json.dumps([{"name": "beads_show", "parameters": {"bead_id": "t-1"}}]),
            }
        ),
    )

    assert out.startswith("❌ Agent 'worker' did not start")
    view = tracker_lifecycle.lookup(str(tmp_path), "t-1")
    assert view["state"] == tracker_lifecycle.INTERRUPTED
    assert view["outcome"] == "not_started"
    # It is not a sticky claim: its lease runs out, so a next run may carry on.
    assert not tracker_lifecycle.is_live(view)


# ---------------------------------------------------------------------------
# the write guard takes over a stale task before bd is awaited
# ---------------------------------------------------------------------------


async def test_a_live_foreign_owner_still_refuses_a_write(tmp_path):
    from tools import beads_tools

    bind(tmp_path, run_id="run-a", lease=60)
    refusal = await beads_tools._guard_owner(binder_ctx(tmp_path), str(tmp_path), "t-1")
    assert refusal and refusal["code"] == "tracker_not_owner"
    assert tracker_lifecycle.lookup(str(tmp_path), "t-1")["owner_run_id"] == "run-a"


async def test_an_expired_foreign_task_is_fenced_by_a_takeover_before_the_write(tmp_path):
    from tools import beads_tools

    bind(tmp_path, run_id="run-a", lease=10, now=time.time() - 1000)
    ctx = binder_ctx(tmp_path)
    assert await beads_tools._guard_owner(ctx, str(tmp_path), "t-1") is None

    view = tracker_lifecycle.lookup(str(tmp_path), "t-1")
    assert view["owner_run_id"] == "run-b"
    assert view["takeover_from"] == "run-a@claimed"
    # The write this run is about to make is owned and will be settled by it.
    assert ctx.context.task_bindings == [entry(tmp_path)]

    with pytest.raises(tracker_lifecycle.LifecycleError):
        tracker_lifecycle.finish(str(tmp_path), "t-1", "run-a", outcome="closed")


async def test_a_taken_over_task_is_settled_by_the_new_owners_finally(tmp_path):
    from tools import beads_tools

    bind(tmp_path, run_id="run-a", lease=10, now=time.time() - 1000)
    ctx = binder_ctx(tmp_path)
    await beads_tools._guard_owner(ctx, str(tmp_path), "t-1")

    written = tracker_lifecycle.settle_bindings(
        ctx.context.task_bindings, run_id="run-b", outcome="timeout"
    )

    assert [view["state"] for view in written] == [tracker_lifecycle.INTERRUPTED]
    assert tracker_lifecycle.lookup(str(tmp_path), "t-1")["owner_run_id"] == "run-b"


# ---------------------------------------------------------------------------
# what the caller/UI reads is the journal, never an invented claim
# ---------------------------------------------------------------------------


def test_binding_views_are_read_from_the_journal_only(tmp_path):
    from tools.orchestrator_tools import _task_binding_views

    bind(tmp_path, run_id="exec-1", lease=60)
    tracker_lifecycle.finish(str(tmp_path), "t-1", "exec-1", outcome="timeout")

    views = _task_binding_views([entry(tmp_path), {"directory": str(tmp_path), "bead_id": "t-missing"}])
    assert [view["bead_id"] for view in views] == ["t-1"]
    assert views[0]["state"] == tracker_lifecycle.INTERRUPTED
    assert views[0]["owner_run_id"] == "exec-1"
    # No journal entry -> no receipt, not a fabricated "claimed".
    assert _task_binding_views([{"directory": "/does/not/exist", "bead_id": "t-1"}]) == []


# ---------------------------------------------------------------------------
# real temporary bd: the journal around real claims and closes
# ---------------------------------------------------------------------------

bd_required = pytest.mark.skipif(shutil.which("bd") is None, reason="bd is not available here")


@bd_required
def test_a_real_interrupted_claim_is_recoverable_and_closes_once(tmp_path):
    from core.tracker_gateway import Backend, execute

    bd = shutil.which("bd")
    backend = Backend.host(bd)
    env = {"BEADS_ACTOR": "grid:alice"}

    def bd_run(argv):
        result = execute(argv, directory=tmp_path, backend=backend, env=env)
        assert result.success, f"{argv}: {result.error}"
        return result.output

    def shown_issue(argv):
        data = json.loads(bd_run(argv))
        if isinstance(data, list):
            assert len(data) == 1, data
            data = data[0]
        return data

    bd_run(["init", "--prefix", "t"])
    created = json.loads(bd_run(["create", "--title", "recover me", "--json"]))
    bead = created["id"]

    # Run A reserves, then claims in the real tracker, then is stopped by the
    # runtime (timeout) - the journal says interrupted, bd still says in_progress.
    tracker_lifecycle.bind(
        str(tmp_path), bead, run_id="exec-a", actor="grid:alice", lease_seconds=0.2
    )
    bd_run(["update", bead, "--claim", "--json"])
    tracker_lifecycle.start(str(tmp_path), bead, "exec-a", lease_seconds=0.2)
    interrupted = tracker_lifecycle.finish(
        str(tmp_path), bead, "exec-a", outcome="timeout"
    )
    assert interrupted["state"] == tracker_lifecycle.INTERRUPTED
    shown = shown_issue(["show", bead, "--json"])
    assert shown["status"] == "in_progress"

    # The lease expires (the killed/stopped run no longer heartbeats); a new
    # run takes over, closes in bd and only that confirmed close is completed.
    time.sleep(0.3)
    taken = bind(tmp_path, run_id="exec-b", bead_id=bead, lease=60)
    assert taken["takeover_from"] == "exec-a@interrupted"
    bd_run(["close", bead, "--reason", "finished by the new run", "--json"])
    completed = tracker_lifecycle.finish(
        str(tmp_path), bead, "exec-b", outcome="closed"
    )
    assert completed["state"] == tracker_lifecycle.COMPLETED

    shown = shown_issue(["show", bead, "--json"])
    assert shown["status"] == "closed"
    # A report or close started by the stale executor is refused (a bd command
    # already in flight when the lease ran out is not fenced).
    with pytest.raises(tracker_lifecycle.LifecycleError):
        tracker_lifecycle.finish(str(tmp_path), bead, "exec-a", outcome="closed")

    # The interruption checkpoint and the takeover are on the task's trail.
    phases = [item["phase"] for item in tracker_lifecycle.checkpoints(str(tmp_path), bead)]
    assert "claimed" in phases and "finished" in phases


@bd_required
def test_a_real_tracker_shares_one_journal_through_a_subdirectory(tmp_path):
    from core.tracker_gateway import Backend, execute

    bd = shutil.which("bd")
    backend = Backend.host(bd)
    execute(["init", "--prefix", "t"], directory=tmp_path, backend=backend,
            env={"BEADS_ACTOR": "grid:alice"})
    sub = tmp_path / "src"
    sub.mkdir()

    bind(tmp_path, run_id="exec-1", lease=60)
    # The tool may spell the project as a subdirectory: same workspace, same
    # journal - a second binding would be the same owner.
    assert tracker_lifecycle.lookup(str(sub), "t-1")["owner_run_id"] == "exec-1"
    assert tracker_lifecycle.journal_path(str(sub)) == tracker_lifecycle.journal_path(str(tmp_path))


def test_releasing_a_page_takeover_preserves_prior_owners_checkpoints(tmp_path):
    bind(tmp_path, run_id="run-before-page", now=time.time() - 100, lease=1)
    tracker_lifecycle.checkpoint(str(tmp_path), "t-1", "run-before-page",
                                 "work-saved", "tests finished; not yet reviewed")
    before = tracker_lifecycle.checkpoints(str(tmp_path), "t-1")
    bind(tmp_path, run_id="ui-update-key")
    assert tracker_lifecycle.release(str(tmp_path), "t-1", "ui-update-key",
                                     reason="page action ended")
    assert tracker_lifecycle.lookup(str(tmp_path), "t-1") is None
    after = tracker_lifecycle.checkpoints(str(tmp_path), "t-1")
    assert after[:len(before)] == before
    assert after[-1]["phase"] == "released"
    assert after[-1]["run_id"] == "ui-update-key"
    takeover = next(item for item in after if item["data"] and item["data"].get("takeover_from"))
    assert takeover["data"]["takeover_from"] == "run-before-page@claimed"
