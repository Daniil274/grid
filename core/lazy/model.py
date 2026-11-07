"""Lazy tasks, their steps, the user's settings and the ledger of runs.

Plain data with explicit (de)serialization: the store keeps each as JSON, and
the planner reads them without touching storage.

A task's ``status`` is what the user and the runner decided - ``active``,
``paused``, ``done``, ``failed``, ``cancelled``; whether an active task is on
track is the planner's answer, recomputed every time, never stored.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional

from core.lazy.budget import Allowance
from core.lazy.estimate import KINDS, sized
from core.lazy.routine import Routine

PRIORITIES = ("low", "normal", "high")
TASK_STATES = ("active", "paused", "done", "failed", "cancelled")
STEP_STATES = ("pending", "running", "done", "failed", "skipped")
#: The task's own history: the latest events only.
HISTORY_LIMIT = 40


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class Step:
    id: str
    kind: str
    tokens: int
    minutes: int
    optional: bool = False
    status: str = "pending"
    #: Continue the interrupted turn rather than start the step anew.
    resume: bool = False
    attempts: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    tokens_used: int = 0
    #: Points the step used per window, as measured (or estimated) after it ran.
    used: Dict[str, float] = field(default_factory=dict)
    #: Where the step left the work: the checkpoint its answer ended with.
    summary: str = ""
    #: Why it last stopped short, in words for the user.
    reason: str = ""

    @property
    def title(self) -> str:
        return KINDS.get(self.kind, self.kind.title())

    @property
    def open(self) -> bool:
        return self.status in ("pending", "running")

    @classmethod
    def make(cls, kind: str, size: str, index: int, *, optional: bool = False) -> "Step":
        tokens, minutes = sized(kind, size)
        return cls(id=f"s{index}", kind=kind, tokens=tokens, minutes=minutes, optional=optional)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping) -> "Step":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class Task:
    id: str
    user_id: str
    title: str
    brief: str
    acceptance: str = ""
    #: The part that may be dropped when time or budget run short.
    optional: str = ""
    deadline: Optional[float] = None
    priority: str = "normal"
    size: str = "M"
    system: Optional[str] = None
    agent: Optional[str] = None
    #: The subscription the task spends - its agent's - when it is not the
    #: settings' own; None spends the settings' one (web_chat.lazy.service).
    account: Optional[str] = None
    status: str = "active"
    #: "Do it now": the task goes first and may run in the user's hours - never over budget.
    run_now: bool = False
    #: Weekly points this task may use in all, beyond the pool's share; None for no cap.
    cap: Optional[float] = None
    #: Seconds between rounds: a task that is done sets up its next round with
    #: fresh steps, due this long after; None runs once.
    repeat: Optional[float] = None
    #: The series of rounds a repeating task belongs to: the id of its first
    #: round, kept by every later round, so the series can be paused, edited or
    #: cancelled as one thing. None for a task that runs once.
    series_id: Optional[str] = None
    #: The user's project this task works in; None for no project.
    project_id: Optional[str] = None
    #: The caller's key for one creation: a repeated request returns this task
    #: instead of making another (web_chat.lazy.service.LazyAgents.create_or_get).
    request_id: Optional[str] = None
    #: Bumped on every save. A caller may name the version it read, and its
    #: change is refused when the task moved since (a stale edit).
    version: int = 0
    conversation_id: Optional[str] = None
    #: No step starts before this: a refused or failed step backs off.
    retry_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    steps: List[Step] = field(default_factory=list)
    history: List[dict] = field(default_factory=list)

    @property
    def open_steps(self) -> List[Step]:
        return [step for step in self.steps if step.open]

    @property
    def running_step(self) -> Optional[Step]:
        return next((step for step in self.steps if step.status == "running"), None)

    @property
    def next_step(self) -> Optional[Step]:
        return next((step for step in self.steps if step.status == "pending"), None)

    @property
    def finished(self) -> bool:
        return self.status in ("done", "failed", "cancelled")

    def step(self, step_id: str) -> Optional[Step]:
        return next((step for step in self.steps if step.id == step_id), None)

    def add_step(self, kind: str, *, after: Optional[Step] = None, optional: bool = False) -> Step:
        index = 1 + max((int(step.id[1:]) for step in self.steps if step.id[1:].isdigit()), default=0)
        new = Step.make(kind, self.size, index, optional=optional)
        position = self.steps.index(after) + 1 if after is not None and after in self.steps else len(self.steps)
        self.steps.insert(position, new)
        return new

    def note(self, text: str, *, kind: str = "info", at: Optional[float] = None) -> None:
        self.history.append({"at": time.time() if at is None else at, "kind": kind, "text": text})
        del self.history[:-HISTORY_LIMIT]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> "Task":
        known = {name for name in cls.__dataclass_fields__}
        values: Dict[str, Any] = {key: value for key, value in data.items() if key in known}
        values["steps"] = [Step.from_dict(step) for step in data.get("steps") or []]
        values["history"] = list(data.get("history") or [])
        return cls(**values)


DEFAULT_ALLOWANCES = {"five_hour": Allowance(share=10.0, safety=10.0), "seven_day": Allowance(share=10.0, safety=5.0)}


@dataclass
class Settings:
    """One user's agreement with the lazy agents."""

    #: Which subscription the pool spends: "claude-code" or "chatgpt".
    account: str = "claude-code"
    allowances: Dict[str, Allowance] = field(default_factory=lambda: dict(DEFAULT_ALLOWANCES))
    routine: Routine = field(default_factory=Routine)
    #: Stop a running step when the user starts a turn of their own.
    yield_to_user: bool = True
    #: Nothing new starts while the whole pool is paused.
    paused: bool = False
    #: For a subscription that reports nothing: its windows' capacity in tokens.
    capacity_tokens: Dict[str, int] = field(default_factory=lambda: {"five_hour": 6_000_000, "seven_day": 60_000_000})

    def allowance(self, window: str) -> Allowance:
        return self.allowances.get(window) or DEFAULT_ALLOWANCES.get(window) or Allowance()

    def to_dict(self) -> dict:
        return {
            "account": self.account,
            "allowances": {name: allowance.to_dict() for name, allowance in self.allowances.items()},
            "routine": self.routine.to_dict(),
            "yield_to_user": self.yield_to_user,
            "paused": self.paused,
            "capacity_tokens": dict(self.capacity_tokens),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "Settings":
        defaults = cls()
        allowances = dict(defaults.allowances)
        for name, value in (data.get("allowances") or {}).items():
            allowances[name] = Allowance.from_dict(value)
        return cls(
            account=str(data.get("account") or defaults.account),
            allowances=allowances,
            routine=Routine.from_dict(data["routine"]) if data.get("routine") else defaults.routine,
            yield_to_user=bool(data.get("yield_to_user", True)),
            paused=bool(data.get("paused", False)),
            capacity_tokens={**defaults.capacity_tokens, **{k: int(v) for k, v in (data.get("capacity_tokens") or {}).items()}},
        )

    def with_allowance(self, window: str, share: Optional[float] = None, safety: Optional[float] = None) -> "Settings":
        current = self.allowance(window)
        changed = Allowance(
            share=current.share if share is None else share,
            safety=current.safety if safety is None else safety,
        )
        return replace(self, allowances={**self.allowances, window: changed})


@dataclass
class Run:
    """One step's run, as the ledger keeps it: what it took from each window."""

    id: str
    user_id: str
    account: str
    task_id: str
    step_id: str
    started_at: float
    ended_at: float
    tokens: int = 0
    used: Dict[str, float] = field(default_factory=dict)
    #: "measured" from the provider's readings, else "estimated" from tokens.
    source: str = "estimated"
    outcome: str = "answered"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping) -> "Run":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})
