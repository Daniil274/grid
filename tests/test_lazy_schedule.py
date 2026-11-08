"""Lazy scheduling: typed releases, calendar rules, DST and a series' budget.

Everything here runs on a simulated clock - no live model, no server: the
calendar rules are pure arithmetic, and the service is driven tick by tick.
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.lazy.budget import Allowance
from core.lazy.estimate import Calibration
from core.lazy.forecast import UserRates
from core.lazy.model import Run, Settings, Step, Task
from core.lazy.planner import plan
from core.lazy.routine import Routine
from core.lazy.schedule import Schedule
from core.lazy.situation import Situation, WindowNow
from core.lazy.store import LazyStore
from core.lazy.windows import HOUR
from core.quota_telemetry import QuotaReading, QuotaTelemetry, WindowReading
from tools import lazy_tools
from web_chat.lazy.chat import LazyChat
from web_chat.lazy.service import LazyAgents, LazyError
from web_chat.lazy.runner import StepOutcome

#: 2027-01-15 08:00 UTC = 11:00 in Moscow.
NOW = 1_800_000_000.0
MOSCOW = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")

NO_HOURS = Routine(hours={}, timezone="UTC")
STILL = UserRates(busy={"five_hour": 0.0, "seven_day": 0.0}, idle={"five_hour": 0.0, "seven_day": 0.0})
#: 1M tokens cost 2 points of the five-hour window and 0.5 of the week.
PRICES = Calibration(price={"five_hour": 2.0, "seven_day": 0.5})
SETTINGS = Settings(
    allowances={"five_hour": Allowance(10, 10), "seven_day": Allowance(10, 5)}, routine=NO_HOURS,
)


def roomy(now: float = NOW) -> Situation:
    return Situation(now=now, account="claude-code", mode="live", windows={
        "five_hour": WindowNow("five_hour", used=0, user=0, agents=0, reserved=0, resets_at=now + 4 * HOUR),
        "seven_day": WindowNow("seven_day", used=0, user=0, agents=0, reserved=0, resets_at=now + 100 * HOUR),
    })


def task_of(*steps, task_id="t1", created=0.0, **fields) -> Task:
    return Task(
        id=task_id, user_id="u", title=task_id, brief="", created_at=created,
        steps=[Step(f"s{i}", kind, tokens, minutes)
               for i, (kind, tokens, minutes) in enumerate(steps, 1)],
        **fields,
    )


# -- the rule on its own ----------------------------------------------------------------
def test_an_interval_releases_a_whole_period_after_the_release():
    rule = Schedule.interval(HOUR)
    assert rule.next_release(NOW + 60, anchor=NOW) == NOW + HOUR  # a short round does not collapse it
    assert rule.next_release(NOW, anchor=NOW) == NOW + HOUR  # an instant round still waits a period
    assert rule.next_release(NOW + HOUR, anchor=NOW) == NOW + 2 * HOUR  # strictly after, never equal
    assert rule.next_release(NOW + HOUR - 1, anchor=NOW) == NOW + HOUR


def test_a_round_that_ran_long_skips_the_slots_it_missed():
    rule = Schedule.interval(HOUR)
    # Released at NOW, ended three and a half hours later: slots 1..3 are gone,
    # and one round waits for slot 4 - not three rounds back to back.
    assert rule.next_release(NOW + 3.5 * HOUR, anchor=NOW) == NOW + 4 * HOUR


def test_a_daily_rule_keeps_the_users_four_oclock():
    rule = Schedule.from_dict({"kind": "daily", "time": "04:00", "timezone": "Europe/Moscow"})
    release = rule.next_release(NOW)  # 11:00 Moscow
    local = datetime.fromtimestamp(release, MOSCOW)
    assert (local.hour, local.minute) == (4, 0)
    assert local.date().isoformat() == "2027-01-16"
    assert release == datetime(2027, 1, 16, 4, 0, tzinfo=MOSCOW).timestamp()


def test_a_missed_daily_slot_is_one_round_not_a_queue():
    rule = Schedule.from_dict({"kind": "daily", "time": "04:00", "timezone": "Europe/Moscow"})
    # Nothing ran for three days: the answer is tomorrow's 04:00, once.
    after = datetime(2027, 1, 19, 10, 0, tzinfo=MOSCOW).timestamp()
    release = rule.next_release(after)
    assert datetime.fromtimestamp(release, MOSCOW) == datetime(2027, 1, 20, 4, 0, tzinfo=MOSCOW)


def test_a_weekly_rule_picks_the_named_local_day():
    rule = Schedule.from_dict({"kind": "weekly", "time": "04:00", "weekday": "mon", "timezone": "Europe/Moscow"})
    release = rule.next_release(NOW)  # Friday 15 January 2027
    local = datetime.fromtimestamp(release, MOSCOW)
    assert local.weekday() == 0 and (local.hour, local.minute) == (4, 0)
    assert local.date().isoformat() == "2027-01-18"


def test_the_spring_gap_moves_forward_by_the_gap_and_is_deterministic():
    rule = Schedule.from_dict({"kind": "daily", "time": "02:30", "timezone": "Europe/Berlin"})
    after = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc).timestamp()
    release = rule.next_release(after)
    # 02:30 does not exist on 29 March 2026: it lands just after the jump, at 03:30 CEST.
    local = datetime.fromtimestamp(release, BERLIN)
    assert (local.hour, local.minute) == (3, 30)
    assert local.utcoffset().total_seconds() == 7200
    assert release == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc).timestamp()
    assert rule.next_release(after) == release  # the same answer every run


def test_the_autumn_fold_takes_the_first_occurrence():
    rule = Schedule.from_dict({"kind": "daily", "time": "02:30", "timezone": "Europe/Berlin"})
    after = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc).timestamp()
    release = rule.next_release(after)
    # 02:30 happens twice on 25 October 2026; the first (CEST, +02:00) is taken.
    assert release == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()
    local = datetime.fromtimestamp(release, BERLIN)
    assert (local.hour, local.minute, local.fold) == (2, 30, 0)
    assert local.utcoffset().total_seconds() == 7200


# -- the planner: a release holds the place, a deadline does not -------------------------
def test_a_release_holds_the_place_and_the_forecast_never_precedes_it():
    task = task_of(("work", 100_000, 30), not_before=NOW + 6 * HOUR)
    result = plan([task], SETTINGS, roomy(), STILL, PRICES)
    entry = result.tasks["t1"]
    assert result.placements[0].start == NOW + 6 * HOUR
    assert entry.scheduled_release == NOW + 6 * HOUR
    assert entry.next_start >= entry.scheduled_release  # release <= forecast, always
    assert entry.wait.code == "not_before" and entry.wait.until == NOW + 6 * HOUR


def test_a_future_deadline_does_not_hold_work():
    # The old docstring promised this and the planner never did it: a deadline
    # says when it must be done, not when it may start.
    task = task_of(("work", 100_000, 30), deadline=NOW + 72 * HOUR)
    result = plan([task], SETTINGS, roomy(), STILL, PRICES)
    assert result.placements[0].start == NOW
    assert result.tasks["t1"].scheduled_release is None


def test_a_release_never_starts_a_round_before_it_even_with_room_to_spare():
    task = task_of(("work", 100_000, 30), not_before=NOW + HOUR, run_now=True)
    result = plan([task], SETTINGS, roomy(), STILL, PRICES)
    assert result.placements[0].start == NOW + HOUR


# -- the planner: the cap ---------------------------------------------------------------
def test_a_series_cap_is_spent_by_the_whole_series():
    task = task_of(("work", 1_000_000, 30), cap=1.0, series_id="s")  # the step costs 0.5 weekly
    fresh = plan([task], SETTINGS, roomy(), STILL, PRICES, series_spend={"s": 0.0})
    assert fresh.placements and fresh.tasks["t1"].state != "blocked"
    spent = plan([task], SETTINGS, roomy(), STILL, PRICES, series_spend={"s": 0.9})
    assert spent.tasks["t1"].state == "blocked" and spent.tasks["t1"].wait.code == "cap"
    assert spent.placements == []


def test_no_cap_is_no_extra_limit_and_zero_is_a_real_zero():
    open_task = task_of(("work", 1_000_000, 30))  # cap None
    assert plan([open_task], SETTINGS, roomy(), STILL, PRICES).placements
    zero = task_of(("work", 1_000_000, 30), cap=0.0)
    result = plan([zero], SETTINGS, roomy(), STILL, PRICES)
    assert result.tasks["t1"].state == "blocked" and result.tasks["t1"].wait.code == "cap"


# -- the service on a simulated clock ---------------------------------------------------
class Access:
    credentials = SimpleNamespace(available=lambda provider: True, problem=lambda p: None)

    def check_model(self, config, key):
        return None


class Config:
    MODELS = {"opus": "claude"}

    def __init__(self, agents):
        self.agents = agents

    def get_agent(self, key):
        return SimpleNamespace(model_keys=lambda: list(self.agents[key]))

    def get_model(self, key):
        return SimpleNamespace(provider=key)

    def get_provider(self, key):
        return SimpleNamespace(auth=self.MODELS[key], name=key)


class Registry:
    def __init__(self, agents):
        self._config = Config(agents)
        self._access = Access()

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
    def __init__(self):
        self.space = SimpleNamespace(registry=Registry({"engineer": ["opus"]}))

    def use(self, user_id):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _open():
            yield self.space

        return _open()


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Runner:
    def __init__(self):
        self.calls = []

    async def run(self, task, step, settings, *, should_stop, on_conversation):
        self.calls.append((task.id, step.id, step.kind))
        on_conversation("ctx-lazy")
        outcome = StepOutcome("answered", NOW, NOW + 60, tokens=100_000, answer="ok")
        outcome.conversation_id = "ctx-lazy"
        return outcome


def reading(five=20.0, week=20.0):
    return QuotaReading("claude-code", {
        "five_hour": WindowReading("five_hour", five, NOW + 3 * HOUR),
        "seven_day": WindowReading("seven_day", week, NOW + 100 * HOUR),
    }, observed_at=NOW, status="allowed")


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    quota = QuotaTelemetry()
    runner = Runner()
    spaces = Spaces()
    store = LazyStore(tmp_path / "lazy.db")
    store.save_settings("u", Settings(routine=Routine(hours={}, timezone="Europe/Moscow")))
    lazy = LazyAgents(store, spaces, quota=quota, runner=runner, clock=clock)
    quota.publish(reading())
    return SimpleNamespace(
        lazy=lazy, store=store, runner=runner, clock=clock, quota=quota, space=spaces.space,
        chat=LazyChat(lazy, "u", spaces.space),
    )


PAYLOAD = {"title": "Report", "brief": "Write it", "system": "eng", "agent": "engineer", "size": "S"}


def payload(**fields):
    return {**PAYLOAD, "deadline": NOW + 48 * HOUR, **fields}


async def finish_round(world, task_id):
    """Run every step of the open round, one tick each, until it is done."""
    for _ in range(8):
        task = world.store.task("u", task_id)
        if task is None or task.status in ("done", "failed", "cancelled"):
            return
        await world.lazy._tick()
        await asyncio.gather(*(running.job for running in list(world.lazy._running.values())), return_exceptions=True)


async def test_two_rounds_of_an_interval_series_are_never_back_to_back(world):
    first = world.lazy.create_task("u", world.space, payload(repeat_hours=1))
    assert first.not_before is None  # the first round is not held: only the plan decides
    await finish_round(world, first.id)

    rounds = world.store.tasks("u", finished=False)
    assert len(rounds) == 1
    second = rounds[0]
    assert second.series_id == first.series_id == first.id
    # Released a whole interval after the first round's own release.
    assert second.not_before == NOW + HOUR
    # The deadline keeps the span the first round had between release and deadline.
    assert second.deadline == second.not_before + (first.deadline - first.created_at)

    # Nothing starts before the release, even with room and no other task.
    world.clock.now = second.not_before - 120
    planned = world.lazy.plan_for("u", world.store.settings("u"))
    assert planned.tasks[second.id].next_start == second.not_before
    assert planned.tasks[second.id].scheduled_release == second.not_before
    calls = len(world.runner.calls)
    await world.lazy._tick()
    assert len(world.runner.calls) == calls  # not a second early

    world.clock.now = second.not_before
    await world.lazy._tick()
    assert set(world.lazy._running)  # the loop woke exactly at the release
    await asyncio.gather(*(running.job for running in list(world.lazy._running.values())), return_exceptions=True)
    assert len(world.runner.calls) == calls + 1
    assert world.store.task("u", second.id).steps[0].started_at == second.not_before


async def test_the_second_round_holds_the_fixed_grid_not_the_runtime(world):
    first = world.lazy.create_task("u", world.space, payload(repeat_hours=1))
    await finish_round(world, first.id)
    second = world.store.tasks("u", finished=False)[0]
    world.clock.now = second.not_before
    await finish_round(world, second.id)
    third = world.store.tasks("u", finished=False)[0]
    assert third.not_before == NOW + 2 * HOUR  # slot 3 on the same grid, not "one hour after round two ended"


async def test_a_series_budget_comes_from_the_ledger_across_rounds(world):
    first = world.lazy.create_task("u", world.space, payload(repeat_hours=24, cap=1.0))
    for step in first.steps:
        step.status = "done"
    first.status = "done"
    world.store.save_task(first)
    second = world.lazy._next_round(first, NOW)
    world.store.save_task(second)
    world.store.add_run(Run(id="r1", user_id="u", account="claude-code", task_id=first.id, step_id="s1",
                            started_at=NOW, ended_at=NOW + 60, used={"seven_day": 0.6}))
    world.store.add_run(Run(id="r2", user_id="u", account="claude-code", task_id=second.id, step_id="s1",
                            started_at=NOW + HOUR, ended_at=NOW + HOUR + 60, used={"seven_day": 0.4}))
    spent = world.lazy.series_spend("u", world.store.settings("u"))
    assert spent[first.series_id] == pytest.approx(1.0)
    # A second round does not open a fresh budget: the series is at its cap.
    entry = world.lazy.plan_for("u", world.store.settings("u")).tasks[second.id]
    assert entry.state == "blocked" and entry.wait.code == "cap"


async def test_a_daily_rule_keeps_the_users_wall_clock(world):
    task = world.lazy.create_task(
        "u", world.space,
        payload(repeat=json.dumps({"kind": "daily", "time": "04:00"})),  # the zone: the user's own
    )
    assert task.schedule.kind == "daily" and task.schedule.timezone == "Europe/Moscow"
    assert task.series_id == task.id  # a calendar rule opens a series, like an interval does
    following = world.lazy._next_round(task, NOW)  # 11:00 in Moscow
    local = datetime.fromtimestamp(following.not_before, MOSCOW)
    assert local.date().isoformat() == "2027-01-16" and (local.hour, local.minute) == (4, 0)
    assert following.schedule.to_dict()["time"] == "04:00"


def test_an_unknown_zone_is_refused_not_silently_utc(world):
    with pytest.raises(LazyError, match="time zone"):
        world.lazy.create_task(
            "u", world.space,
            payload(repeat=json.dumps({"kind": "daily", "time": "04:00", "timezone": "Mars/Olympus"})),
        )


# -- the tools' contract ----------------------------------------------------------------
def ctx(chat):
    return SimpleNamespace(context=SimpleNamespace(factory=SimpleNamespace(lazy_agents=chat)))


async def call(tool, chat, **arguments):
    return json.loads(await tool.on_invoke_tool(ctx(chat), json.dumps(arguments)))


async def test_tools_leave_the_cap_open_unless_it_is_named(world):
    open_task = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD)
    assert world.store.task("u", open_task["task"]["id"]).cap is None  # not a silent zero
    zero = await call(lazy_tools.lazy_create, world.chat, **{**PAYLOAD, "cap": 0})
    assert world.store.task("u", zero["task"]["id"]).cap == 0.0  # a real zero, named on purpose


async def test_a_release_is_a_moment_with_an_offset_and_an_epoch(world):
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, not_before="2026-11-10T04:00")
    task_id = made["task"]["id"]
    expected = datetime(2026, 11, 10, 4, 0, tzinfo=MOSCOW).timestamp()
    assert world.store.task("u", task_id).not_before == expected

    one = await call(lazy_tools.lazy_get, world.chat, task_id=task_id)
    moment = one["task"]["times"]["not_before"]
    assert moment["epoch"] == pytest.approx(expected, abs=0.001)
    assert moment["iso"].startswith("2026-11-10T04:00") and moment["iso"].endswith("+03:00")
    assert moment["timezone"] == "Europe/Moscow"
    # Where it lands is the release or later - never earlier.
    assert one["placement"]["scheduled_release"] == expected
    assert one["placement"]["next_start"] >= one["placement"]["scheduled_release"]


async def test_a_release_can_be_moved_and_cleared_by_the_tools(world):
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD)
    task_id = made["task"]["id"]
    moved = await call(lazy_tools.lazy_update, world.chat, task_id=task_id, not_before="2026-11-10T04:00")
    assert moved["task"]["not_before"] == datetime(2026, 11, 10, 4, 0, tzinfo=MOSCOW).timestamp()
    cleared = await call(lazy_tools.lazy_update, world.chat, task_id=task_id, not_before="")
    assert cleared["task"]["not_before"] is None
