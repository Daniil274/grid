"""How much one user may run: turns at once, and turns per day.

Checked at the one place every turn starts (ChatSession._start): a typed
message, a Continue, a message leaving the queue. The operator sets the limits
in ``user_limits`` (the catalog, or the single system's config;
schemas.UserLimitsPolicy); they apply on a server with accounts.

- *Running turns*: the conversations of the user with a turn on, counted in
  the user's space (web_chat.turns). A refused turn is not counted for the day.
- *Turns per day*: counted in the accounts database per UTC day, checked and
  counted in one step, so parallel requests cannot pass the limit together.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from schemas.schemas import UserLimitsPolicy


class TurnCounter(Protocol):
    def count_turn(self, user_id: str, limit: Optional[int]) -> bool:
        """Count a turn unless the day's *limit* is reached; whether it was counted."""


class TurnLimits:
    """Admits or refuses the turns of one user."""

    def __init__(self, user_id: str, policy: Callable[[], UserLimitsPolicy], counter: TurnCounter) -> None:
        """*policy* is read at every turn, so an edited config applies at once."""
        self._user_id = user_id
        self._policy = policy
        self._counter = counter

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
        if not self._counter.count_turn(self._user_id, policy.turns_per_day):
            return f"You have used today's {policy.turns_per_day} turns. The count starts again at 00:00 UTC."
        return None
