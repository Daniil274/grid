"""When each step runs, whether each deadline holds, and what would make it hold.

The planner is arithmetic over time: no model is asked, so it reruns on every
change - a new task, a moved deadline, a new reading, a finished step.

Tasks are taken earliest deadline first ("do it now" ahead of all, then
priority, then age), each step as early as everything allows:

1. outside the user's hours, and ending before they begin (unless the task
   may run in them);
2. when the single runner is free - one step at a time;
3. when its cost fits the room in every window (core.lazy.budget) of the
   period it starts in. Room in a period only shrinks as steps are booked,
   so a step that does not fit waits for the next period of that window.

Running as early as the budget allows is the point: uncertainty is resolved
before the deadline is near. A step that cannot be placed within the horizon
blocks its task; that is "no plan found", not proof that none exists.

For a task that misses its deadline the planner tries each way out, one at a
time, by planning again: a later deadline, a larger share of one window, the
optional part dropped, the user's hours allowed. Each that works is offered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from core.lazy.budget import headroom
from core.lazy.estimate import Calibration
from core.lazy.forecast import UserRates
from core.lazy.model import PRIORITIES, Settings, Task
from core.lazy.situation import Situation
from core.lazy.windows import DAY, WINDOWS, Periods

#: Steps start on this grid (seconds).
SLOT = 300.0
HORIZON = 14 * DAY
#: The chart shows this far ahead, one point per CURVE_STEP.
CHART_SPAN = 7 * DAY
CURVE_STEP = 1800.0


@dataclass(frozen=True)
class Placement:
    task_id: str
    step_id: str
    kind: str
    start: float
    end: float
    cost: Dict[str, float]
    running: bool = False

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "step_id": self.step_id, "kind": self.kind,
            "start": self.start, "end": self.end, "running": self.running,
            "cost": {name: round(points, 2) for name, points in self.cost.items()},
        }


@dataclass(frozen=True)
class Wait:
    """Why a step does not start sooner."""

    code: str
    text: str
    until: Optional[float] = None
    window: Optional[str] = None
    need: Optional[float] = None
    room: Optional[float] = None

    def to_dict(self) -> dict:
        data = {"code": self.code, "text": self.text}
        for key in ("until", "window", "need", "room"):
            value = getattr(self, key)
            if value is not None:
                data[key] = round(value, 2) if isinstance(value, float) and key in ("need", "room") else value
        return data


@dataclass(frozen=True)
class Option:
    kind: str
    text: str
    #: What taking it changes: {"task": {...}} or {"settings": {...}}.
    change: dict

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "change": self.change}


@dataclass
class TaskPlan:
    task_id: str
    state: str
    finish_at: Optional[float] = None
    next_start: Optional[float] = None
    #: The scheduled release this round waits for (``Task.not_before``); the
    #: forecast ``next_start`` is never earlier than it.
    scheduled_release: Optional[float] = None
    wait: Optional[Wait] = None
    late_by: float = 0.0
    cost: Dict[str, float] = field(default_factory=dict)
    options: List[Option] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "finish_at": self.finish_at,
            "next_start": self.next_start,
            "scheduled_release": self.scheduled_release,
            "wait": self.wait.to_dict() if self.wait else None,
            "late_by": self.late_by,
            "cost": {name: round(points, 2) for name, points in self.cost.items()},
            "options": [option.to_dict() for option in self.options],
        }


@dataclass
class Plan:
    now: float
    horizon_end: float
    placements: List[Placement]
    tasks: Dict[str, TaskPlan]
    windows: Dict[str, dict]

    def to_dict(self) -> dict:
        return {
            "now": self.now,
            "horizon_end": self.horizon_end,
            "placements": [placement.to_dict() for placement in self.placements],
            "tasks": {task_id: plan.to_dict() for task_id, plan in self.tasks.items()},
            "windows": self.windows,
        }

    def next_placement(self) -> Optional[Placement]:
        upcoming = [p for p in self.placements if not p.running]
        return min(upcoming, key=lambda p: p.start, default=None)


# -- the windows over time ----------------------------------------------------------
class _Ledger:
    """Room per window and period: what the user is expected to take, what is booked."""

    def __init__(self, situation: Situation, settings: Settings, rates: UserRates) -> None:
        self.now = situation.now
        self.settings = settings
        self.rates = rates
        self.situation = situation
        self.periods: Dict[str, Periods] = {
            name: Periods.from_reset(spec, self.now, situation.windows[name].resets_at)
            for name, spec in WINDOWS.items()
        }
        self.booked: Dict[Tuple[str, int], float] = {}
        self._user: Dict[Tuple[str, int], float] = {}

    def user_plus(self, name: str, index: int) -> float:
        """``U+``: the user's use of a period, known plus forecast to its end."""
        key = (name, index)
        if key not in self._user:
            start, end = self.periods[name].bounds(index)
            routine = self.settings.routine
            if index == 0:
                value = self.situation.windows[name].user + self.rates.amount(name, routine, self.now, end)
            else:
                value = self.rates.amount(name, routine, start, end)
            self._user[key] = value
        return self._user[key]

    def agents(self, name: str, index: int) -> float:
        measured = self.situation.windows[name].agents if index == 0 else 0.0
        return measured + self.booked.get((name, index), 0.0)

    def room(self, name: str, t: float) -> Tuple[float, int]:
        index = self.periods[name].index(t)
        reserved = self.situation.windows[name].reserved if index == 0 else 0.0
        return (
            headroom(
                self.settings.allowance(name),
                agents=self.agents(name, index), reserved=reserved, user=self.user_plus(name, index),
            ),
            index,
        )

    def book(self, cost: Dict[str, float], t: float) -> None:
        for name, points in cost.items():
            index = self.periods[name].index(t)
            self.booked[(name, index)] = self.booked.get((name, index), 0.0) + points


# -- planning -----------------------------------------------------------------------
def _order(task: Task) -> tuple:
    rank = PRIORITIES.index(task.priority) if task.priority in PRIORITIES else 1
    return (0 if task.run_now else 1, task.deadline if task.deadline is not None else math.inf, -rank, task.created_at)


def _slot_after(t: float) -> float:
    return math.ceil(t / SLOT) * SLOT


def plan(
    tasks: Iterable[Task],
    settings: Settings,
    situation: Situation,
    rates: UserRates,
    calibration: Calibration,
    *,
    offer: bool = True,
    series_spend: Optional[Mapping[str, float]] = None,
) -> Plan:
    """The plan for the user's tasks now; *offer* adds the ways out for missed deadlines.

    *series_spend* is what each series cost in the rolling seven days the caller
    measures (``series_id`` -> weekly points, from the ledger). A series' cap is
    spent across its rounds, so a new round does not open a fresh budget; a task
    that does not repeat keeps counting its own closed steps.
    """
    now = situation.now
    horizon_end = now + HORIZON
    ledger = _Ledger(situation, settings, rates)
    routine = settings.routine
    busy: List[Tuple[float, float]] = []
    placements: List[Placement] = []
    plans: Dict[str, TaskPlan] = {}
    tasks = list(tasks)

    # A step already running holds the runner, and its booking is the reservation.
    for task in tasks:
        step = task.running_step
        if step is None:
            continue
        started = step.started_at or now
        end = max(now + SLOT, started + step.minutes * 60)
        busy.append((started, end))
        placements.append(Placement(task.id, step.id, step.kind, started, end, calibration.cost(step.tokens), running=True))

    def search(ready: float, cost: Dict[str, float], length: float, my_hours: bool) -> Tuple[Optional[float], Optional[Wait]]:
        t = max(now, ready)
        # The constraint met last is the one that decided the start.
        last: Optional[Wait] = None
        for name, points in cost.items():
            share = settings.allowance(name).share
            if points > share + 1e-9:
                return None, Wait(
                    "too_big", f"The step needs more of the {WINDOWS[name].label.lower()} than the agents' share",
                    window=name, need=points, room=share,
                )
        while t < horizon_end:
            reason: Optional[Wait] = None
            nxt = t
            if not my_hours and routine.busy_at(t):
                nxt = routine.busy_until(t)
                reason = Wait("my_hours", "Your hours: the subscription is yours", until=nxt)
            elif not my_hours and routine.next_busy(t, t + length) < t + length:
                nxt = routine.busy_until(routine.next_busy(t, t + length))
                reason = Wait("my_hours", "Would run into your hours", until=nxt)
            else:
                clash = next(((a, b) for a, b in sorted(busy) if a < t + length and b > t), None)
                if clash is not None:
                    nxt = clash[1]
                    reason = Wait("busy", "The runner has another step then", until=nxt)
                else:
                    for name, points in cost.items():
                        room, index = ledger.room(name, t)
                        if points > room + 1e-9:
                            nxt = ledger.periods[name].bounds(index)[1]
                            reason = Wait(
                                "budget", f"Not enough room in the {WINDOWS[name].label.lower()}",
                                until=nxt, window=name, need=points, room=room,
                            )
                            break
            if reason is None:
                return t, last
            last = reason
            t = max(_slot_after(nxt), t + SLOT) if nxt <= t else nxt
        return None, last or Wait("horizon", "No room found in the next two weeks")

    for task in sorted(tasks, key=_order):
        if task.finished:
            continue
        cost_left = {name: 0.0 for name in WINDOWS}
        for step in task.open_steps:
            for name, points in calibration.cost(step.tokens).items():
                cost_left[name] += points
        result = TaskPlan(task.id, "flexible", cost=cost_left)
        plans[task.id] = result
        if task.status == "paused":
            result.state = "paused"
            result.wait = Wait("paused", "Paused")
            continue
        if not task.open_steps:
            result.state = "done"
            continue
        running = task.running_step
        ready = next((p.end for p in placements if p.running and p.task_id == task.id), now)
        after = Wait("after_step", "After the step before it", until=ready)
        if task.retry_at is not None and running is None and task.retry_at > ready:
            ready = task.retry_at
            after = Wait("retry", "Trying again later", until=ready)
        result.scheduled_release = task.not_before
        if task.not_before is not None and running is None and task.not_before > ready:
            # The scheduled release: the plan places the round no earlier, and
            # the loop wakes at it. A forecast may be later (no room yet); it is
            # never earlier than the release.
            ready = task.not_before
            after = Wait("not_before", "Scheduled to start later", until=ready)
        if running is not None:
            result.state = "running"
        if settings.paused:
            if running is None:
                result.state = "paused"
                result.wait = Wait("pool_paused", "Lazy agents are paused")
            continue
        if task.series_id and series_spend is not None:
            # One budget for the whole series over the rolling window, not a
            # fresh one per round.
            spent_week = float(series_spend.get(task.series_id, 0.0))
        else:
            spent_week = sum(step.used.get("seven_day", 0.0) for step in task.steps if not step.open)
        finish: Optional[float] = ready if running is not None else None
        blocked = False
        for step in task.steps:
            if step.status != "pending":
                continue
            cost = calibration.cost(step.tokens)
            if task.cap is not None and spent_week + cost.get("seven_day", 0.0) > task.cap + 1e-9:
                result.wait = result.wait or Wait("cap", "Over this task's own budget cap", need=cost.get("seven_day"), room=max(0.0, task.cap - spent_week))
                blocked = True
                break
            length = step.minutes * 60.0
            start, why = search(ready, cost, length, task.run_now)
            if start is None:
                result.wait = result.wait or why
                blocked = True
                break
            if result.next_start is None:
                result.next_start = start
                if start > max(now, ready) + SLOT / 2 and why is not None:
                    result.wait = why
                elif (
                    start > now + SLOT / 2 and task.not_before is not None
                    and task.not_before > now and ready >= task.not_before
                ):
                    # Nothing else holds it: the release does, and the answer
                    # says so rather than leaving the delay unexplained.
                    result.wait = after
                elif start > now + SLOT / 2 and why is None and ready > now:
                    result.wait = after
            ledger.book(cost, start)
            busy.append((start, start + length))
            placements.append(Placement(task.id, step.id, step.kind, start, start + length, cost))
            spent_week += cost.get("seven_day", 0.0)
            ready = start + length
            finish = ready
        result.finish_at = None if blocked else finish
        if blocked:
            result.state = "blocked"
        elif running is None:
            if task.deadline is None:
                result.state = "flexible"
            elif finish is not None and finish <= task.deadline:
                result.state = "on_track"
            else:
                result.state = "at_risk"
        if task.deadline is not None and result.finish_at is not None:
            result.late_by = max(0.0, result.finish_at - task.deadline)
        if running is not None and task.deadline is not None and (blocked or result.late_by > 0):
            result.state = "running_late"

    windows = _projection(ledger, placements, situation, settings, rates, now)
    result_plan = Plan(now=now, horizon_end=horizon_end, placements=sorted(placements, key=lambda p: p.start), tasks=plans, windows=windows)
    if offer:
        for task in tasks:
            task_plan = plans.get(task.id)
            if task_plan is not None and task.deadline is not None and task_plan.state in ("at_risk", "blocked", "running_late"):
                task_plan.options = ways_out(
                    task, tasks, settings, situation, rates, calibration, task_plan, series_spend
                )
    return result_plan


def _projection(
    ledger: _Ledger, placements: List[Placement], situation: Situation, settings: Settings, rates: UserRates, now: float
) -> Dict[str, dict]:
    """Per window, for the chart: periods with their room, and use over time."""
    result: Dict[str, dict] = {}
    end = now + CHART_SPAN
    for name, spec in WINDOWS.items():
        periods = ledger.periods[name]
        allowance = settings.allowance(name)
        rows = []
        index = 0
        while True:
            start, stop = periods.bounds(index)
            if start >= end:
                break
            rows.append({
                "start": start, "end": stop,
                "user": round(ledger.user_plus(name, index), 2),
                "agents": round(ledger.agents(name, index), 2),
                "room": round(ledger.room(name, max(now, start))[0], 2),
            })
            index += 1
            if index > 400:
                break
        times, user, agents = [], [], []
        t = now
        while t <= end:
            index = periods.index(t)
            start, _stop = periods.bounds(index)
            if index == 0:
                used_user = situation.windows[name].user + rates.amount(name, settings.routine, now, t)
                used_agents = situation.windows[name].agents
            else:
                used_user = rates.amount(name, settings.routine, start, t)
                used_agents = 0.0
            used_agents += sum(
                p.cost.get(name, 0.0) for p in placements
                if not p.running and periods.index(p.start) == index and p.start <= t
            )
            times.append(t)
            user.append(round(min(100.0, used_user), 2))
            agents.append(round(min(100.0, used_agents), 2))
            t += CURVE_STEP
        result[name] = {
            "label": spec.label,
            "share": allowance.share,
            "safety": allowance.safety,
            "periods": rows,
            "curve": {"t": times, "user": user, "agents": agents},
        }
    return result


# -- ways out of a missed deadline ----------------------------------------------------
def _on_time(
    task_id: str, tasks: List[Task], settings: Settings, situation: Situation, rates: UserRates,
    calibration: Calibration, series_spend: Optional[Mapping[str, float]] = None,
) -> Tuple[bool, Optional[float]]:
    trial = plan(tasks, settings, situation, rates, calibration, offer=False, series_spend=series_spend).tasks.get(task_id)
    if trial is None:
        return False, None
    return trial.state in ("on_track", "running"), trial.finish_at


def _with_task(tasks: List[Task], task_id: str, **changes) -> List[Task]:
    result = []
    for task in tasks:
        if task.id == task_id:
            copy = replace(task, steps=[replace(step) for step in task.steps])
            for key, value in changes.items():
                if key == "drop_optional":
                    copy.steps = [step for step in copy.steps if not (step.optional and step.status == "pending")]
                else:
                    setattr(copy, key, value)
            result.append(copy)
        else:
            result.append(task)
    return result


def ways_out(
    task: Task, tasks: List[Task], settings: Settings, situation: Situation, rates: UserRates,
    calibration: Calibration, current: TaskPlan, series_spend: Optional[Mapping[str, float]] = None,
) -> List[Option]:
    options: List[Option] = []
    # 1. Keep everything as it is, at a later deadline.
    if current.finish_at is not None:
        later = math.ceil(current.finish_at / 900.0) * 900.0
        options.append(Option("deadline", "Keep the plan and move the deadline", {"task": {"deadline": later}}))
    # 2. Drop the optional part.
    if any(step.optional and step.status == "pending" for step in task.steps):
        ok, _ = _on_time(task.id, _with_task(tasks, task.id, drop_optional=True), settings, situation, rates, calibration, series_spend)
        if ok:
            options.append(Option("drop_optional", "Drop the optional part", {"task": {"drop_optional": True}}))
    # 3. Let it run in the user's hours.
    if not task.run_now and not settings.routine.hours == {}:
        ok, _ = _on_time(task.id, _with_task(tasks, task.id, run_now=True), settings, situation, rates, calibration, series_spend)
        if ok:
            options.append(Option("my_hours", "Let it run during your hours too", {"task": {"run_now": True}}))
    # 4. A larger share of one window: the least that is enough.
    for name, spec in WINDOWS.items():
        current_share = settings.allowance(name).share
        if current_share >= 100.0:
            continue
        ok, _ = _on_time(task.id, tasks, settings.with_allowance(name, share=100.0), situation, rates, calibration, series_spend)
        if not ok:
            continue
        lo, hi = current_share, 100.0
        while hi - lo > 0.5:
            mid = (lo + hi) / 2
            fits, _ = _on_time(task.id, tasks, settings.with_allowance(name, share=mid), situation, rates, calibration, series_spend)
            lo, hi = (lo, mid) if fits else (mid, hi)
        share = math.ceil(hi * 2) / 2
        options.append(Option(
            "share", f"Let agents use {share:g}% of the {spec.label.lower()} (now {current_share:g}%)",
            {"settings": {"allowances": {name: {"share": share}}}},
        ))
    return options
