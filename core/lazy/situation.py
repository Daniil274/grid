"""Where every window of the pool's subscription stands right now.

The provider's latest reading says how much of each window is used in all;
the ledger of runs says how much of that the lazy agents took. The rest is
the user's - their chats, other apps, anything outside this pool.

A reading grows old between requests. Its age decides the mode:

- ``live``     - read within the last few minutes;
- ``stale``    - older: the user's expected use since then is added, so the
  picture errs on the side of less room;
- ``estimate`` - the subscription reports nothing; use is counted from tokens
  against the capacity the user declared;
- ``unknown``  - nothing read yet: the windows are taken as half used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional

from core.lazy.forecast import UserRates
from core.lazy.model import Run
from core.lazy.routine import Routine
from core.lazy.windows import WINDOWS, WindowSpec
from core.quota_telemetry import QuotaReading

#: A reading younger than this needs no forecast on top.
FRESH = 10 * 60.0
#: With nothing read yet, each window is taken as this used.
ASSUMED_USED = 50.0


@dataclass(frozen=True)
class WindowNow:
    name: str
    #: Points used in the current period, in all.
    used: float
    #: Of which the user's (everything outside the pool).
    user: float
    #: Of which the pool's runs.
    agents: float
    #: Booked by a running step and not measured yet.
    reserved: float
    #: When the current period ends; None for a window not open (it opens with the next use).
    resets_at: Optional[float]

    def to_dict(self) -> dict:
        return {
            "used": round(self.used, 2), "user": round(self.user, 2), "agents": round(self.agents, 2),
            "reserved": round(self.reserved, 2), "resets_at": self.resets_at,
        }


@dataclass(frozen=True)
class Situation:
    now: float
    account: str
    windows: Dict[str, WindowNow]
    mode: str
    observed_at: Optional[float] = None
    #: The provider's last verdict ("allowed", "allowed_warning", "rejected").
    status: str = "allowed"
    overage_allowed: Optional[bool] = None


def _current(spec: WindowSpec, used: float, resets_at: Optional[float], read_at: float, now: float):
    """(used, resets_at, as of) for the period holding *now*, given a window as read."""
    if resets_at is None or resets_at > now:
        return used, resets_at, read_at
    # The read period is over: the window is empty since its reset.
    if spec.anchored:
        return 0.0, None, resets_at
    end = resets_at
    while end <= now:
        end += spec.length
    return 0.0, end, end - spec.length


def from_reading(
    reading: Optional[QuotaReading],
    *,
    account: str,
    runs: Iterable[Run],
    reserved: Mapping[str, float],
    rates: UserRates,
    routine: Routine,
    now: float,
    estimate: bool = False,
) -> Situation:
    """The situation from the latest reading (None: nothing read yet) and the ledger."""
    runs = [run for run in runs if run.account == account]
    windows: Dict[str, WindowNow] = {}
    for name, spec in WINDOWS.items():
        window = reading.windows.get(name) if reading is not None else None
        if window is None:
            used, resets_at, as_of = ASSUMED_USED, (None if spec.anchored else now + spec.length), now
        else:
            used, resets_at, as_of = _current(spec, window.used, window.resets_at, reading.observed_at, now)
        start = resets_at - spec.length if resets_at is not None else now
        in_period = [run for run in runs if run.ended_at >= start]
        agents = sum(run.used.get(name, 0.0) for run in in_period)
        seen = sum(run.used.get(name, 0.0) for run in in_period if run.ended_at <= as_of)
        user = max(0.0, used - seen)
        if not estimate and now - as_of > FRESH:
            # Unseen since the reading: assume the user spent what they usually do.
            user += rates.amount(name, routine, max(as_of, start), now)
        windows[name] = WindowNow(
            name=name,
            used=min(100.0, user + agents),
            user=user,
            agents=agents,
            reserved=float(reserved.get(name, 0.0)),
            resets_at=resets_at,
        )
    if estimate:
        mode = "estimate"
    elif reading is None:
        mode = "unknown"
    else:
        mode = "live" if now - reading.observed_at <= FRESH else "stale"
    return Situation(
        now=now,
        account=account,
        windows=windows,
        mode=mode,
        observed_at=reading.observed_at if reading is not None else None,
        status=reading.status if reading is not None else "allowed",
        overage_allowed=reading.overage_allowed if reading is not None else None,
    )
