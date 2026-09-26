"""The live spaces of a server: at most one per user, built on demand.

Two spaces of one user must never exist at once: each holds a ContextManager
that writes the user's conversation file, and the second would overwrite the
first. So the pool is the only way to reach a space, and it replaces or drops
one only when nothing can still be using it:

- every request and socket holds a *lease* while it runs (:meth:`SpacePool.use`);
- a space is *idle* when no turn is claimed or running and no background work
  is left (UserSpace.idle) - a turn outlives the socket that started it.

A space with no lease that is idle can go. That happens when

- the configs changed (:meth:`SpacePool.invalidate`): a space is *stale* and is
  rebuilt the next time it is free, so a running turn finishes on the configs it
  started with;
- it has been unused for ``idle_seconds`` (:meth:`SpacePool.sweep`), so a
  server with many users keeps only the active ones loaded.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Dict, Iterator, Optional

from web_chat.space import UserSpace

logger = logging.getLogger("grid.web_chat.spaces")


@dataclass
class _Entry:
    space: UserSpace
    leases: int = 0
    stale: bool = False
    last_used: float = field(default_factory=time.monotonic)

    @property
    def free(self) -> bool:
        return self.leases == 0 and self.space.idle


class SpacePool:
    """Builds, lends and retires the spaces of a server."""

    def __init__(
        self,
        build: Callable[[str], UserSpace],
        *,
        idle_seconds: Optional[float] = None,
    ) -> None:
        """``build(user_id)`` makes a user's space; ``idle_seconds`` None keeps
        every space loaded until it is stale."""
        self._build = build
        self._idle_seconds = idle_seconds
        self._entries: Dict[str, _Entry] = {}
        # One lock per user who ever connected: building, replacing and retiring
        # that user's space happen under it, so they never overlap, while other
        # users' spaces are built at the same time.
        self._locks: Dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def use(self, user_id: str) -> AsyncIterator[UserSpace]:
        """Lend the user's space for the duration of the block."""
        entry = await self._lease(user_id)
        try:
            yield entry.space
        finally:
            entry.leases -= 1
            entry.last_used = time.monotonic()

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def _lease(self, user_id: str) -> _Entry:
        async with self._lock(user_id):
            entry = self._entries.get(user_id)
            if entry is not None and entry.stale and entry.free:
                await self._retire(user_id)
                entry = None
            if entry is None:
                # Building reads files and may start a container: off the loop.
                space = await asyncio.to_thread(self._build, user_id)
                entry = self._entries[user_id] = _Entry(space)
            entry.leases += 1
            entry.last_used = time.monotonic()
            return entry

    def live(self) -> Iterator[UserSpace]:
        """The spaces loaded now."""
        return iter([entry.space for entry in self._entries.values()])

    def invalidate(self) -> None:
        """The configs changed: rebuild every space once it is free."""
        for entry in self._entries.values():
            entry.stale = True

    async def sweep(self) -> int:
        """Retire the free spaces that are stale or unused for too long; how many."""
        retired = 0
        for user_id in list(self._entries):
            async with self._lock(user_id):
                entry = self._entries.get(user_id)
                if entry is not None and entry.free and (entry.stale or self._expired(entry)):
                    await self._retire(user_id)
                    retired += 1
        return retired

    def _expired(self, entry: _Entry) -> bool:
        return self._idle_seconds is not None and time.monotonic() - entry.last_used >= self._idle_seconds

    async def _retire(self, user_id: str) -> None:
        """Close and drop the user's space; the caller holds the user's lock."""
        space = self._entries.pop(user_id).space
        await self._close(space)

    async def close(self) -> None:
        """Close every space, running turns included: the server is going down."""
        for user_id in list(self._entries):
            async with self._lock(user_id):
                if user_id in self._entries:
                    await self._retire(user_id)

    @staticmethod
    async def _close(space: UserSpace) -> None:
        try:
            await space.close()
        except Exception:
            logger.exception("Closing the space of %s failed", space.user_id)
