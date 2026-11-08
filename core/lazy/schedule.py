"""When a repeating task's next round may be released.

A series is not "the last round plus a delay": the release of the next round is
its own moment, kept apart from the deadline and from the forecast.

- ``interval`` - every *seconds*, on a fixed grid anchored at the series' first
  release. A round that ran long does not drag the grid: the next release is the
  first grid slot strictly after the round ended, so late work skips the slots
  it missed instead of firing a queue of rounds back to back.
- ``daily`` / ``weekly`` - a wall-clock time (``HH:MM``) on a local day, in the
  user's own zone. The zone is named explicitly: an hour means that hour where
  the user lives, not on the server.

Daylight saving is decided, not guessed, and the same on every run:

- a time that does not exist (the spring gap) moves forward by the gap - the
  first instant at or after the requested wall time;
- a time that happens twice (the autumn fold) takes the first occurrence
  (``fold=0``).

A missed slot is skipped once, never replayed: :meth:`Schedule.next_release`
answers with the first release strictly after the moment it is asked about, so a
server that was down for a week makes one round, not seven.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional

from core.lazy.routine import zone

KINDS = ("interval", "daily", "weekly")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
#: Missed slots are skipped - the only policy today, named so it is explicit.
MISSED = ("skip",)


def _parse_time(text: str) -> tuple:
    clean = str(text or "").strip()
    if ":" not in clean:
        raise ValueError("A time is HH:MM, e.g. 04:00.")
    hours, minutes = clean.split(":", 1)
    hour, minute = int(hours), int(minutes)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("A time is HH:MM between 00:00 and 23:59.")
    return hour, minute


def _weekday(value: Any) -> int:
    """0 for Monday ... 6 for Sunday, from a name or a number."""
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 6:
            return value
        raise ValueError("A weekday is 0-6 (Monday-Sunday) or a name like mon.")
    text = str(value or "").strip().lower()[:3]
    if text in WEEKDAYS:
        return WEEKDAYS.index(text)
    raise ValueError("A weekday is 0-6 (Monday-Sunday) or a name like mon.")


def _instant(day: date, hour: int, minute: int, tz) -> float:
    """The Unix second of a local wall time, DST decided as the module describes.

    ``fold=0`` is the first occurrence of an ambiguous time; in a gap Python
    uses the offset from before the transition, which lands the instant after
    the gap - the wall time moved forward, never backwards and never dropped.
    """
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz, fold=0)
    return local.timestamp()


@dataclass(frozen=True)
class Schedule:
    """How often, and from when, a series releases its rounds."""

    kind: str = "interval"
    #: For ``interval``: seconds between releases.
    seconds: float = 0.0
    #: For ``daily``/``weekly``: the local wall time.
    hour: int = 0
    minute: int = 0
    #: For ``weekly``: 0 = Monday ... 6 = Sunday, in the schedule's zone.
    weekday: int = 0
    #: The IANA zone the wall time is read in; the user's own unless named.
    timezone: str = "UTC"
    #: What to do with slots missed while nothing could run: "skip" (no queue).
    missed: str = "skip"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"Unknown repeat kind: {self.kind}.")
        if self.kind == "interval" and self.seconds <= 0:
            raise ValueError("An interval repeat needs a positive period.")
        if self.kind != "interval" and not (0 <= self.hour <= 23 and 0 <= self.minute <= 59):
            raise ValueError("A calendar repeat needs a wall time between 00:00 and 23:59.")
        if self.kind == "weekly" and not (0 <= int(self.weekday) <= 6):
            raise ValueError("A weekday is 0-6 (Monday-Sunday).")
        if self.missed not in MISSED:
            raise ValueError(f"Unknown missed policy: {self.missed}.")

    # -- (de)serialization -------------------------------------------------------------
    def to_dict(self) -> dict:
        data = {"kind": self.kind, "timezone": self.timezone, "missed": self.missed}
        if self.kind == "interval":
            data["seconds"] = self.seconds
            data["hours"] = self.seconds / 3600.0
        else:
            data["time"] = f"{self.hour:02d}:{self.minute:02d}"
            if self.kind == "weekly":
                data["weekday"] = WEEKDAYS[self.weekday]
                data["weekday_index"] = self.weekday
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> "Schedule":
        kind = str(data.get("kind") or "interval").strip().lower()
        if kind == "interval":
            seconds = data.get("seconds")
            if seconds in (None, ""):
                hours = data.get("hours")
                seconds = float(hours) * 3600.0 if hours not in (None, "") else 0.0
            return cls(kind="interval", seconds=float(seconds), timezone=str(data.get("timezone") or "UTC"),
                       missed=str(data.get("missed") or "skip"))
        hour, minute = _parse_time(data.get("time") or "")
        weekday = _weekday(data["weekday"]) if kind == "weekly" and data.get("weekday") not in (None, "") else 0
        return cls(kind=kind, hour=hour, minute=minute, weekday=weekday,
                   timezone=str(data.get("timezone") or "UTC"), missed=str(data.get("missed") or "skip"))

    @classmethod
    def interval(cls, seconds: float, *, timezone: str = "UTC") -> "Schedule":
        return cls(kind="interval", seconds=float(seconds), timezone=timezone)

    def with_timezone(self, timezone: str) -> "Schedule":
        return Schedule(kind=self.kind, seconds=self.seconds, hour=self.hour, minute=self.minute,
                        weekday=self.weekday, timezone=timezone or self.timezone, missed=self.missed)

    # -- the answer ---------------------------------------------------------------------
    def first_release(self, now: float) -> float:
        """The release of a series' *first* round, opening the series at *now*.

        An interval has no wall time to wait for: its first round is due at the
        moment the series opens and only the plan may hold it. A calendar rule
        does name a wall time, so its first round waits for the next slot - a
        series "daily at 04:00" made at 11:00 does not run at 11:00 and then
        never again on its own grid.
        """
        if self.kind == "interval":
            return float(now)
        return self.next_release(now)

    def next_release(self, after: float, *, anchor: Optional[float] = None) -> float:
        """The first release strictly after *after* (the round's end).

        *anchor* is the series' first release, which holds an interval on its
        grid; a calendar release is a wall time and needs none.
        """
        if self.kind == "interval":
            base = float(anchor if anchor is not None else after)
            if after < base:
                # The round ended before its own release (clock skew): the first
                # grid slot, base itself, is still ahead of it.
                return base
            steps = int(math.floor((after - base) / self.seconds + 1e-9)) + 1
            return base + steps * self.seconds
        tz = zone(self.timezone)
        day = datetime.fromtimestamp(after, tz).date()
        if self.kind == "daily":
            for ahead in range(0, 4):
                moment = _instant(day + timedelta(days=ahead), self.hour, self.minute, tz)
                if moment > after:
                    return moment
            return _instant(day + timedelta(days=4), self.hour, self.minute, tz)
        # weekly: the next matching weekday, this week or next.
        ahead = (self.weekday - day.weekday()) % 7
        for shift in (ahead, ahead + 7):
            moment = _instant(day + timedelta(days=shift), self.hour, self.minute, tz)
            if moment > after:
                return moment
        return _instant(day + timedelta(days=ahead + 14), self.hour, self.minute, tz)

    def describe(self) -> str:
        """A short phrase for the user's history and the DTOs."""
        if self.kind == "interval":
            hours = self.seconds / 3600.0
            if hours >= 24 and hours % 24 == 0:
                return f"every {hours / 24:g} day(s)"
            return f"every {hours:g} h"
        clock = f"{self.hour:02d}:{self.minute:02d}"
        if self.kind == "daily":
            return f"daily at {clock} ({self.timezone})"
        return f"weekly on {WEEKDAYS[self.weekday]} at {clock} ({self.timezone})"
