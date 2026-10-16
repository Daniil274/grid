"""The quota windows of a subscription and their periods.

Claude reports two windows: a five-hour one that starts with the first use
after the last one ended, and a seven-day one that resets at a known moment.
Each is a capacity of 100 points; what one window allows says nothing about
another, so their points are never added together.

For planning, periods follow one another from the current reset: the user's
own use - even a little - keeps the five-hour window open, so the next period
starts when this one ends. Telemetry corrects that guess as soon as the
provider answers again.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

HOUR = 3600.0
DAY = 24 * HOUR


@dataclass(frozen=True)
class WindowSpec:
    name: str
    label: str
    length: float
    #: Opens with the first use after the last period ended (else resets on a schedule).
    anchored: bool


FIVE_HOUR = WindowSpec("five_hour", "5-hour window", 5 * HOUR, anchored=True)
SEVEN_DAY = WindowSpec("seven_day", "Weekly limit", 7 * DAY, anchored=False)
WINDOWS: Dict[str, WindowSpec] = {spec.name: spec for spec in (FIVE_HOUR, SEVEN_DAY)}


@dataclass(frozen=True)
class Periods:
    """The periods of one window from now on: the current one ends at ``first_end``."""

    spec: WindowSpec
    first_end: float

    @classmethod
    def from_reset(cls, spec: WindowSpec, now: float, resets_at: Optional[float]) -> "Periods":
        """Periods given the reported reset; a window with none open starts now."""
        if resets_at is None:
            return cls(spec, now + spec.length)
        end = resets_at
        if end <= now:  # the reported period is over: later ones follow on
            end += math.ceil((now - end) / spec.length + 1e-9) * spec.length
        return cls(spec, end)

    def index(self, t: float) -> int:
        """0 for the current period, 1 for the next one, ..."""
        if t < self.first_end:
            return 0
        return 1 + int((t - self.first_end) // self.spec.length)

    def bounds(self, index: int) -> Tuple[float, float]:
        """(start, end) of period *index*; the current one starts a full length before its end."""
        end = self.first_end + index * self.spec.length
        return end - self.spec.length, end
