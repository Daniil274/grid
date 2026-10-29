"""One Telegram message as a chat turn of the user's own conversation.

The turn starts the way a lazy step does (web_chat.lazy.runner): a chat
session with no browser behind it starts the turn in the user's space, so it
is an ordinary turn - traced, checkpointed, counted against the user's
limits, and visible in the web chat afterwards.

While it runs, the runner calls its watcher every few seconds; that is where
the channel publishes the calls waiting for the user's approval as inline
buttons. A message that arrives while a turn is running is refused as busy
instead of queueing: the chat stays one turn behind, never a backlog.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from web_chat.lazy.runner import HeadlessSocket, last_answer

logger = logging.getLogger("grid.web_chat.telegram")

#: How often a running turn is looked at (approvals, the time limit).
POLL = 2.0
#: How long a graceful stop may take before the turn is cancelled.
GRACE = 60.0
#: A turn running longer than this is asked to stop at a safe point.
LIMIT = 30 * 60.0


@dataclass
class TurnOutcome:
    #: "answered", "busy", "refused", "interrupted", "error", "timeout".
    outcome: str
    answer: str = ""
    reason: str = ""
    tokens: int = 0
    context_id: str | None = None


class TurnRunner:
    """Runs one message as a turn and reports what the user should be told."""

    def __init__(self, spaces: Any, *, poll: float = POLL, grace: float = GRACE,
                 limit: float | None = LIMIT, system: str | None = None,
                 agent: str | None = None) -> None:
        self._spaces = spaces
        self._poll = poll
        self._grace = grace
        self._limit = limit
        self._system = system
        self._agent = agent

    async def run(self, user_id: str, context_id: str, message: str, *,
                  on_watch: Callable[[Any, str], Awaitable[None]] | None = None,
                  on_context: Callable[[str], None] | None = None) -> TurnOutcome:
        """Start the turn in *context_id* and wait for its end."""
        from web_chat.session import ChatSession

        async with self._spaces.use(user_id) as space:
            self._prepare(space, context_id)
            if on_context is not None:
                on_context(context_id)
            active = space.turns.running(context_id)
            if active is None:
                if space.turns.is_claimed(context_id):
                    return TurnOutcome("busy", reason="The conversation already has an active turn.",
                                       context_id=context_id)
                socket = HeadlessSocket()
                session = ChatSession(space, socket, context_id)
                if not await session._start(message, self._system, self._agent):
                    return TurnOutcome("refused", reason=socket.refusal() or "The turn did not start.",
                                       context_id=context_id)
                active = space.turns.running(context_id)
            if active is None:
                return TurnOutcome("error", reason="The turn vanished.", context_id=context_id)
            turn, job = active
            started = time.monotonic()
            stop_reason: str | None = None
            stop_at = 0.0
            while not job.done():
                await asyncio.wait({job}, timeout=self._poll)
                if job.done():
                    break
                if on_watch is not None:
                    with suppress(Exception):
                        await on_watch(space, context_id)
                if stop_reason is None and self._overran(started):
                    stop_reason = "It ran longer than this channel allows; ask again to continue."
                    stop_at = time.monotonic()
                    logger.info("Stopping Telegram turn in %s: time limit", context_id)
                    if not turn.request_stop():
                        job.cancel()
                elif stop_reason is not None and time.monotonic() - stop_at > self._grace:
                    job.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await job
            outcome = getattr(turn, "outcome", None) or ("interrupted" if stop_reason else "error")
            answer = last_answer(space, context_id) if outcome == "answered" else ""
            return TurnOutcome(
                "timeout" if stop_reason and outcome != "answered" else outcome,
                answer=answer,
                reason=stop_reason or "",
                tokens=sum(getattr(turn, "tokens", (0, 0))),
                context_id=context_id,
            )

    def _prepare(self, space: Any, context_id: str) -> None:
        """Name the conversation the first time the chat writes into it."""
        manager = space.context_manager()
        fresh = not manager.has_context(context_id)
        if fresh:
            manager.ensure_context(context_id)
            space.update_conversation_metadata(
                context_id,
                created_by_web=True,
                title="Telegram",
                system_key=self._system,
                agent_key=self._agent,
            )
            assign = getattr(space, "assign_chat_space", None)
            if assign is not None:
                assign(context_id)

    def _overran(self, started: float) -> bool:
        return self._limit is not None and time.monotonic() - started > self._limit
