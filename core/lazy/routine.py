"""When the subscription is the user's own: the hours agents keep off it.

A routine is a set of hours per weekday in the user's own time zone, kept as
minute ranges (``[600, 1140]`` is 10:00-19:00). Agents start no step inside it
and plan each step to end before it begins; outside it they may work. The
same hours tell the forecast when the user is likely to spend the quota.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterator, List, Mapping, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MINUTES_PER_DAY = 24 * 60
#: Mon-Fri 10:00-19:00: a working day, until the user says otherwise.
DEFAULT_HOURS: Dict[int, List[Tuple[int, int]]] = {day: [(600, 1140)] for day in range(5)}


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _normalized(hours: Mapping[int, List[Tuple[int, int]]]) -> Dict[int, List[Tuple[int, int]]]:
    """Ranges clamped to the day, sorted and merged where they touch."""
    result: Dict[int, List[Tuple[int, int]]] = {}
    for day, ranges in hours.items():
        clean = sorted(
            (max(0, min(MINUTES_PER_DAY, int(a))), max(0, min(MINUTES_PER_DAY, int(b))))
            for a, b in ranges
        )
        merged: List[Tuple[int, int]] = []
        for a, b in clean:
            if b <= a:
                continue
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        if merged:
            result[int(day) % 7] = merged
    return result


@dataclass(frozen=True)
class Routine:
    hours: Dict[int, List[Tuple[int, int]]] = field(default_factory=lambda: dict(DEFAULT_HOURS))
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        object.__setattr__(self, "hours", _normalized(self.hours))

    # -- (de)serialization ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {"timezone": self.timezone, "hours": {str(day): [list(r) for r in ranges] for day, ranges in self.hours.items()}}

    @classmethod
    def from_dict(cls, data: Mapping) -> "Routine":
        hours = {int(day): [tuple(r) for r in ranges] for day, ranges in (data.get("hours") or {}).items()}
        return cls(hours=hours, timezone=str(data.get("timezone") or "UTC"))

    # -- queries -------------------------------------------------------------------
    def intervals(self, start: float, end: float) -> Iterator[Tuple[float, float]]:
        """The user's hours within [start, end), as Unix-second intervals."""
        if end <= start or not self.hours:
            return
        tz = zone(self.timezone)
        date = datetime.fromtimestamp(start, tz).date()
        last = datetime.fromtimestamp(end, tz).date()
        while date <= last:
            # Minutes past local midnight in wall-clock time: a DST day has 23 or 25 hours.
            midnight = datetime(date.year, date.month, date.day, tzinfo=tz)
            for a, b in self.hours.get(date.weekday(), ()):
                lo = (midnight + timedelta(minutes=a)).timestamp()
                hi = (midnight + timedelta(minutes=b)).timestamp()
                if hi > start and lo < end:
                    yield max(lo, start), min(hi, end)
            date += timedelta(days=1)

    def busy_at(self, t: float) -> bool:
        return any(True for _ in self.intervals(t, t + 1))

    def busy_until(self, t: float) -> float:
        """The end of the user's hours that hold *t* (ranges that touch run on); *t* outside them."""
        end = t
        for _ in range(9):
            covering = [hi for lo, hi in self.intervals(end, end + 2 * MINUTES_PER_DAY * 60) if lo <= end < hi]
            if not covering:
                break
            end = max(covering)
        return end

    def next_busy(self, t: float, limit: float) -> float:
        """When the user's hours next begin after *t*; *limit* when not before it."""
        for lo, _hi in self.intervals(t, limit):
            return max(lo, t)
        return limit

    def busy_seconds(self, start: float, end: float) -> float:
        return sum(hi - lo for lo, hi in self.intervals(start, end))
