"""How much one user may run: turns at once, and turns per day.

Checked at the one place every turn starts (ChatSession._start): a typed
message, a Continue, a message leaving the queue. The operator sets the limits
in ``user_limits`` (the catalog, or the single system's config;
schemas.UserLimitsPolicy); they apply on a server with accounts.

- *Running turns*: the conversations of the user with a turn on, counted in
  the user's space (web_chat.turns). A refused turn is not counted for the day.
- *Turns per day*: counted in the accounts database per UTC day, checked and
  counted in one step, so parallel requests cannot pass the limit together.
- *Tokens per day* and *tokens per turn*: spent model tokens (input plus
  output), counted per model response of the running turn (web_chat.trace);
  a turn past its per-turn budget stops at its next step instead of running on.
- *Dollars per turn, day and month*: what the operator was charged for the
  user's model calls (core.pricing), read from the usage record. Calls on the
  user's own key or subscription cost the operator nothing and are not counted.
  A cost is known only once a call is done, so a turn can pass a budget by one
  call before it is stopped.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from core.pricing import format_usd, to_micro
from schemas.schemas import UserLimitsPolicy


class TurnCounter(Protocol):
    def count_turn(self, user_id: str, limit: Optional[int]) -> bool:
        """Count a turn unless the day's *limit* is reached; whether it was counted."""

    def count_tokens(self, user_id: str, tokens_in: int, tokens_out: int) -> None:
        """Add a finished turn's tokens (input and output) to the user's day."""

    def tokens_today(self, user_id: str) -> int:
        """The tokens the user's turns spent today (input and output)."""


class SpendReader(Protocol):
    def spent_micro(self, user_id: str, since: float) -> int:
        """Micro-dollars the operator was charged for the user's calls since *since*."""


def utc_day_start(now: float) -> float:
    moment = datetime.fromtimestamp(now, timezone.utc)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def utc_month_start(now: float) -> float:
    moment = datetime.fromtimestamp(now, timezone.utc)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


class TurnLimits:
    """Admits or refuses the turns of one user."""

    def __init__(
        self,
        user_id: str,
        policy: Callable[[], UserLimitsPolicy],
        counter: TurnCounter,
        spend: Optional[SpendReader] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """*policy* is read at every turn, so an edited config applies at once."""
        self._user_id = user_id
        self._policy = policy
        self._counter = counter
        self._spend = spend
        self._clock = clock

    def admit(self, running: int) -> Optional[str]:
        """None when a new turn may start - it is then counted - or why not.

        *running* is how many turns the user has on now.
        """
        policy = self._policy()
        if running >= policy.running_turns:
            return (
                f"You have {running} turn(s) running, the most this server allows at once. "
                "Wait for one to finish, or stop it."
            )
        if policy.tokens_per_day is not None and self._counter.tokens_today(self._user_id) >= policy.tokens_per_day:
            return (
                f"You have used today's {policy.tokens_per_day:,} tokens. "
                "The count starts again at 00:00 UTC."
            )
        refusal = self._over_budget(policy)
        if refusal is not None:
            return refusal
        if not self._counter.count_turn(self._user_id, policy.turns_per_day):
            return f"You have used today's {policy.turns_per_day} turns. The count starts again at 00:00 UTC."
        return None

    def _over_budget(self, policy: UserLimitsPolicy) -> Optional[str]:
        if self._spend is None:
            return None
        now = self._clock()
        for limit, since, period, again in (
            (policy.usd_per_day, utc_day_start(now), "today's", "at 00:00 UTC"),
            (policy.usd_per_month, utc_month_start(now), "this month's", "on the 1st at 00:00 UTC"),
        ):
            if limit is None:
                continue
            budget = to_micro(limit) or 0
            spent = self._spend.spent_micro(self._user_id, since)
            if spent >= budget:
                return (
                    f"You have used {period} {format_usd(budget)} budget ({format_usd(spent)} spent). "
                    f"It starts again {again}."
                )
        return None

    def usd_budget(self) -> Optional[int]:
        """The micro-dollars one turn may cost; None when no per-turn budget is set."""
        limit = self._policy().usd_per_turn
        return to_micro(limit) if limit is not None else None

    def token_budget(self) -> Optional[int]:
        """The tokens one turn may spend; None when no per-turn budget is set."""
        return self._policy().max_tokens_per_turn
