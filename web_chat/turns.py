"""The turns running in one space, and the background work beside them.

A turn belongs to its space, not to the socket that started it (see
web_chat.session), so the space keeps the record of what runs where:

- a conversation is *claimed* from the moment a turn is accepted until the
  turn has finished - one turn per conversation at a time;
- the turn and its task are *registered* while the task lives, so a socket
  that opens later can attach to it;
- work started beside the command loop (a delivery decision, moving the queue
  on) is *owned* here until it finishes, and a failure is logged, not lost.

A space with nothing claimed, registered or owned is idle and can be unloaded
(web_chat.spaces).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Coroutine, Optional

logger = logging.getLogger("grid.web_chat.turns")


class TurnBoard:
    """Claims, running turns and background work of one space."""

    def __init__(self) -> None:
        self._claimed: set[str] = set()
        self._turns: dict[str, tuple[Any, asyncio.Task]] = {}
        self._background: set[asyncio.Task] = set()

    # -- claims ----------------------------------------------------------------
    def claim(self, context_id: str) -> bool:
        """Reserve the conversation for a new turn; False when one is on already."""
        if context_id in self._claimed:
            return False
        self._claimed.add(context_id)
        return True

    def release(self, context_id: str) -> None:
        self._claimed.discard(context_id)

    def is_claimed(self, context_id: str) -> bool:
        return context_id in self._claimed

    @property
    def claimed_count(self) -> int:
        """Conversations with a turn on: the user's turns running now."""
        return len(self._claimed)

    # -- running turns ---------------------------------------------------------
    def register(self, context_id: str, turn: Any, task: asyncio.Task) -> None:
        self._turns[context_id] = (turn, task)

    def forget(self, context_id: str, turn: Any) -> None:
        """Drop the record of *turn*; a newer turn of the conversation stays."""
        active = self._turns.get(context_id)
        if active is not None and active[0] is turn:
            del self._turns[context_id]

    def get(self, context_id: str) -> Optional[tuple[Any, asyncio.Task]]:
        """The registered (turn, task), finished or not; None when there is none."""
        return self._turns.get(context_id)

    def running(self, context_id: str) -> Optional[tuple[Any, asyncio.Task]]:
        """The registered (turn, task) while its task has not finished."""
        active = self._turns.get(context_id)
        return active if active is not None and not active[1].done() else None

    def is_running(self, context_id: str) -> bool:
        return self.running(context_id) is not None

    # -- background work -------------------------------------------------------
    def spawn(self, coroutine: Coroutine[Any, Any, Any], name: str = "chat-background") -> asyncio.Task:
        """Run *coroutine* beside the command loop, owned here until it ends."""
        task = asyncio.get_running_loop().create_task(coroutine, name=name)
        self._background.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Chat background work %s failed", task.get_name(), exc_info=task.exception())

    # -- lifecycle -------------------------------------------------------------
    @property
    def idle(self) -> bool:
        """Nothing claimed, running or in the background."""
        return not (self._claimed or self._background or any(not task.done() for _, task in self._turns.values()))

    async def close(self) -> None:
        """Cancel every turn and background task and wait for them to end."""
        tasks = [task for _, task in self._turns.values()] + list(self._background)
        for task in tasks:
            task.cancel()
        for task in tasks:
            # A turn reports its own failure; background failures are logged
            # by _finished. Closing only waits for them to end.
            with suppress(asyncio.CancelledError, Exception):
                await task
