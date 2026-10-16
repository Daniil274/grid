"""How much the agents may still spend in one window.

For a window *j* with capacity ``C`` (100 points):

- ``share``  - the points the agents may use per period, ``alpha_j * C``;
- ``agents`` - what they already used this period (``A_j``);
- ``reserved`` - what a running step booked and has not yet been measured (``R_j``);
- ``user`` - the user's use this period: known plus forecast to its end (``U_j+``);
- ``safety`` - points kept free on top of that forecast (``M_j``).

    H_j = max(0, min(share - agents - reserved,
                     C - agents - reserved - user - safety))

A step whose cost is ``q_j`` may start only when ``q_j <= H_j`` in every
window at once: room in the five-hour window says nothing about the week.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

CAPACITY = 100.0


@dataclass(frozen=True)
class Allowance:
    """What the user agreed the agents may take from one window."""

    share: float = 10.0
    safety: float = 5.0

    def to_dict(self) -> dict:
        return {"share": self.share, "safety": self.safety}

    @classmethod
    def from_dict(cls, data: Mapping) -> "Allowance":
        return cls(
            share=max(0.0, min(CAPACITY, float(data.get("share", 10.0)))),
            safety=max(0.0, min(CAPACITY, float(data.get("safety", 5.0)))),
        )


def headroom(
    allowance: Allowance, *, agents: float, reserved: float, user: float, capacity: float = CAPACITY
) -> float:
    """``H_j``: the points a new step may use in this window now."""
    own = allowance.share - agents - reserved
    common = capacity - agents - reserved - user - allowance.safety
    return max(0.0, min(own, common))


def binding(allowance: Allowance, *, agents: float, reserved: float, user: float, capacity: float = CAPACITY) -> str:
    """Which limit makes ``H_j``: "share" (the agents' own) or "common" (the user's room)."""
    own = allowance.share - agents - reserved
    common = capacity - agents - reserved - user - allowance.safety
    return "share" if own <= common else "common"


def fits(cost: Mapping[str, float], room: Mapping[str, float]) -> Tuple[bool, Optional[str]]:
    """Whether *cost* fits *room* in every window; else the first window it does not."""
    for name, points in cost.items():
        if points > room.get(name, 0.0) + 1e-9:
            return False, name
    return True, None
