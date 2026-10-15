"""Run deadlines that do not count the time a person takes to answer.

A turn, a sub-agent's run and a call into another system are each bounded in
seconds (``settings.agent_timeout``, an agent's ``timeout``, the orchestrator's
``timeout_seconds``). A call the action policy holds for the user's approval
(core.action_policy) waits in the middle of such a run; on the wall clock that
wait counted against the agent, so a sub-agent asking for permission was killed
while the request still sat in the chat - and its retry asked again and died the
same way.

:class:`Deadline` is ``asyncio.timeout`` that can be paused. Every deadline a
run enters is recorded in a context variable, which child tasks inherit, so the
code that waits for a person - :func:`waiting_for_person` - pauses all
deadlines around it, nearest to outermost, and resumes each with the time it had
left. Pauses are counted: parallel sub-agents waiting under one turn keep the
turn's deadline paused until the last of them is answered.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

_ACTIVE: ContextVar[tuple["Deadline", ...]] = ContextVar("grid_deadlines", default=())


class Deadline:
    """``async with Deadline(seconds)``: ``asyncio.timeout`` that :func:`waiting_for_person` pauses.

    ``seconds`` of None or 0 is no limit - the block runs as long as it needs -
    and pausing it is a no-op, as for ``contextlib.nullcontext``.
    """

    def __init__(self, seconds: float | None) -> None:
        self.seconds = seconds if seconds and seconds > 0 else None
        self._timeout: asyncio.Timeout | None = None
        self._token: Token | None = None
        self._pauses = 0
        self._remaining: float | None = None

    async def __aenter__(self) -> "Deadline":
        if self.seconds is not None:
            self._timeout = asyncio.timeout(self.seconds)
            await self._timeout.__aenter__()
            self._token = _ACTIVE.set((*_ACTIVE.get(), self))
        return self

    async def __aexit__(self, *exc: Any) -> bool | None:
        if self._timeout is None:
            return None
        if self._token is not None:
            try:
                _ACTIVE.reset(self._token)
            except ValueError:  # left from another context than entered: nothing to restore there
                pass
        return await self._timeout.__aexit__(*exc)

    def expired(self) -> bool:
        return self._timeout is not None and self._timeout.expired()

    def pause(self) -> None:
        """Stop the clock; it resumes with the time it had left (see :meth:`resume`)."""
        if self._timeout is None:
            return
        if self._pauses == 0:
            when = self._timeout.when()
            if when is not None and not self._timeout.expired():
                self._remaining = max(0.0, when - asyncio.get_running_loop().time())
                self._reschedule(None)
            else:
                self._remaining = None
        self._pauses += 1

    def resume(self) -> None:
        if self._timeout is None or self._pauses == 0:
            return
        self._pauses -= 1
        if self._pauses == 0 and self._remaining is not None:
            self._reschedule(asyncio.get_running_loop().time() + self._remaining)
            self._remaining = None

    def _reschedule(self, when: float | None) -> None:
        try:
            self._timeout.reschedule(when)
        except RuntimeError:  # already left: its block is over, nothing to bound
            pass


@contextmanager
def waiting_for_person() -> Iterator[None]:
    """Pause every deadline of the current run while the block waits for a person."""
    held = _ACTIVE.get()
    for deadline in held:
        deadline.pause()
    try:
        yield
    finally:
        for deadline in reversed(held):
            deadline.resume()
