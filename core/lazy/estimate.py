"""What a step costs before it runs, and how runs correct the guess.

A task is cut into steps by its size. Each step has a token estimate and a
duration; its cost in a window is tokens times that window's price in points
per million tokens. The prices start as guesses and are calibrated by every
run the provider measured: the observed points per million tokens move the
price, bounded so that one odd run cannot swing it.

Steps:

- ``scout`` - look just enough to plan and size the work (a bounded budget
  before any promise about a large task);
- ``work``  - do it;
- ``verify`` - check the result against the acceptance criteria, reserved up
  front so the budget is never spent on work no one checked;
- ``extra`` - the optional part, the first thing to drop when time or budget
  run short;
- ``fix``   - repair what verification found, then verify again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

#: (tokens, minutes) per step kind and task size.
SIZES: Dict[str, Dict[str, tuple]] = {
    "S": {"scout": (120_000, 6), "work": (350_000, 12), "verify": (120_000, 6), "extra": (150_000, 8), "fix": (150_000, 8)},
    "M": {"scout": (250_000, 10), "work": (1_000_000, 30), "verify": (300_000, 10), "extra": (400_000, 15), "fix": (350_000, 12)},
    "L": {"scout": (500_000, 15), "work": (2_500_000, 60), "verify": (600_000, 15), "extra": (900_000, 30), "fix": (700_000, 20)},
}
SIZE_LABELS = {"S": "Small", "M": "Medium", "L": "Large"}

KINDS: Dict[str, str] = {
    "scout": "Scout",
    "work": "Work",
    "verify": "Verify",
    "extra": "Extras",
    "fix": "Fix",
}

#: Points per million tokens, before any run calibrated them.
DEFAULT_PRICE = {"five_hour": 3.0, "seven_day": 0.45}
#: How far one run may move a price (as a factor), and how much it counts.
MAX_STEP = 2.0
LEARNING = 0.35


@dataclass(frozen=True)
class Calibration:
    price: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PRICE))
    samples: int = 0

    def cost(self, tokens: int) -> Dict[str, float]:
        return {name: tokens / 1_000_000 * price for name, price in self.price.items()}

    def learn(self, tokens: int, used: Mapping[str, float]) -> "Calibration":
        """This calibration after a run of *tokens* measured *used* points."""
        if tokens < 20_000:
            return self  # too small to say anything about a price
        price = dict(self.price)
        for name, points in used.items():
            if name not in price or points < 0:
                continue
            seen = points / (tokens / 1_000_000)
            old = price[name]
            seen = max(old / MAX_STEP, min(old * MAX_STEP, seen)) if old > 0 else seen
            price[name] = (1 - LEARNING) * old + LEARNING * seen
        return Calibration(price=price, samples=self.samples + 1)

    def to_dict(self) -> dict:
        return {"price": dict(self.price), "samples": self.samples}

    @classmethod
    def from_dict(cls, data: Mapping) -> "Calibration":
        return cls(
            price={**DEFAULT_PRICE, **{k: float(v) for k, v in (data.get("price") or {}).items()}},
            samples=int(data.get("samples") or 0),
        )


def sized(kind: str, size: str) -> tuple:
    return SIZES.get(size, SIZES["M"]).get(kind, SIZES["M"]["work"])


SIZE_LINE = re.compile(r"size(?:\s+estimate)?\s*[:\-–]\s*\**\s*(S|M|L|small|medium|large)\b", re.IGNORECASE)
VERDICT_LINE = re.compile(r"verdict\s*[:\-–]\s*\**\s*(pass(?:ed)?|fail(?:ed)?)\b", re.IGNORECASE)


def size_in(answer: str) -> Optional[str]:
    """The size a scout's answer settled on ("Size: M"), if it names one."""
    match = SIZE_LINE.search(answer or "")
    if not match:
        return None
    word = match.group(1).upper()
    return word[0] if word[0] in SIZES else None


def verdict_in(answer: str) -> Optional[bool]:
    """A verifier's verdict ("Verdict: pass"); None when it gives none."""
    match = VERDICT_LINE.search(answer or "")
    if not match:
        return None
    return match.group(1).lower().startswith("pass")


def checkpoint_of(answer: str, limit: int = 1200) -> str:
    """The answer's closing "Checkpoint" section, else its end: what the next step starts from."""
    text = (answer or "").strip()
    marker = re.search(r"^#*\s*\**checkpoint\**\s*:?\s*$", text, re.IGNORECASE | re.MULTILINE)
    if marker:
        text = text[marker.end():].strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def kinds_for(optional: str) -> List[str]:
    """The steps of a new task: scout, work, the optional part when there is one, verify."""
    return ["scout", "work", *(["extra"] if optional.strip() else []), "verify"]
