"""The lazy tasks of a server: their plans, and the loop that runs their steps.

Between steps nothing runs: the loop sleeps until the earliest planned step
(or a change wakes it - a new task, an edit, a reading, a finished step),
then plans again and starts that step if it is still due. Planning is
arithmetic (core.lazy.planner); the only model calls are the steps' own.

A task spends its agent's subscription. The user's tasks form one pool per
subscription - the settings' own, and one for each other subscription their
agents run on - each planned on its own windows. One step runs at a time per
pool, so a ChatGPT task and a Claude Code task go side by side. When a step
ends the service

- writes what it took to the ledger: measured from the provider's readings
  before and after it when they are fresh, else estimated from its tokens -
  and a measured run calibrates the prices of later steps;
- applies its answer: a scout's size re-estimates the steps after it, a
  failed verification adds a fix and a second verification;
- or, for a step stopped short, keeps it pending to continue later, and backs
  off one that could not start.

A task done with a ``repeat`` sets up its next round: the same work with
fresh steps and a new chat, due that long after the last deadline.

The provider's readings also teach the user's own rates (core.lazy.forecast):
use between two readings that the pool's runs do not explain is the user's.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.lazy.estimate import SIZES, Calibration, checkpoint_of, kinds_for, size_in, sized, verdict_in
from core.lazy.model import PRIORITIES, Run, Settings, Step, Task, new_id
from core.lazy.planner import CHART_SPAN, Plan, plan
from core.lazy.situation import FRESH, Situation, from_reading
from core.lazy.store import LazyStore
from core.lazy.windows import DAY, HOUR, WINDOWS
from core.quota_telemetry import CLAUDE_CODE, QuotaReading, QuotaTelemetry, telemetry
from web_chat.lazy.accounts import (
    CHATGPT, LABELS, agent_choice, agent_choices, estimated_reading, pool_key, reports_windows,
)
from web_chat.lazy.runner import StepOutcome, StepRunner

logger = logging.getLogger("grid.web_chat.lazy")

#: The loop looks again at least this often, to follow forecasts and readings.
TICK = 300.0
#: A step that keeps failing to start or ending in errors is given up after this many tries.
MAX_ATTEMPTS = 4
#: How long a refused or failed step waits before it is tried again, per attempt.
BACKOFF = 15 * 60.0
#: Readings this far apart, in one period, teach the user's rates.
LEARN_SPAN = 30 * 60.0
#: Over its share by more than this, a running step is stopped.
TOLERANCE = 0.5


class LazyError(ValueError):
    """A request the lazy agents cannot take, in words for the user."""


@dataclass
class Running:
    task_id: str
    step_id: str
    job: asyncio.Task
    started_at: float
    baseline: Optional[QuotaReading]
    account: str


class LazyAgents:
    def __init__(
        self,
        store: LazyStore,
        spaces: Any,
        *,
        usage: Any = None,
        quota: Optional[QuotaTelemetry] = None,
        runner: Optional[Any] = None,
        snapshot: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
        tick: float = TICK,
    ) -> None:
        self.store = store
        self.spaces = spaces
        self.usage = usage
        self.quota = quota or telemetry()
        self.runner = runner or StepRunner(spaces, clock=clock)
        #: The server's one picture of subscriptions (web_chat.usage_snapshot); its
        #: ChatGPT reading replaces the token estimate as soon as one is fetched.
        self.snapshot = snapshot
        self.clock = clock
        self.tick = tick
        #: The step running in each pool: (user, subscription) -> its run.
        self._running: Dict[Tuple[str, str], Running] = {}
        #: Stops the user asked for, by task: read by the running step's watcher.
        self._stops: Dict[str, str] = {}
        self._learned: Dict[Tuple[str, str], Tuple[float, float, Optional[float]]] = {}
        self._wake: Optional[asyncio.Event] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._unsubscribe: Optional[Callable[[], None]] = None

    # -- lifecycle ---------------------------------------------------------------------
    async def start(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._recover()
        stored = self.store.latest_reading(CLAUDE_CODE)
        if stored is not None and self.quota.latest(CLAUDE_CODE) is None:
            self.quota.publish(stored)
        self._unsubscribe = self.quota.subscribe(self._on_reading)
        self._loop_task = asyncio.create_task(self._loop(), name="lazy-agents")

    async def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        for task in [self._loop_task, *(running.job for running in self._running.values())]:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task

    def wake(self) -> None:
        if self._wake is None:
            return
        if self._event_loop is not None and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(self._wake.set)
        else:
            self._wake.set()

    def _recover(self) -> None:
        """A step marked running when the server stopped continues from where it was."""
        for user_id in self.store.users():
            for task in self.store.tasks(user_id, finished=False):
                step = task.running_step
                if step is None:
                    continue
                step.status = "pending"
                step.resume = True
                step.reason = "Interrupted by a server restart"
                task.note(f"{step.title} was interrupted by a server restart; it will continue", kind="warning")
                self.store.save_task(task)

    async def _loop(self) -> None:
        while True:
            try:
                delay = await self._tick()
            except Exception:  # noqa: BLE001 - one bad tick must not end the loop
                logger.exception("Lazy agents' tick failed")
                delay = 60.0
            assert self._wake is not None
            self._wake.clear()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)

    async def _tick(self) -> float:
        """Start every step that is due; how long until the next one is."""
        now = self.clock()
        delay = self.tick
        for user_id in self.store.users():
            settings = self.store.settings(user_id)
            for account, tasks in self.pools(settings, self.store.tasks(user_id, finished=False)).items():
                if not tasks or (user_id, account) in self._running:
                    continue
                pool = self.pool_settings(settings, account)
                current = self.plan_for(user_id, pool, tasks=tasks, offer=False)
                upcoming = current.next_placement()
                if upcoming is None:
                    continue
                if upcoming.start <= now + 1.0:
                    await self._read_reading(user_id, pool)
                    self._launch(user_id, upcoming.task_id, upcoming.step_id, pool)
                else:
                    delay = min(delay, upcoming.start - now)
        return max(1.0, delay)

    async def _read_reading(self, user_id: str, settings: Settings) -> None:
        """A ChatGPT pool plans on the plan's own windows: read them when they are due."""
        if self.snapshot is None or settings.account != CHATGPT:
            return
        ensure = getattr(self.snapshot, "ensure_reading", None)
        if ensure is not None:
            with suppress(Exception):  # a failed read plans on the estimate, as before
                await ensure(user_id, CHATGPT)

    async def ensure_fresh(self, user: Any) -> None:
        """Before the page shows a picture, read the free sources this user may see."""
        if self.snapshot is None:
            return
        refresh = getattr(self.snapshot, "refresh_free", None)
        if refresh is not None:
            with suppress(Exception):  # the page still shows cached numbers and their age
                await refresh(user)

    # -- the picture ---------------------------------------------------------------------
    def situation(self, user_id: str, settings: Settings) -> Situation:
        now = self.clock()
        key = pool_key(settings.account, user_id)
        if reports_windows(settings.account):
            reading = self.quota.latest(CLAUDE_CODE) or self.store.latest_reading(CLAUDE_CODE)
            estimate = False
        elif self.snapshot is not None:
            # The server's one picture answers for a ChatGPT pool; a pool of any
            # other subscription keeps its token estimate until it reports windows.
            reading, estimate = self.snapshot.reading_for(user_id, settings, self.usage, now)
        else:
            reading = estimated_reading(self.usage, user_id, settings, now)
            estimate = True
        reserved: Dict[str, float] = {}
        running = self._running.get((user_id, settings.account))
        if running is not None:
            task = self.store.task(user_id, running.task_id)
            step = task.step(running.step_id) if task else None
            if step is not None:
                reserved = self.calibration(settings.account, user_id).cost(step.tokens)
        return from_reading(
            reading, account=key, runs=self.store.runs(key, now - 8 * DAY), reserved=reserved,
            rates=self.store.rates(user_id), routine=settings.routine, now=now, estimate=estimate,
        )

    def calibration(self, account: str, user_id: str) -> Calibration:
        return self.store.calibration(pool_key(account, user_id))

    # -- pools: one per subscription the user's tasks spend ------------------------------
    @staticmethod
    def pools(settings: Settings, tasks: List[Task]) -> Dict[str, List[Task]]:
        """*tasks* by the subscription each spends, the settings' own first.

        A task spends its agent's subscription (``Task.account``); one created
        before tasks had their own, or on the settings' subscription, has none
        and spends the settings' one. Each pool is planned on its own windows
        and runs one step at a time, beside the others.
        """
        pools: Dict[str, List[Task]] = {settings.account: []}
        for task in tasks:
            pools.setdefault(task.account or settings.account, []).append(task)
        return pools

    @staticmethod
    def pool_settings(settings: Settings, account: str) -> Settings:
        """*settings* for the pool of *account*: the same hours, shares and pause."""
        return settings if account == settings.account else replace(settings, account=account)

    def plan_for(self, user_id: str, settings: Settings, *, tasks: Optional[List[Task]] = None, offer: bool = True) -> Plan:
        """The plan of the pool *settings* names; its tasks unless *tasks* are given."""
        if tasks is None:
            primary = self.store.settings(user_id).account
            tasks = [
                task for task in self.store.tasks(user_id, finished=False)
                if (task.account or primary) == settings.account
            ]
        return plan(
            tasks, settings, self.situation(user_id, settings), self.store.rates(user_id),
            self.calibration(settings.account, user_id), offer=offer,
        )

    def settings_for(self, user_id: str, space: Any = None, timezone: Optional[str] = None) -> Settings:
        """The user's settings; a first visit picks the subscription their agents run on."""
        if self.store.has_settings(user_id) or space is None:
            return self.store.settings(user_id)
        settings = Settings()
        accounts = {choice.account for choice in agent_choices(space) if choice.account}
        if CLAUDE_CODE not in accounts and "chatgpt" in accounts:
            settings.account = "chatgpt"
        if timezone:
            settings.routine = replace(settings.routine, timezone=timezone)
        self.store.save_settings(user_id, settings)
        return settings

    def state(self, user_id: str, space: Any, *, timezone: Optional[str] = None) -> dict:
        """Everything the page shows: settings, the windows now, the plan, the tasks, the agents."""
        settings = self.settings_for(user_id, space, timezone)
        tasks = self.store.tasks(user_id)
        pictures = []
        for account, pool_tasks in self.pools(settings, [task for task in tasks if not task.finished]).items():
            pool = self.pool_settings(settings, account)
            pool_situation = self.situation(user_id, pool)
            pool_plan = plan(
                pool_tasks, pool, pool_situation, self.store.rates(user_id), self.calibration(account, user_id),
            )
            pictures.append((pool, pool_situation, pool_plan))
        _, situation, current = pictures[0]
        calibration = self.calibration(settings.account, user_id)
        merged = current.to_dict()
        for _, _, other in pictures[1:]:
            extra = other.to_dict()
            merged["placements"] = sorted([*merged["placements"], *extra["placements"]], key=lambda p: p.get("start") or 0)
            merged["tasks"] = {**merged["tasks"], **extra["tasks"]}
        running = next((run for (owner, _), run in self._running.items() if owner == user_id), None)
        return {
            "now": self.clock(),
            "settings": settings.to_dict(),
            "account": self._account_view(user_id, settings, situation),
            "windows": self._windows_view(settings, situation, current),
            # The other subscriptions the user's tasks spend, each with its own windows.
            "pools": [
                {"account": self._account_view(user_id, pool, pool_situation),
                 "windows": self._windows_view(pool, pool_situation, pool_plan)}
                for pool, pool_situation, pool_plan in pictures[1:]
                if pool_plan.tasks
            ],
            "plan": merged,
            # The user's hours over the chart, in their own zone: the page draws them as they are.
            "my_hours": [list(span) for span in settings.routine.intervals(situation.now - HOUR, situation.now + CHART_SPAN)],
            "tasks": [
                self._task_view(task, self.calibration(task.account or settings.account, user_id))
                for task in reversed(tasks)
            ],
            "running": {"task_id": running.task_id, "step_id": running.step_id, "started_at": running.started_at} if running else None,
            "agents": [choice.to_dict() for choice in agent_choices(space)],
            "sizes": {size: {kind: {"tokens": t, "minutes": m} for kind, (t, m) in kinds.items()} for size, kinds in SIZES.items()},
        }

    def _account_view(self, user_id: str, settings: Settings, situation: Situation) -> dict:
        calibration = self.calibration(settings.account, user_id)
        return {
            "key": settings.account,
            "label": LABELS.get(settings.account, settings.account),
            "mode": situation.mode,
            "observed_at": situation.observed_at,
            "status": situation.status,
            "overage_allowed": situation.overage_allowed,
            "calibrated_runs": calibration.samples,
            "learned_readings": self.store.rates(user_id).samples,
        }

    @staticmethod
    def _windows_view(settings: Settings, situation: Situation, current: Plan) -> dict:
        windows = {}
        for name, spec in WINDOWS.items():
            now_window = situation.windows[name]
            allowance = settings.allowance(name)
            projected = current.windows[name]["periods"][0] if current.windows[name]["periods"] else {}
            windows[name] = {
                "label": spec.label,
                **now_window.to_dict(),
                "share": allowance.share,
                "safety": allowance.safety,
                "forecast": round(max(0.0, projected.get("user", now_window.user) - now_window.user), 2),
                "room": projected.get("room", 0.0),
            }
        return windows

    @staticmethod
    def _task_view(task: Task, calibration: Calibration) -> dict:
        data = task.to_dict()
        data["account_label"] = LABELS.get(task.account, task.account) if task.account else None
        for step, view in zip(task.steps, data["steps"]):
            view["title"] = step.title
            view["estimate"] = {name: round(points, 2) for name, points in calibration.cost(step.tokens).items()}
        return data

    # -- tasks ---------------------------------------------------------------------------
    def draft(self, user_id: str, space: Any, payload: dict) -> Task:
        """A task as the form describes it, checked but not saved."""
        settings = self.settings_for(user_id, space)
        title = str(payload.get("title") or "").strip()
        system, agent = payload.get("system"), payload.get("agent")
        if not system or not agent:
            raise LazyError("Pick the agent that does the work.")
        choice = agent_choice(space, system, agent)
        if choice.problem:
            raise LazyError(choice.problem)
        if choice.account not in LABELS:
            raise LazyError(f"{choice.label} does not run on a subscription lazy agents can spend.")
        size = payload.get("size") if payload.get("size") in SIZES else "M"
        task = Task(
            id=new_id("lazy"),
            user_id=user_id,
            title=title[:160] or "Untitled task",
            brief=str(payload.get("brief") or "")[:8000],
            acceptance=str(payload.get("acceptance") or "")[:2000],
            optional=str(payload.get("optional") or "")[:2000],
            deadline=_deadline(payload.get("deadline")),
            priority=payload.get("priority") if payload.get("priority") in PRIORITIES else "normal",
            size=size,
            system=system,
            agent=agent,
            cap=_points(payload.get("cap")),
            repeat=_repeat(payload.get("repeat_hours")),
            # The agent's subscription; None when it is the settings' own.
            account=None if choice.account == settings.account else choice.account,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        for index, kind in enumerate(kinds_for(task.optional), start=1):
            task.steps.append(Step.make(kind, size, index, optional=kind == "extra"))
        return task

    def create_task(self, user_id: str, space: Any, payload: dict) -> Task:
        if not str(payload.get("title") or "").strip():
            raise LazyError("Give the task a title.")
        task = self.draft(user_id, space, payload)
        task.note("Created")
        self.store.save_task(task)
        self.wake()
        return task

    def preview_new(self, user_id: str, space: Any, payload: dict) -> dict:
        """Where a task like this would land among the others - nothing is saved."""
        task = self.draft(user_id, space, payload)
        settings = self.store.settings(user_id)
        settings = self.pool_settings(settings, task.account or settings.account)
        tasks = self.pools(self.store.settings(user_id), self.store.tasks(user_id, finished=False)).get(settings.account, [])
        before = self.plan_for(user_id, settings, tasks=tasks, offer=False)
        after = self.plan_for(user_id, settings, tasks=[*tasks, task])
        calibration = self.calibration(settings.account, user_id)
        others = [
            {"task_id": other.id, "title": other.title, "before": before.tasks[other.id].to_dict(), "after": after.tasks[other.id].to_dict()}
            for other in tasks
            if other.id in before.tasks and other.id in after.tasks
            and before.tasks[other.id].state != after.tasks[other.id].state
        ]
        return {
            "plan": after.tasks[task.id].to_dict(),
            "steps": [
                {"kind": step.kind, "title": step.title, "tokens": step.tokens, "minutes": step.minutes,
                 "estimate": {name: round(points, 2) for name, points in calibration.cost(step.tokens).items()}}
                for step in task.steps
            ],
            "placements": [p.to_dict() for p in after.placements if p.task_id == task.id],
            "others": others,
        }

    def task(self, user_id: str, task_id: str) -> Task:
        task = self.store.task(user_id, task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def preview(self, user_id: str, task_id: str, changes: dict) -> dict:
        """What *changes* would do to this task's plan and to the others' - nothing is saved."""
        primary = self.store.settings(user_id)
        current = self.store.task(user_id, task_id)
        settings = self.pool_settings(primary, (current.account if current else None) or primary.account)
        tasks = self.pools(primary, self.store.tasks(user_id, finished=False)).get(settings.account, [])
        before = self.plan_for(user_id, settings, tasks=tasks)
        trial_tasks, trial_settings = list(tasks), settings
        if "task" in changes or any(key in changes for key in EDITABLE):
            target = next((t for t in tasks if t.id == task_id), None)
            if target is None:
                raise KeyError(task_id)
            edited = _edited(_copy(target), changes.get("task", changes), self.clock())
            trial_tasks = [edited if t.id == task_id else t for t in tasks]
        if "settings" in changes:
            trial_settings = _with_settings(settings, changes["settings"])
        after = plan(
            trial_tasks, trial_settings, self.situation(user_id, trial_settings), self.store.rates(user_id),
            self.calibration(trial_settings.account, user_id),
        )
        others = []
        for other in tasks:
            if other.id == task_id:
                continue
            was, now = before.tasks.get(other.id), after.tasks.get(other.id)
            if was and now and (was.state != now.state or abs((was.finish_at or 0) - (now.finish_at or 0)) > 60):
                others.append({"task_id": other.id, "title": other.title, "before": was.to_dict(), "after": now.to_dict()})
        return {
            "before": before.tasks[task_id].to_dict() if task_id in before.tasks else None,
            "after": after.tasks[task_id].to_dict() if task_id in after.tasks else None,
            "others": others,
        }

    def update_task(self, user_id: str, task_id: str, changes: dict) -> Task:
        task = self.task(user_id, task_id)
        if task.finished:
            raise LazyError("This task is finished.")
        status = changes.get("status")
        task = _edited(task, changes, self.clock())
        if status == "paused" and self._is_running(user_id, task_id):
            self._stops[task_id] = "Paused by you"
        self.store.save_task(task)
        self.wake()
        return task

    def cancel(self, user_id: str, task_id: str) -> Task:
        task = self.task(user_id, task_id)
        if task.finished:
            return task
        if self._is_running(user_id, task_id):
            self._stops[task_id] = "Cancelled by you"
        task.status = "cancelled"
        for step in task.steps:
            if step.status == "pending":
                step.status = "skipped"
        task.note("Cancelled", kind="warning")
        task.updated_at = self.clock()
        self.store.save_task(task)
        self.wake()
        return task

    def delete(self, user_id: str, task_id: str) -> None:
        if self._is_running(user_id, task_id):
            raise LazyError("A step of this task is running: cancel it first.")
        if not self.store.delete_task(user_id, task_id):
            raise KeyError(task_id)

    def apply(self, user_id: str, task_id: str, change: dict) -> None:
        """Take one of the plan's ways out: a change to the task, or to the settings."""
        if "settings" in change:
            self.update_settings(user_id, _with_settings(self.store.settings(user_id), change["settings"]).to_dict())
        if "task" in change:
            self.update_task(user_id, task_id, change["task"])

    def update_settings(self, user_id: str, payload: dict) -> Settings:
        current = self.store.settings(user_id)
        merged = {**current.to_dict(), **{key: value for key, value in payload.items() if value is not None}}
        if "allowances" in payload:
            merged["allowances"] = {**current.to_dict()["allowances"], **payload["allowances"]}
        settings = Settings.from_dict(merged)
        if settings.account not in LABELS:
            raise LazyError("Unknown subscription.")
        if settings.account != current.account:
            # A task without its own subscription spent the settings' one: it
            # keeps spending that one, not the one picked now.
            for task in self.store.tasks(user_id, finished=False):
                if task.account is None:
                    task.account = current.account
                elif task.account == settings.account:
                    task.account = None
                else:
                    continue
                self.store.save_task(task)
        if settings.paused and not current.paused:
            for running in self._running.values():
                if self.store.task(user_id, running.task_id) is not None:
                    self._stops[running.task_id] = "Lazy agents were paused"
        self.store.save_settings(user_id, settings)
        self.wake()
        return settings

    def _is_running(self, user_id: str, task_id: str) -> bool:
        return any(owner == user_id and run.task_id == task_id for (owner, _), run in self._running.items())

    # -- running a step -----------------------------------------------------------------
    def _launch(self, user_id: str, task_id: str, step_id: str, settings: Settings) -> None:
        task = self.store.task(user_id, task_id)
        step = task.step(step_id) if task else None
        if task is None or step is None or step.status != "pending":
            return
        now = self.clock()
        step.status = "running"
        step.started_at = now
        step.attempts += 1
        step.reason = ""
        task.retry_at = None
        task.updated_at = now
        task.note(f"{step.title} started" + (" (continuing)" if step.resume else ""), kind="run")
        self.store.save_task(task)
        account = pool_key(settings.account, user_id)
        baseline = self.quota.latest(CLAUDE_CODE) if reports_windows(settings.account) else None
        job = asyncio.create_task(self._run(user_id, task, step, settings), name=f"lazy-{task_id}-{step_id}")
        key = (user_id, settings.account)
        self._running[key] = Running(task_id, step_id, job, now, baseline, account)

        def finished(_job: asyncio.Task) -> None:
            self._running.pop(key, None)
            self._stops.pop(task_id, None)
            self.wake()

        job.add_done_callback(finished)

    async def _run(self, user_id: str, task: Task, step: Step, settings: Settings) -> None:
        try:
            async with self.spaces.use(user_id) as space:
                choice = agent_choice(space, task.system or "", task.agent or "")
            if choice.account != settings.account:
                outcome = StepOutcome(
                    "refused", self.clock(), self.clock(),
                    reason=choice.problem or f"{choice.label} no longer runs on the {LABELS.get(settings.account)}",
                )
            else:
                outcome = await self.runner.run(
                    task, step, settings,
                    should_stop=lambda: self._should_stop(user_id, task.id, settings),
                    on_conversation=lambda context_id: self._remember_conversation(user_id, task.id, context_id),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded on the step, retried with back-off
            logger.exception("Lazy step %s/%s failed", task.id, step.id)
            outcome = StepOutcome("error", self.clock(), self.clock(), reason=str(exc) or type(exc).__name__)
        self.finish(user_id, task.id, step.id, outcome, settings)

    def _remember_conversation(self, user_id: str, task_id: str, context_id: str) -> None:
        task = self.store.task(user_id, task_id)
        if task is not None and task.conversation_id != context_id:
            task.conversation_id = context_id
            self.store.save_task(task)

    def _should_stop(self, user_id: str, task_id: str, settings: Settings) -> Optional[str]:
        if task_id in self._stops:
            return self._stops[task_id]
        if self.store.settings(user_id).paused:
            return "Lazy agents were paused"
        running = self._running.get((user_id, settings.account))
        if running is None or not reports_windows(settings.account):
            return None
        reading = self.quota.latest(CLAUDE_CODE)
        if reading is None or reading.observed_at < running.started_at:
            return None
        if reading.status == "rejected":
            return "The subscription refused: its limit is reached"
        situation = self.situation(user_id, settings)
        # Only a baseline read just before the start tells this run's use from
        # the user's: an old one would book everything since it to the agents
        # (the same rule measure() keeps).
        fresh = running.baseline is not None and running.started_at - running.baseline.observed_at <= FRESH
        for name, spec in WINDOWS.items():
            window = reading.windows.get(name)
            if window is None:
                continue
            allowance = settings.allowance(name)
            before = running.baseline.windows.get(name) if fresh else None
            this_run = window.used - before.used if before is not None and before.resets_at == window.resets_at else 0.0
            if situation.windows[name].agents + max(0.0, this_run) > allowance.share + TOLERANCE:
                return f"Reached the agents' share of the {spec.label.lower()}"
            if window.used >= 100.0 - allowance.safety:
                return f"Reached your reserve in the {spec.label.lower()}"
        return None

    def measure(
        self, settings: Settings, outcome: StepOutcome, baseline: Optional[QuotaReading], user_id: str, started_at: float,
    ) -> Tuple[Dict[str, float], str]:
        """Points the run took per window, and whether they were measured or estimated.

        Measured: the growth between a reading fresh at the start and one taken
        during or after the run, in the same period of every window.
        """
        if not reports_windows(settings.account):
            used = {
                name: outcome.tokens / max(1, settings.capacity_tokens.get(name, 1)) * 100.0 for name in WINDOWS
            }
            return used, "counted"
        estimate = self.calibration(settings.account, user_id).cost(outcome.tokens)
        end = self.quota.latest(CLAUDE_CODE)
        fresh = baseline is not None and started_at - baseline.observed_at <= FRESH
        if not fresh or end is None or end.observed_at < started_at:
            return estimate, "estimated"
        used: Dict[str, float] = {}
        for name in WINDOWS:
            before, after = baseline.windows.get(name), end.windows.get(name)
            if before is None or after is None or before.resets_at != after.resets_at:
                return estimate, "estimated"  # a window reset mid-run: the difference means nothing
            used[name] = max(0.0, after.used - before.used)
        return used, "measured"

    def finish(self, user_id: str, task_id: str, step_id: str, outcome: StepOutcome, settings: Settings) -> Optional[Task]:
        """Record a step's run and move its task on."""
        running = self._running.get((user_id, settings.account))
        mine = running is not None and running.task_id == task_id
        baseline = running.baseline if mine else None
        started_at = running.started_at if mine else outcome.started_at
        task = self.store.task(user_id, task_id)
        if task is None:
            return None
        step = task.step(step_id)
        if step is None:
            return task
        now = self.clock()
        account = pool_key(settings.account, user_id)
        if outcome.tokens or outcome.outcome != "refused":
            used, source = self.measure(settings, outcome, baseline, user_id, started_at)
            self.store.add_run(Run(
                id=new_id("run"), user_id=user_id, account=account, task_id=task_id, step_id=step_id,
                started_at=outcome.started_at, ended_at=outcome.ended_at, tokens=outcome.tokens,
                used=used, source=source, outcome=outcome.outcome,
            ))
            step.tokens_used += outcome.tokens
            step.used = {name: step.used.get(name, 0.0) + points for name, points in used.items()}
            if source == "measured":
                self.store.save_calibration(account, self.store.calibration(account).learn(outcome.tokens, used))
        if outcome.conversation_id:
            task.conversation_id = outcome.conversation_id
        step.finished_at = now
        if task.status == "cancelled":
            step.status = "skipped"
        elif outcome.outcome == "answered":
            self._answered(task, step, outcome.answer)
        elif outcome.outcome in ("interrupted", "stopped"):
            step.status = "pending"
            step.resume = True
            step.reason = outcome.reason or "Stopped at a safe point"
            task.note(f"{step.title} stopped: {step.reason}", kind="warning")
        else:
            step.status = "pending"
            step.resume = outcome.outcome != "refused" and step.resume
            step.reason = outcome.reason or "The step did not finish"
            if step.attempts >= MAX_ATTEMPTS:
                step.status = "failed"
                task.status = "failed"
                task.note(f"{step.title} failed {step.attempts} times; the task stopped: {step.reason}", kind="error")
            else:
                task.retry_at = now + BACKOFF * step.attempts
                task.note(f"{step.title} could not finish: {step.reason}", kind="warning")
        if task.status == "active" and not task.open_steps:
            task.status = "done"
            task.note("Done", kind="done")
            if task.repeat:
                following = self._next_round(task, now)
                self.store.save_task(following)
                task.note(f"The next round is {following.id}", kind="info")
        task.updated_at = now
        self.store.save_task(task)
        return task

    def _next_round(self, task: Task, now: float) -> Task:
        """The round after a repeating *task*: the same work, fresh steps, a new chat."""
        repeat = float(task.repeat or 0)
        following = Task(
            id=new_id("lazy"), user_id=task.user_id, title=task.title, brief=task.brief,
            acceptance=task.acceptance, optional=task.optional,
            deadline=max(now, task.deadline or now) + repeat, priority=task.priority, size=task.size,
            system=task.system, agent=task.agent, account=task.account, cap=task.cap, repeat=task.repeat,
            created_at=now, updated_at=now,
        )
        for index, kind in enumerate(kinds_for(following.optional), start=1):
            following.steps.append(Step.make(kind, following.size, index, optional=kind == "extra"))
        following.note(f"Next round of {task.id}; repeats every {_hours(repeat)}")
        return following

    def _answered(self, task: Task, step: Step, answer: str) -> None:
        step.status = "done"
        step.resume = False
        step.reason = ""
        step.summary = checkpoint_of(answer)
        task.note(f"{step.title} done", kind="done")
        if step.kind == "scout":
            found = size_in(answer)
            if found and found != task.size:
                task.size = found
                for later in task.steps:
                    if later.status == "pending":
                        later.tokens, later.minutes = sized(later.kind, found)
                task.note(f"The scout sized the work as {found}: the plan follows", kind="info")
        if step.kind == "verify" and verdict_in(answer) is False:
            fixes = sum(1 for s in task.steps if s.kind == "fix")
            if fixes >= 2:
                task.status = "failed"
                task.note("Verification failed again after two fixes; the task stopped", kind="error")
                return
            fix = task.add_step("fix", after=step)
            task.add_step("verify", after=fix)
            task.note("Verification found problems: a fix and another check were added", kind="warning")

    # -- learning the user's rates ------------------------------------------------------
    def _on_reading(self, reading: QuotaReading) -> None:
        try:
            self.store.add_reading(reading)
            self._learn(reading)
        except Exception:  # noqa: BLE001 - a reading that cannot be kept is just not kept
            logger.exception("Could not keep a quota reading")
        self.wake()

    def _learn(self, reading: QuotaReading) -> None:
        for user_id in sorted(set(self.store.users()) | set(self.store.settings_users())):
            settings = self.store.settings(user_id)
            if pool_key(settings.account, user_id) != reading.account:
                continue
            if (user_id, settings.account) in self._running:
                continue  # the step's own use would pass for the user's
            rates = self.store.rates(user_id)
            changed = False
            for name, window in reading.windows.items():
                key = (user_id, name)
                last = self._learned.get(key)
                if last is None or last[2] != window.resets_at or window.used < last[1]:
                    self._learned[key] = (reading.observed_at, window.used, window.resets_at)
                    continue
                if reading.observed_at - last[0] < LEARN_SPAN:
                    continue
                agents = sum(
                    run.used.get(name, 0.0)
                    for run in self.store.runs(reading.account, last[0])
                    if run.ended_at <= reading.observed_at
                )
                spent = max(0.0, window.used - last[1] - agents)
                rates = rates.learn(name, settings.routine, last[0], reading.observed_at, spent)
                self._learned[key] = (reading.observed_at, window.used, window.resets_at)
                changed = True
            if changed:
                self.store.save_rates(user_id, rates)


# -- editing ---------------------------------------------------------------------------
EDITABLE = ("title", "brief", "acceptance", "deadline", "priority", "size", "run_now", "cap", "drop_optional", "status")


def _deadline(value: Any) -> Optional[float]:
    if value in (None, "", 0):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise LazyError("The deadline is not a moment in time.") from None


def _repeat(hours: Any) -> Optional[float]:
    """Seconds between rounds from ``repeat_hours``; None (once) for nothing or zero.

    The interval is not a calendar time: the round after this one is due that
    long after this round's deadline (or after it ended, when that is later),
    so a daily task does not keep a fixed hour of the day.
    """
    if hours in (None, "", 0, "0"):
        return None
    if isinstance(hours, bool):
        raise LazyError("The repeat is not a number of hours.")
    try:
        value = float(hours)
    except (TypeError, ValueError):
        raise LazyError("The repeat is not a number of hours.") from None
    if not math.isfinite(value):
        raise LazyError("The repeat is not a number of hours.")
    if value == 0:
        return None
    if value < 1:
        raise LazyError("A repeat is at least one hour.")
    return value * HOUR


def _hours(seconds: float) -> str:
    hours = seconds / HOUR
    return f"{hours / 24:g} day(s)" if hours >= 24 and hours % 24 == 0 else f"{hours:g} h"


def _points(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        raise LazyError("The cap is not a number of points.") from None


def _copy(task: Task) -> Task:
    return Task.from_dict(task.to_dict())


def _edited(task: Task, changes: dict, now: float) -> Task:
    """*task* with *changes* applied (the page's fields, or a way out of the plan)."""
    notes: List[str] = []
    if "title" in changes and str(changes["title"]).strip():
        task.title = str(changes["title"]).strip()[:160]
    for key, limit in (("brief", 8000), ("acceptance", 2000)):
        if key in changes:
            setattr(task, key, str(changes[key] or "")[:limit])
    if "deadline" in changes:
        task.deadline = _deadline(changes["deadline"])
        notes.append("Deadline changed" if task.deadline else "Deadline removed")
    if changes.get("priority") in PRIORITIES and changes["priority"] != task.priority:
        task.priority = changes["priority"]
        notes.append(f"Priority: {task.priority}")
    if changes.get("size") in SIZES and changes["size"] != task.size:
        task.size = changes["size"]
        for step in task.steps:
            if step.status == "pending":
                step.tokens, step.minutes = sized(step.kind, task.size)
        notes.append(f"Size: {task.size}")
    if "run_now" in changes:
        task.run_now = bool(changes["run_now"])
        notes.append("Do it now: first in line, also in your hours" if task.run_now else "Back in line")
    if "cap" in changes:
        task.cap = _points(changes["cap"])
    if changes.get("drop_optional"):
        for step in task.steps:
            if step.optional and step.status == "pending":
                step.status = "skipped"
        notes.append("Optional part dropped")
    if changes.get("status") in ("active", "paused"):
        if changes["status"] != task.status:
            task.status = changes["status"]
            notes.append("Paused" if task.status == "paused" else "Resumed")
            if task.status == "active":
                task.retry_at = None
    for text in notes:
        task.note(text, kind="change", at=now)
    task.updated_at = now
    return task


def _with_settings(settings: Settings, change: dict) -> Settings:
    data = settings.to_dict()
    if "allowances" in change:
        allowances = dict(data["allowances"])
        for name, value in change["allowances"].items():
            allowances[name] = {**allowances.get(name, {}), **value}
        data["allowances"] = allowances
    for key, value in change.items():
        if key != "allowances":
            data[key] = value
    return Settings.from_dict(data)
