"""Messages the user sends to a turn that is running, delivered at the next step.

A user may add to a task while the agent works on it ("also check the tests",
"the file is in docs/"). Such a message does not stop the run: before the
agent's next model call it is

1. stored in the agent's SDK session - the SDK saves each finished step before
   the next call, so the message lands right after the step it followed, where
   the next turn will read it;
2. inserted into this run's model input at that same position, on this call
   and on every later call of the run (the SDK builds each call's input from
   the run's own items, not from the session);
3. reported through ``on_delivered`` so the chat shows it as a user message.

A message that finds no next call - the run answered first - is handed back by
:meth:`Steering.undelivered` for the caller to queue as the next turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

#: Opens a delivered message, so the model reads it as added mid-task.
STEER_PREFIX = "[The user added this while you were working on the task - take it into account]\n"


@dataclass
class SteerMessage:
    message_id: str
    text: str
    images: List[str] = field(default_factory=list)
    #: Called once the message reached the agent; errors are logged.
    on_delivered: Optional[Callable[["SteerMessage"], Any]] = None
    #: Called when the turn ended before any model call read the message.
    on_undelivered: Optional[Callable[["SteerMessage"], Any]] = None

    def as_item(self) -> dict:
        """The user message the model reads."""
        content: List[dict] = [{"type": "input_text", "text": STEER_PREFIX + self.text}]
        content.extend({"type": "input_image", "image_url": url, "detail": "auto"} for url in self.images)
        return {"role": "user", "content": content}


class Steering:
    """Pending and delivered mid-turn messages of one top-level turn."""

    def __init__(self) -> None:
        self._pending: List[SteerMessage] = []
        # (position in the run's input, item), in delivery order.
        self._delivered: List[tuple[int, dict]] = []

    def add(self, message: SteerMessage) -> None:
        self._pending.append(message)

    def undelivered(self) -> List[SteerMessage]:
        """Take the messages no model call has read; they are no longer pending."""
        pending, self._pending = self._pending, []
        return pending

    def withdraw(self, message_id: str) -> bool:
        """Take back a message no model call has read yet - the user cancelled it.

        False when it is no longer pending: already read, or never added.
        """
        kept = [message for message in self._pending if message.message_id != message_id]
        found = len(kept) != len(self._pending)
        self._pending = kept
        return found

    def hand_back(self) -> None:
        """The turn is over: give every message still pending to its sender."""
        for message in self.undelivered():
            if message.on_undelivered is None:
                logger.warning("A mid-turn message was never delivered and has no one to take it back")
                continue
            try:
                message.on_undelivered(message)
            except Exception:
                logger.exception("Handing back an undelivered mid-turn message failed")

    def new_run(self) -> None:
        """A rerun reads delivered messages from its session: stop inserting them."""
        self._delivered.clear()

    async def apply(self, items: List[Any], session: Any) -> List[Any]:
        """*items* of a model call with every mid-turn message in its place."""
        pending, self._pending = self._pending, []
        for message in pending:
            item = message.as_item()
            if session is not None:
                await session.add_items([item])
            self._delivered.append((len(items), item))
            if message.on_delivered is not None:
                try:
                    message.on_delivered(message)
                except Exception:
                    logger.exception("Reporting a delivered mid-turn message failed")
        if not self._delivered:
            return items
        result = list(items)
        # Ascending positions: each insert shifts the later ones by one.
        for shift, (position, item) in enumerate(self._delivered):
            result.insert(min(position + shift, len(result)), item)
        return result
