"""Lazy agents' core: windows, budget, forecast, estimates, the planner and its store."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from core.lazy.budget import Allowance, fits, headroom
from core.lazy.estimate import Calibration, checkpoint_of, kinds_for, size_in, verdict_in
from core.lazy.forecast import UserRates
from core.lazy.model import Run, Settings, Step, Task
from core.lazy.planner import plan
from core.lazy.routine import Routine
from core.lazy.situation import FRESH, Situation, WindowNow, from_reading
from core.lazy.store import LazyStore
from core.lazy.windows import FIVE_HOUR, HOUR, SEVEN_DAY, Periods
from core.quota_telemetry import QuotaReading, QuotaTelemetry, WindowReading, claude_reading

UTC = timezone.utc
#: A Saturday, 01:00 UTC: outside any weekday hours.
NOW = datetime(2026, 10, 17, 1, 0, tzinfo=UTC).timestamp()
NO_HOURS = Routine(hours={}, timezone="UTC")
STILL = UserRates(busy={"five_hour": 0.0, "seven_day": 0.0}, idle={"five_hour": 0.0, "seven_day": 0.0})
#: 1M tokens cost 2 points of the five-hour window and 0.5 of the week.
PRICES = Calibration(price={"five_hour": 2.0, "seven_day": 0.5})


def concept_settings(**changes) -> Settings:
    settings = Settings(
        allowances={"five_hour": Allowance(10, 10), "seven_day": Allowance(10, 5)},
        routine=NO_HOURS,
    )
    for key, value in changes.items():
        setattr(settings, key, value)
    return settings


def concept_situation(now: float = NOW) -> Situation:
    """The worked example: agents used 6 and 7, the user 70 and 78, no reservation."""
    return Situation(now=now, account="claude-code", mode="live", windows={
        "five_hour": WindowNow("five_hour", used=76, user=70, agents=6, reserved=0, resets_at=now + 2 * HOUR),
        "seven_day": WindowNow("seven_day", used=85, user=78, agents=7, reserved=0, resets_at=now + 96 * HOUR),
    })


def task_of(*steps, task_id="t1", deadline=NOW + 72 * HOUR, created=0.0, **fields) -> Task:
    return Task(id=task_id, user_id="u", title=task_id, brief="", deadline=deadline, created_at=created,
                steps=[Step(f"s{i}", kind, tokens, minutes, optional=kind == "extra") for i, (kind, tokens, minutes) in enumerate(steps, 1)],
                **fields)


REPORT = (("scout", 1_000_000, 10), ("work", 2_500_000, 30), ("verify", 1_000_000, 10))


# -- budget ------------------------------------------------------------------------------
def test_headroom_is_the_concepts_formula():
    assert headroom(Allowance(10, 10), agents=6, reserved=0, user=70) == pytest.approx(4)
    assert headroom(Allowance(10, 5), agents=7, reserved=0, user=78) == pytest.approx(3)
    # The user's room binds before the agents' own share.
    assert headroom(Allowance(10, 10), agents=0, reserved=0, user=85) == pytest.approx(5)
    # Never negative; a reservation counts like use.
    assert headroom(Allowance(10, 10), agents=9, reserved=3, user=0) == 0


def test_a_step_must_fit_every_window_at_once():
    assert fits({"five_hour": 2, "seven_day": 0.5}, {"five_hour": 4, "seven_day": 3}) == (True, None)
    assert fits({"five_hour": 2, "seven_day": 3.5}, {"five_hour": 4, "seven_day": 3}) == (False, "seven_day")


# -- the planner -------------------------------------------------------------------------
def test_the_concepts_example_scouts_now_and_works_after_the_reset():
    result = plan([task_of(*REPORT)], concept_settings(), concept_situation(), STILL, PRICES)
    starts = {p.step_id: p.start - NOW for p in result.placements}
    assert starts == {"s1": 0, "s2": 2 * HOUR, "s3": 2 * HOUR + 30 * 60}
    assert result.tasks["t1"].state == "on_track"
    assert result.tasks["t1"].finish_at == NOW + 2 * HOUR + 40 * 60


def test_a_week_that_cannot_hold_both_tasks_offers_ways_out():
    first = task_of(*REPORT)
    second = task_of(("work", 2_000_000, 20), task_id="t2", created=1.0)
    result = plan([first, second], concept_settings(), concept_situation(), STILL, PRICES)
    late = result.tasks["t2"]
    assert result.tasks["t1"].state == "on_track"
    assert late.state == "at_risk"
    # It waits for the weekly reset, and says so.
    assert late.next_start == NOW + 96 * HOUR
    assert late.wait.code == "budget" and late.wait.window == "seven_day"
    kinds = {option.kind: option for option in late.options}
    assert kinds["deadline"].change["task"]["deadline"] >= late.finish_at
    # 7 + 2.25 + 1 = 10.25 weekly points: the least share that is enough, in halves.
    assert kinds["share"].change == {"settings": {"allowances": {"seven_day": {"share": 10.5}}}}


def test_earliest_deadline_goes_first_and_one_step_runs_at_a_time():
    roomy = Situation(now=NOW, account="a", mode="live", windows={
        "five_hour": WindowNow("five_hour", 0, 0, 0, 0, NOW + 5 * HOUR),
        "seven_day": WindowNow("seven_day", 0, 0, 0, 0, NOW + 100 * HOUR),
    })
    later = task_of(("work", 100_000, 30), task_id="later", deadline=NOW + 10 * HOUR)
    sooner = task_of(("work", 100_000, 30), task_id="sooner", deadline=NOW + 5 * HOUR, created=5.0)
    result = plan([later, sooner], concept_settings(), roomy, STILL, PRICES)
    by_task = {p.task_id: p for p in result.placements}
    assert by_task["sooner"].start == NOW
    assert by_task["later"].start == NOW + 30 * 60
    assert result.tasks["later"].wait.code == "busy"


def test_the_users_hours_are_kept_free_unless_the_task_must_run_now():
    hours = Routine(hours={5: [(60, 600)]}, timezone="UTC")  # Saturday 01:00-10:00
    roomy = Situation(now=NOW, account="a", mode="live", windows={
        "five_hour": WindowNow("five_hour", 0, 0, 0, 0, NOW + 5 * HOUR),
        "seven_day": WindowNow("seven_day", 0, 0, 0, 0, NOW + 100 * HOUR),
    })
    task = task_of(("work", 100_000, 30))
    waiting = plan([task], concept_settings(routine=hours), roomy, STILL, PRICES)
    assert waiting.placements[0].start == NOW + 9 * HOUR
    assert waiting.tasks["t1"].wait.code == "my_hours"
    task.run_now = True
    now_please = plan([task], concept_settings(routine=hours), roomy, STILL, PRICES)
    assert now_please.placements[0].start == NOW


def test_a_step_ends_before_the_users_hours_begin():
    hours = Routine(hours={5: [(90, 600)]}, timezone="UTC")  # Saturday 01:30-10:00
    roomy = Situation(now=NOW, account="a", mode="live", windows={
        "five_hour": WindowNow("five_hour", 0, 0, 0, 0, NOW + 5 * HOUR),
        "seven_day": WindowNow("seven_day", 0, 0, 0, 0, NOW + 100 * HOUR),
    })
    fits_before = plan([task_of(("work", 100_000, 20))], concept_settings(routine=hours), roomy, STILL, PRICES)
    assert fits_before.placements[0].start == NOW
    too_long = plan([task_of(("work", 100_000, 45))], concept_settings(routine=hours), roomy, STILL, PRICES)
    assert too_long.placements[0].start == NOW + 9 * HOUR


def test_dropping_the_optional_part_can_save_a_deadline():
    # 2 + 1 + 1 points fit the four the five-hour window has now; the optional 2 do not.
    task = task_of(("scout", 1_000_000, 10), ("work", 500_000, 10), ("extra", 1_000_000, 10), ("verify", 500_000, 10),
                   deadline=NOW + 90 * 60)
    result = plan([task], concept_settings(), concept_situation(), STILL, PRICES)
    assert result.tasks["t1"].state == "at_risk"
    assert "drop_optional" in {option.kind for option in result.tasks["t1"].options}


def test_a_step_larger_than_the_share_is_blocked_with_the_reason():
    result = plan([task_of(("work", 6_000_000, 30))], concept_settings(), concept_situation(), STILL, PRICES)
    assert result.tasks["t1"].state == "blocked"
    assert result.tasks["t1"].wait.code == "too_big"


def test_paused_tasks_and_a_paused_pool_plan_nothing():
    task = task_of(*REPORT, status="paused")
    assert plan([task], concept_settings(), concept_situation(), STILL, PRICES).tasks["t1"].state == "paused"
    pool = plan([task_of(*REPORT)], concept_settings(paused=True), concept_situation(), STILL, PRICES)
    assert pool.tasks["t1"].wait.code == "pool_paused" and pool.placements == []


def test_a_running_step_holds_the_runner_and_its_task():
    task = task_of(*REPORT)
    task.steps[0].status, task.steps[0].started_at = "running", NOW - 60
    result = plan([task], concept_settings(), concept_situation(), STILL, PRICES)
    assert result.tasks["t1"].state == "running"
    assert result.placements[0].running and result.placements[0].step_id == "s1"


def test_the_chart_projects_both_windows():
    result = plan([task_of(*REPORT)], concept_settings(), concept_situation(), STILL, PRICES).to_dict()
    five = result["windows"]["five_hour"]
    assert five["periods"][0]["user"] == 70 and five["periods"][0]["room"] == pytest.approx(4 - 2)
    assert five["curve"]["agents"][0] == 6 + 2  # the scout, booked at the start


# -- periods and situation ---------------------------------------------------------------
def test_periods_follow_on_from_the_reported_reset():
    periods = Periods.from_reset(FIVE_HOUR, NOW, NOW + HOUR)
    assert periods.index(NOW) == 0 and periods.index(NOW + HOUR) == 1
    assert periods.bounds(1) == (NOW + HOUR, NOW + 6 * HOUR)
    closed = Periods.from_reset(FIVE_HOUR, NOW, None)
    assert closed.first_end == NOW + 5 * HOUR
    week = Periods.from_reset(SEVEN_DAY, NOW, NOW - 2 * HOUR)  # a past reset rolls on
    assert week.first_end == NOW - 2 * HOUR + 7 * 24 * HOUR


def reading(five_used, week_used, *, at=NOW, five_reset=NOW + HOUR, week_reset=NOW + 50 * HOUR):
    return QuotaReading("claude-code", {
        "five_hour": WindowReading("five_hour", five_used, five_reset),
        "seven_day": WindowReading("seven_day", week_used, week_reset),
    }, observed_at=at)


def test_the_situation_splits_use_between_the_user_and_the_pool():
    runs = [Run("r1", "u", "claude-code", "t", "s1", NOW - 600, NOW - 300, used={"five_hour": 4.0, "seven_day": 1.0})]
    now = from_reading(reading(30, 40), account="claude-code", runs=runs, reserved={"five_hour": 2.0},
                       rates=STILL, routine=NO_HOURS, now=NOW)
    five = now.windows["five_hour"]
    assert (five.user, five.agents, five.reserved) == (26, 4, 2)
    assert now.mode == "live"


def test_a_stale_reading_adds_what_the_user_usually_spends():
    rates = UserRates(busy={"five_hour": 0, "seven_day": 0}, idle={"five_hour": 6.0, "seven_day": 1.0})
    old = from_reading(reading(30, 40, at=NOW - 2 * HOUR, five_reset=NOW + HOUR), account="claude-code", runs=[],
                       reserved={}, rates=rates, routine=NO_HOURS, now=NOW)
    assert old.mode == "stale"
    assert old.windows["five_hour"].user == pytest.approx(30 + 12)


def test_a_window_whose_period_ended_is_empty_and_closed():
    over = from_reading(reading(90, 40, at=NOW - 3 * HOUR, five_reset=NOW - HOUR), account="claude-code", runs=[],
                        reserved={}, rates=STILL, routine=NO_HOURS, now=NOW)
    assert over.windows["five_hour"].used == 0 and over.windows["five_hour"].resets_at is None
    unknown = from_reading(None, account="claude-code", runs=[], reserved={}, rates=STILL, routine=NO_HOURS, now=NOW)
    assert unknown.mode == "unknown" and unknown.windows["seven_day"].used == 50


def test_fresh_is_ten_minutes():
    assert FRESH == 600


# -- telemetry ---------------------------------------------------------------------------
CLAUDE_EVENT = {
    "status": "allowed", "resetsAt": 1792172400, "rateLimitType": "five_hour", "overageStatus": "rejected",
    "unifiedWindows": {"five_hour": {"utilization": 0.7, "resetsAt": 1792172400},
                       "seven_day": {"utilization": 0.23, "resetsAt": 1792717200}},
}


def test_a_claude_rate_limit_event_becomes_a_reading():
    found = claude_reading(CLAUDE_EVENT, now=NOW)
    assert found.windows["five_hour"].used == pytest.approx(70)
    assert found.windows["seven_day"].resets_at == 1792717200
    assert found.overage_allowed is False
    assert claude_reading({"status": "allowed"}) is None
    assert claude_reading("nonsense") is None


def test_telemetry_keeps_the_newest_and_tells_listeners():
    book = QuotaTelemetry()
    heard = []
    stop = book.subscribe(heard.append)
    book.publish(reading(10, 10, at=NOW))
    book.publish(reading(5, 5, at=NOW - 10))  # older, arriving late
    assert book.latest("claude-code").windows["five_hour"].used == 10
    stop()
    book.publish(reading(20, 20, at=NOW + 10))
    assert len(heard) == 1


# -- forecast and estimates --------------------------------------------------------------
def test_the_user_rates_learn_from_clean_intervals_only():
    hours = Routine(hours={5: [(0, 1440)]}, timezone="UTC")
    rates = UserRates()
    learned = rates.learn("five_hour", hours, NOW, NOW + HOUR, spent=20.0)
    assert learned.busy["five_hour"] == pytest.approx(0.7 * 12 + 0.3 * 20)
    assert learned.samples == 1
    mixed = Routine(hours={5: [(0, 90)]}, timezone="UTC")
    assert rates.learn("five_hour", mixed, NOW, NOW + 2 * HOUR, spent=5.0) is rates
    assert rates.learn("five_hour", hours, NOW, NOW + 60, spent=5.0) is rates  # too short


def test_calibration_moves_toward_measured_prices_within_bounds():
    calibration = Calibration(price={"five_hour": 2.0, "seven_day": 0.5})
    learned = calibration.learn(1_000_000, {"five_hour": 10.0, "seven_day": 0.5})
    assert learned.price["five_hour"] == pytest.approx(0.65 * 2 + 0.35 * 4)  # 10 is clamped to 2x
    assert learned.price["seven_day"] == pytest.approx(0.5)
    assert calibration.learn(1000, {"five_hour": 99}) is calibration


def test_answers_are_read_for_size_verdict_and_checkpoint():
    assert size_in("Plan...\nSize: L\n") == "L"
    assert size_in("**Size estimate:** medium") == "M"
    assert size_in("no size here") is None
    assert verdict_in("Verdict: pass") is True and verdict_in("Verdict: **FAIL** - tests") is False
    assert verdict_in("looks fine") is None
    assert checkpoint_of("work...\n\n## Checkpoint\nDone: A. Left: B.") == "Done: A. Left: B."
    assert kinds_for("") == ["scout", "work", "verify"]
    assert kinds_for("charts") == ["scout", "work", "extra", "verify"]


def test_the_routine_knows_local_hours_and_chains_past_midnight():
    moscow = Routine(timezone="Europe/Moscow")
    tz = ZoneInfo("Europe/Moscow")
    monday_noon = datetime(2026, 10, 19, 12, 0, tzinfo=tz).timestamp()
    assert moscow.busy_at(monday_noon)
    assert moscow.busy_until(monday_noon) == datetime(2026, 10, 19, 19, 0, tzinfo=tz).timestamp()
    night = Routine(hours={0: [(1200, 1440)], 1: [(0, 120)]}, timezone="UTC")
    start = datetime(2026, 10, 19, 21, 0, tzinfo=UTC).timestamp()
    assert night.busy_until(start) == datetime(2026, 10, 20, 2, 0, tzinfo=UTC).timestamp()


# -- the store ---------------------------------------------------------------------------
def test_the_store_keeps_everything_across_reopening(tmp_path):
    path = tmp_path / "lazy.db"
    store = LazyStore(path)
    task = task_of(*REPORT)
    task.note("Created")
    store.save_task(task)
    store.save_settings("u", concept_settings())
    store.add_run(Run("r1", "u", "claude-code", "t1", "s1", NOW - 60, NOW, tokens=5, used={"five_hour": 1.0}))
    store.add_reading(reading(10, 20))
    store.save_rates("u", UserRates(samples=3))
    store.save_calibration("claude-code", Calibration(samples=2))
    store.close()

    again = LazyStore(path)
    assert again.task("u", "t1").steps[1].kind == "work"
    assert again.task("other", "t1") is None  # a task is its owner's alone
    assert again.users() == ["u"]
    assert again.settings("u").allowance("seven_day").safety == 5
    assert again.runs("claude-code", NOW - 120)[0].used == {"five_hour": 1.0}
    assert again.latest_reading("claude-code").windows["seven_day"].used == 20
    assert again.rates("u").samples == 3 and again.calibration("claude-code").samples == 2
    assert again.delete_task("u", "t1") and again.task("u", "t1") is None
