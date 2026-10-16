"""One step of a lazy task, run as a chat turn of the task's own conversation.

The step starts the way a planned restart resumes a turn (web_chat.restart):
a chat session with no browser behind it starts the turn in the user's space,
so the step is an ordinary turn - traced, checkpointed, counted against the
user's limits, watchable live from the chat, resumed by a planned restart.

While it runs, the step is watched every few seconds and asked to stop after
the step it is on - never cut mid-tool - when:

- the service wants it stopped (task paused or cancelled, pool paused, the
  quota ran out: the ``should_stop`` callback);
- it ran far past its estimate;
- the user started a turn of their own and asked agents to yield.

A stopped step leaves an interruption in its conversation; its next run
continues it with everything already done.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Optional

from core.context import is_tool_result
from core.lazy.model import Settings, Step, Task
from web_chat.lazy.prompts import step_message

logger = logging.getLogger("grid.web_chat.lazy")

#: Past this many times its estimate, a step is stopped and continued later.
OVERRUN = 3.0
#: How long a graceful stop may take before the turn is cancelled.
GRACE = 120.0
POLL = 5.0


class HeadlessSocket:
    """What a step's turn would send a browser; kept only to read a refusal from."""

    def __init__(self) -> None:
        self.events: Deque[dict] = deque(maxlen=50)

    async def send_json(self, event: dict) -> None:
        self.events.append(event)

    def refusal(self) -> str:
        for event in reversed(self.events):
            if event.get("type") in ("error", "busy") and event.get("content"):
                return str(event["content"])
        return ""


@dataclass
class StepOutcome:
    #: "answered", "interrupted", "stopped", "error" - the turn's - or "refused" when none started.
    outcome: str
    started_at: float
    ended_at: float
    tokens: int = 0
    answer: str = ""
    #: Why it stopped short, in words for the user.
    reason: str = ""
    conversation_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def last_answer(space: Any, context_id: str) -> str:
    view = space.context_manager().conversation_view(context_id) or {}
    for message in reversed(view.get("messages") or []):
        if getattr(message, "role", None) == "assistant" and not is_tool_result(message):
            content = getattr(message, "content", "")
            return content if isinstance(content, str) else str(content)
    return ""


def ensure_conversation(space: Any, task: Task) -> str:
    """The task's conversation, made on its first step (or again, if the user deleted it)."""
    manager = space.context_manager()
    if task.conversation_id and manager.has_context(task.conversation_id):
        return task.conversation_id
    context_id = manager.start_new_context()
    space.update_conversation_metadata(
        context_id,
        created_by_web=True,
        title=f"Lazy · {task.title}",
        system_key=task.system,
        agent_key=task.agent,
        lazy_task=task.id,
    )
    assign = getattr(space, "assign_chat_space", None)
    if assign is not None:
        assign(context_id)
    return context_id


class StepRunner:
    def __init__(self, spaces: Any, *, clock: Callable[[], float] = time.time, poll: float = POLL, grace: float = GRACE) -> None:
        self._spaces = spaces
        self._clock = clock
        self._poll = poll
        self._grace = grace

    async def run(
        self,
        task: Task,
        step: Step,
        settings: Settings,
        *,
        should_stop: Callable[[], Optional[str]] = lambda: None,
        on_conversation: Callable[[str], None] = lambda context_id: None,
    ) -> StepOutcome:
        from web_chat.session import ChatSession

        started = self._clock()
        async with self._spaces.use(task.user_id) as space:
            context_id = ensure_conversation(space, task)
            on_conversation(context_id)
            manager = space.context_manager()
            active = space.turns.running(context_id)
            if active is None:
                if space.turns.is_claimed(context_id):
                    return StepOutcome("refused", started, self._clock(), reason="The conversation is busy", conversation_id=context_id)
                resume = step.resume and manager.pending_interruption(context_id) is not None
                socket = HeadlessSocket()
                session = ChatSession(space, socket, context_id)
                message = None if resume else step_message(task, step)
                if not await session._start(message, task.system, task.agent):
                    return StepOutcome(
                        "refused", started, self._clock(),
                        reason=socket.refusal() or "The turn did not start", conversation_id=context_id,
                    )
                active = space.turns.get(context_id)
            if active is None:
                return StepOutcome("error", started, self._clock(), reason="The turn vanished", conversation_id=context_id)
            turn, job = active
            stop_reason: Optional[str] = None
            stop_at = 0.0
            while not job.done():
                await asyncio.wait({job}, timeout=self._poll)
                if job.done():
                    break
                if stop_reason is None:
                    stop_reason = should_stop() or self._overrun(turn, step) or self._user_working(space, context_id, settings)
                    if stop_reason is not None:
                        stop_at = self._clock()
                        logger.info("Stopping lazy step %s/%s: %s", task.id, step.id, stop_reason)
                        if not turn.request_stop():
                            job.cancel()
                elif self._clock() - stop_at > self._grace:
                    job.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await job
            outcome = getattr(turn, "outcome", None) or ("stopped" if stop_reason else "error")
            tokens = sum(getattr(turn, "tokens", (0, 0)))
            return StepOutcome(
                outcome,
                started,
                self._clock(),
                tokens=tokens,
                answer=last_answer(space, context_id) if outcome == "answered" else "",
                reason=stop_reason or "",
                conversation_id=context_id,
            )

    @staticmethod
    def _overrun(turn: Any, step: Step) -> Optional[str]:
        tokens = sum(getattr(turn, "tokens", (0, 0)))
        if step.tokens and tokens > OVERRUN * step.tokens:
            return f"Ran past {OVERRUN:g}x its estimate"
        return None

    @staticmethod
    def _user_working(space: Any, context_id: str, settings: Settings) -> Optional[str]:
        if not settings.yield_to_user:
            return None
        others = [key for key, _turn, _job in space.turns.running_turns() if key != context_id]
        return "You started working: the subscription is yours" if others else None
