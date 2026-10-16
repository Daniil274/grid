"""How much of each window the user is expected to spend, and when.

The user spends at one rate during their own hours (core.lazy.routine) and at
another outside them; both are points per hour, per window. They start from
conservative guesses and learn from the provider's readings: between two
readings of one period, growth the agents' runs do not explain is the user's.
An interval that lies wholly inside or wholly outside the user's hours updates
that rate; a mixed one teaches nothing exact and is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping

from core.lazy.routine import Routine
from core.lazy.windows import HOUR

#: Points per hour while the user works with the subscription, and otherwise.
DEFAULT_BUSY = {"five_hour": 12.0, "seven_day": 1.2}
DEFAULT_IDLE = {"five_hour": 0.5, "seven_day": 0.05}
#: How fast a new observation moves a learned rate.
LEARNING = 0.3
#: An interval shorter than this says too little about a rate.
MIN_INTERVAL = 20 * 60.0


@dataclass(frozen=True)
class UserRates:
    busy: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BUSY))
    idle: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_IDLE))
    samples: int = 0

    def to_dict(self) -> dict:
        return {"busy": dict(self.busy), "idle": dict(self.idle), "samples": self.samples}

    @classmethod
    def from_dict(cls, data: Mapping) -> "UserRates":
        return cls(
            busy={**DEFAULT_BUSY, **{k: float(v) for k, v in (data.get("busy") or {}).items()}},
            idle={**DEFAULT_IDLE, **{k: float(v) for k, v in (data.get("idle") or {}).items()}},
            samples=int(data.get("samples") or 0),
        )

    def amount(self, window: str, routine: Routine, start: float, end: float) -> float:
        """Points the user is expected to spend in [start, end)."""
        if end <= start:
            return 0.0
        busy = routine.busy_seconds(start, end)
        idle = (end - start) - busy
        return (busy * self.busy.get(window, 0.0) + idle * self.idle.get(window, 0.0)) / HOUR

    def learn(self, window: str, routine: Routine, start: float, end: float, spent: float) -> "UserRates":
        """These rates after the user was seen spending *spent* points in [start, end)."""
        length = end - start
        if length < MIN_INTERVAL or spent < 0:
            return self
        busy = routine.busy_seconds(start, end)
        rate = spent / (length / HOUR)
        if busy >= length - 1:
            key = "busy"
        elif busy <= 1:
            key = "idle"
        else:
            return self
        rates = dict(getattr(self, key))
        rates[window] = (1 - LEARNING) * rates.get(window, rate) + LEARNING * rate
        changed = {"busy": self.busy, "idle": self.idle, key: rates}
        return UserRates(busy=changed["busy"], idle=changed["idle"], samples=self.samples + 1)
