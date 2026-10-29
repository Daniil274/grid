"""The lazy agents' service: tasks through their steps, stops, retries, accounting, the API."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from core.lazy.budget import Allowance
from core.lazy.model import Settings
from core.lazy.routine import Routine
from core.lazy.store import LazyStore
from core.quota_telemetry import QuotaReading, QuotaTelemetry, WindowReading
from web_chat.lazy.http import register_lazy_routes
from web_chat.lazy.runner import StepOutcome
from web_chat.lazy.service import BACKOFF, LazyAgents, LazyError

NOW = 1_800_000_000.0
HOUR = 3600.0


# -- a user's space, as far as lazy agents look into it -----------------------------------
class Access:
    def __init__(self, denied=()):
        self.denied = set(denied)
        self.credentials = SimpleNamespace(available=lambda provider: provider.auth != "none", problem=lambda p: "no login")

    def check_model(self, config, key):
        if key in self.denied:
            raise ValueError(f"{key} is not in your plan")


class Config:
    MODELS = {"opus": "claude", "luna": "chatgpt", "cheap": "api_key"}

    def __init__(self, agents):
        self.agents = agents

    def get_agent(self, key):
        return SimpleNamespace(model_keys=lambda: list(self.agents[key]))

    def get_model(self, key):
        return SimpleNamespace(provider=key)

    def get_provider(self, key):
        return SimpleNamespace(auth=self.MODELS[key], name=key)


class Registry:
    def __init__(self, agents, denied=()):
        self._config = Config(agents)
        self._access = Access(denied)

    def systems(self):
        return [SimpleNamespace(key="eng", name="Engineering")]

    def agents(self, system):
        return {key: SimpleNamespace(routable=True) for key in self._config.agents}

    def agent_label(self, system, agent):
        return agent.title()

    def config(self, system):
        return self._config

    def factory(self, system):
        return SimpleNamespace(models=SimpleNamespace(access=self._access))


class Spaces:
    def __init__(self, registry):
        self.space = SimpleNamespace(registry=registry)

    @asynccontextmanager
    async def use(self, user_id):
        yield self.space


AGENTS = {"engineer": ["opus", "luna"], "chatter": ["luna"], "payg": ["cheap"]}


class Runner:
    """Answers each step from a script; holds a step until released when asked to."""

    def __init__(self):
        self.calls = []
        self.script = []
        self.hold = None
        self.stop_seen = None

    async def run(self, task, step, settings, *, should_stop, on_conversation):
        self.calls.append((task.id, step.id, step.kind, step.resume))
        on_conversation("ctx-lazy")
        if self.hold is not None:
            while not self.hold.is_set():
                self.stop_seen = should_stop() or self.stop_seen
                await asyncio.sleep(0.01)
        outcome = self.script.pop(0) if self.script else StepOutcome("answered", NOW, NOW + 60, tokens=100_000, answer="ok")
        outcome.conversation_id = "ctx-lazy"
        return outcome


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def reading(five, week, at=NOW, five_reset=NOW + 3 * HOUR, status="allowed"):
    return QuotaReading("claude-code", {
        "five_hour": WindowReading("five_hour", five, five_reset),
        "seven_day": WindowReading("seven_day", week, NOW + 100 * HOUR),
    }, observed_at=at, status=status)


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    quota = QuotaTelemetry()
    runner = Runner()
    spaces = Spaces(Registry(AGENTS))
    store = LazyStore(tmp_path / "lazy.db")
    store.save_settings("u", Settings(routine=Routine(hours={}, timezone="UTC")))
    lazy = LazyAgents(store, spaces, quota=quota, runner=runner, clock=clock)
    quota.publish(reading(20, 20))
    return SimpleNamespace(lazy=lazy, store=store, runner=runner, clock=clock, quota=quota, space=spaces.space)


def new_task(world, **fields):
    payload = {"title": "Report", "brief": "Write it", "system": "eng", "agent": "engineer", "size": "S",
               "deadline": NOW + 48 * HOUR, **fields}
    return world.lazy.create_task("u", world.space, payload)


async def run_due(world):
    """One tick, and the step it started to its end."""
    await world.lazy._tick()
    for running in list(world.lazy._running.values()):
        await running.job


# -- creating ------------------------------------------------------------------------------
def test_a_task_is_cut_into_steps_and_runs_on_its_agents_subscription(world):
    task = new_task(world, optional="charts")
    assert [step.kind for step in task.steps] == ["scout", "work", "extra", "verify"]
    assert task.steps[2].optional
    assert task.account is None  # the settings' own subscription
    # An agent on another subscription gets a pool of its own instead of a refusal.
    assert new_task(world, agent="chatter").account == "chatgpt"
    with pytest.raises(LazyError, match="pay-per-use"):
        new_task(world, agent="payg")
    with pytest.raises(LazyError, match="title"):
        new_task(world, title=" ")


def test_a_first_visit_picks_the_subscription_the_agents_run_on(tmp_path):
    spaces = Spaces(Registry(AGENTS, denied={"opus"}))
    lazy = LazyAgents(LazyStore(tmp_path / "l.db"), spaces, quota=QuotaTelemetry(), runner=Runner())
    settings = lazy.settings_for("new", spaces.space, "Europe/Moscow")
    assert settings.account == "chatgpt" and settings.routine.timezone == "Europe/Moscow"


# -- running -------------------------------------------------------------------------------
async def test_steps_run_one_after_another_and_the_scout_sizes_the_work(world):
    task = new_task(world)
    world.runner.script = [StepOutcome("answered", NOW, NOW + 60, tokens=120_000, answer="Plan.\nSize: L\n## Checkpoint\nKnown: all.")]
    await run_due(world)
    stored = world.store.task("u", task.id)
    assert [c[2] for c in world.runner.calls] == ["scout"]
    assert stored.steps[0].status == "done" and stored.steps[0].summary == "Known: all."
    assert stored.size == "L" and stored.steps[1].tokens == 2_500_000
    assert stored.conversation_id == "ctx-lazy"
    await run_due(world)
    await run_due(world)
    stored = world.store.task("u", task.id)
    assert [c[2] for c in world.runner.calls] == ["scout", "work", "verify"]
    assert stored.status == "done"
    assert len(world.store.runs("claude-code", NOW - HOUR)) == 3


async def test_a_failed_verification_adds_a_fix_and_another_check(world):
    task = new_task(world)
    world.runner.script = [
        StepOutcome("answered", NOW, NOW, tokens=1, answer="Size: S"),
        StepOutcome("answered", NOW, NOW, tokens=1, answer="done"),
        StepOutcome("answered", NOW, NOW, tokens=1, answer="Verdict: fail - tests break"),
    ]
    for _ in range(3):
        await run_due(world)
    kinds = [(step.kind, step.status) for step in world.store.task("u", task.id).steps]
    assert kinds == [("scout", "done"), ("work", "done"), ("verify", "done"), ("fix", "pending"), ("verify", "pending")]


async def test_a_stopped_step_continues_and_a_refused_one_backs_off(world):
    task = new_task(world)
    world.runner.script = [StepOutcome("interrupted", NOW, NOW + 30, tokens=50_000, reason="You started working")]
    await run_due(world)
    scout = world.store.task("u", task.id).steps[0]
    assert scout.status == "pending" and scout.resume and scout.reason == "You started working"
    world.runner.script = [StepOutcome("refused", NOW, NOW, reason="Daily turn limit reached")]
    await run_due(world)
    stored = world.store.task("u", task.id)
    assert world.runner.calls[-1][3] is True  # it continued the interrupted turn
    assert stored.retry_at == NOW + BACKOFF * 2
    assert world.lazy.plan_for("u", world.store.settings("u")).tasks[task.id].wait.code == "retry"


async def test_a_step_that_keeps_failing_fails_its_task(world):
    task = new_task(world)
    for attempt in range(4):
        world.runner.script = [StepOutcome("error", NOW, NOW, reason="boom")]
        world.clock.now = NOW + attempt * 10 * HOUR
        await run_due(world)
    assert world.store.task("u", task.id).status == "failed"


async def test_cancelling_or_pausing_stops_a_running_step(world):
    task = new_task(world)
    world.runner.hold = asyncio.Event()
    await world.lazy._tick()
    assert world.lazy.state("u", world.space)["running"]["task_id"] == task.id
    world.lazy.cancel("u", task.id)
    await asyncio.sleep(0.05)
    assert world.runner.stop_seen == "Cancelled by you"
    world.runner.hold.set()
    await asyncio.gather(*(r.job for r in world.lazy._running.values()))
    assert world.store.task("u", task.id).status == "cancelled"


async def test_the_quota_stops_a_step_that_passes_the_agents_share(world):
    new_task(world)
    world.runner.hold = asyncio.Event()
    await world.lazy._tick()
    world.quota.publish(reading(20 + 11, 21, at=NOW + 1))  # the run took 11 of a 10-point share
    await asyncio.sleep(0.05)
    assert world.runner.stop_seen == "Reached the agents' share of the 5-hour window"
    world.runner.hold.set()
    await asyncio.gather(*(r.job for r in world.lazy._running.values()))


async def test_an_old_reading_does_not_book_the_users_use_since_to_the_running_step(world):
    # The last reading before the start is 40 minutes old; the user spent 30
    # points since, in a session of their own. That is not the step's.
    world.clock.now += 40 * 60  # the fixture's reading at NOW is now 40 minutes old
    new_task(world)
    world.runner.hold = asyncio.Event()
    await world.lazy._tick()
    world.quota.publish(reading(50, 22, at=world.clock.now + 1))
    await asyncio.sleep(0.05)
    assert world.runner.stop_seen is None
    world.runner.hold.set()
    await asyncio.gather(*(r.job for r in world.lazy._running.values()))


async def test_a_run_between_fresh_readings_is_measured_and_calibrates(world):
    task = new_task(world)
    world.runner.hold = asyncio.Event()
    await world.lazy._tick()
    world.quota.publish(reading(23, 20.5, at=NOW + 50))
    world.runner.script = [StepOutcome("answered", NOW + 1, NOW + 60, tokens=1_000_000, answer="Size: S")]
    world.runner.hold.set()
    await asyncio.gather(*(r.job for r in world.lazy._running.values()))
    run = world.store.runs("claude-code", NOW - HOUR)[0]
    assert run.source == "measured" and run.used == pytest.approx({"five_hour": 3.0, "seven_day": 0.5})
    assert world.store.calibration("claude-code").samples == 1
    assert world.store.task("u", task.id).steps[0].used == pytest.approx({"five_hour": 3.0, "seven_day": 0.5})


async def test_without_a_fresh_reading_the_run_is_estimated(world):
    world.quota.publish(reading(20, 20, at=NOW + 1))
    world.clock.now = NOW + 2 * HOUR  # the reading is old by the time the step starts
    new_task(world)
    await run_due(world)
    assert world.store.runs("claude-code", NOW)[0].source == "estimated"


async def test_a_step_running_when_the_server_stopped_continues(world):
    task = new_task(world)
    task.steps[0].status = "running"
    world.store.save_task(task)
    world.lazy._recover()
    scout = world.store.task("u", task.id).steps[0]
    assert scout.status == "pending" and scout.resume


def test_readings_teach_the_users_rates(world):
    world.lazy._on_reading(reading(20, 20, at=NOW))
    world.lazy._on_reading(reading(30, 21, at=NOW + HOUR))
    rates = world.store.rates("u")
    assert rates.samples == 2  # an hour of each window
    assert rates.idle["five_hour"] == pytest.approx(0.7 * 0.5 + 0.3 * 10)
    assert rates.idle["seven_day"] == pytest.approx(0.7 * 0.05 + 0.3 * 1)


# -- negotiating ---------------------------------------------------------------------------
def test_a_way_out_is_previewed_then_taken(world):
    world.store.save_settings("u", Settings(
        allowances={"five_hour": Allowance(10, 10), "seven_day": Allowance(0.5, 5)}, routine=Routine(hours={}, timezone="UTC"),
    ))
    task = new_task(world, size="M", deadline=NOW + HOUR)
    state = world.lazy.state("u", world.space)
    planned = state["plan"]["tasks"][task.id]
    assert planned["state"] in ("at_risk", "blocked")
    share = next(option for option in planned["options"] if option["kind"] == "share" and "seven_day" in str(option["change"]))
    preview = world.lazy.preview("u", task.id, share["change"])
    assert preview["after"]["state"] == "on_track"
    world.lazy.apply("u", task.id, share["change"])
    assert world.store.settings("u").allowance("seven_day").share == share["change"]["settings"]["allowances"]["seven_day"]["share"]


def test_a_draft_shows_where_it_would_land(world):
    preview = world.lazy.preview_new("u", world.space, {"title": "Draft", "system": "eng", "agent": "engineer", "size": "S"})
    assert [step["kind"] for step in preview["steps"]] == ["scout", "work", "verify"]
    assert preview["plan"]["state"] == "flexible" and len(preview["placements"]) == 3
    assert world.store.tasks("u") == []


# -- the API ---------------------------------------------------------------------------------
def test_the_routes_act_for_the_signed_in_user(world):
    app = FastAPI()
    api = APIRouter()
    user = SimpleNamespace(id="u")

    async def current_user():
        return user

    async def current_space():
        return world.space

    register_lazy_routes(api, current_user, current_space, world.lazy)
    app.include_router(api)
    client = TestClient(app)
    created = client.post("/api/lazy/tasks", json={"title": "Doc", "system": "eng", "agent": "engineer"}).json()
    task_id = created["created"]
    assert created["tasks"][0]["id"] == task_id and created["windows"]["five_hour"]["share"] == 10
    assert client.post("/api/lazy/tasks", json={"title": "Doc", "system": "eng", "agent": "payg"}).status_code == 400
    assert client.patch(f"/api/lazy/tasks/{task_id}", json={"priority": "high"}).json()["tasks"][0]["priority"] == "high"
    assert client.patch("/api/lazy/tasks/nope", json={"priority": "high"}).status_code == 404
    paused = client.put("/api/lazy/settings", json={"paused": True}).json()
    assert paused["settings"]["paused"] is True
    assert client.post(f"/api/lazy/tasks/{task_id}/cancel").json()["tasks"][0]["status"] == "cancelled"
    assert client.delete(f"/api/lazy/tasks/{task_id}").json()["tasks"] == []


# -- a pool per subscription ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_tasks_on_two_subscriptions_run_side_by_side_each_on_its_own_pool(world):
    claude = new_task(world, title="On Claude")
    chatgpt = new_task(world, title="On ChatGPT", agent="chatter")
    await world.lazy._tick()
    running = {account: run.task_id for (_, account), run in world.lazy._running.items()}
    assert running == {"claude-code": claude.id, "chatgpt": chatgpt.id}
    for run in list(world.lazy._running.values()):
        await run.job
    # Each run is booked to its own subscription's ledger (the host login is one, a plan each user's).
    runs = {run.task_id: run.account for run in world.store.runs("claude-code", 0) + world.store.runs("chatgpt:u", 0)}
    assert runs == {claude.id: "claude-code", chatgpt.id: "chatgpt:u"}


def test_the_page_shows_the_other_pool_beside_the_settings_one(world):
    new_task(world, title="On Claude")
    other = new_task(world, title="On ChatGPT", agent="chatter")
    state = world.lazy.state("u", world.space)
    assert state["account"]["key"] == "claude-code"
    assert [pool["account"]["key"] for pool in state["pools"]] == ["chatgpt"]
    assert set(state["pools"][0]["windows"]) == {"five_hour", "seven_day"}
    assert other.id in state["plan"]["tasks"]
    view = next(task for task in state["tasks"] if task["id"] == other.id)
    assert view["account"] == "chatgpt" and view["account_label"]


def test_switching_the_settings_subscription_keeps_every_task_on_its_own(world):
    claude = new_task(world, title="On Claude")
    chatgpt = new_task(world, title="On ChatGPT", agent="chatter")
    world.lazy.update_settings("u", {"account": "chatgpt"})
    assert world.store.task("u", claude.id).account == "claude-code"
    assert world.store.task("u", chatgpt.id).account is None  # now the settings' own


@pytest.mark.asyncio
async def test_a_repeating_task_sets_up_its_next_round_when_done(world):
    task = new_task(world, title="Review the chats", repeat_hours=24)
    assert task.repeat == 24 * HOUR
    for _ in range(len(task.steps)):
        await run_due(world)
    done = world.store.task("u", task.id)
    assert done.status == "done"
    rounds = [t for t in world.store.tasks("u", finished=False) if t.title == "Review the chats"]
    assert len(rounds) == 1 and rounds[0].id != task.id
    following = rounds[0]
    assert following.repeat == task.repeat and following.agent == task.agent
    assert following.deadline >= task.deadline + 24 * HOUR  # a day after the round it follows
    assert all(step.status == "pending" for step in following.steps) and following.conversation_id is None
    assert new_task(world, repeat_hours=0).repeat is None  # zero or nothing runs once
