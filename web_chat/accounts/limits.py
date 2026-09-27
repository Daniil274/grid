"""Slowing down password guessing: failures are counted and, past a limit, refused.

Failures are counted per key over a sliding window. The HTTP layer counts each
failed sign-in twice: under the username and the client address, and under the
address alone, so one address cannot try many passwords on one account or a
few passwords on many accounts. A key over its limit is refused before the
password is even checked, until its oldest failure leaves the window.

The counts live in memory: a restart forgets them, which costs an attacker a
restart they cannot cause. A server behind several processes would move them
to shared storage.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict


class AttemptLimiter:
    """At most ``limit`` failures per key within ``window`` seconds."""

    #: Keys kept at most; past it, keys whose failures all left the window go
    #: at once, so a spray of made-up usernames cannot grow memory unbounded.
    MAX_KEYS = 100_000

    def __init__(self, *, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._failures: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> float:
        """Seconds until *key* may try again; 0 when it may try now. Records nothing."""
        with self._lock:
            if key not in self._failures:
                return 0.0
            failures = self._recent(key)
            if len(failures) < self._limit:
                return 0.0
            return max(0.0, failures[0] + self._window - self._clock())

    def fail(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(self._clock())
            if len(self._failures) > self.MAX_KEYS:
                self._prune_locked()

    def forget(self, key: str) -> None:
        """A success: earlier failures of *key* no longer count."""
        with self._lock:
            self._failures.pop(key, None)

    def _recent(self, key: str) -> Deque[float]:
        failures = self._failures.setdefault(key, deque())
        horizon = self._clock() - self._window
        while failures and failures[0] <= horizon:
            failures.popleft()
        return failures

    def prune(self) -> None:
        """Drop keys with no failure left in the window, so memory stays bounded."""
        with self._lock:
            self._prune_locked()

    def _prune_locked(self) -> None:
        for key in [key for key in self._failures if not self._recent(key)]:
            del self._failures[key]
