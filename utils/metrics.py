"""Utility module for lightweight runtime metrics collection.

The module provides a very small in‑memory metrics collector that can be used
throughout the code base without any external dependencies.  It is deliberately
simple – the goal is to expose counters and timing information that the
self‑improvement engine can later read.

Typical usage::

    from utils.metrics import metrics

    metrics.inc("agent_runs")
    with metrics.timeit("agent_execution"):
        ...  # code you want to time

    snapshot = metrics.snapshot()  # {'counters': {...}, 'avg_times': {...}}
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Dict, List


class _Metrics:
    """Thread‑safe container for counters and timers.

    The implementation is intentionally tiny – it stores counters in a ``dict``
    and keeps a list of elapsed times for each named timer.  All operations are
    guarded by a ``Lock`` so the collector works correctly in multi‑threaded
    environments (the agent framework itself is async but may still spawn threads
    for I/O)."""

    def __init__(self) -> None:
        self._counters: Dict[str, int] = {}
        self._timers: Dict[str, List[float]] = {}
        self._lock = Lock()

    # ---------------------------------------------------------------------
    # Counter API
    # ---------------------------------------------------------------------
    def inc(self, name: str, amount: int = 1) -> None:
        """Increment a named counter by *amount* (default 1)."""
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    # ---------------------------------------------------------------------
    # Timer API – used as a context manager
    # ---------------------------------------------------------------------
    def timeit(self, name: str):
        """Return a context manager that records the elapsed time.

        Example::

            with metrics.timeit("agent_execution"):
                run_some_long_operation()
        """

        class _Timer:
            def __enter__(self_inner):
                self_inner.start = time.perf_counter()
                return self_inner

            def __exit__(self_inner, exc_type, exc, tb):
                elapsed = time.perf_counter() - self_inner.start
                with self._lock:
                    self._timers.setdefault(name, []).append(elapsed)

        return _Timer()

    # ---------------------------------------------------------------------
    # Snapshot – a serialisable view of current metrics
    # ---------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Return a shallow copy of counters and average timings.

        The returned structure is JSON‑serialisable and can be fed directly to a
        LLM that will propose configuration changes.
        """
        with self._lock:
            avg_times = {
                key: (sum(times) / len(times) if times else 0.0)
                for key, times in self._timers.items()
            }
            return {
                "counters": dict(self._counters),
                "avg_times": avg_times,
            }


# A module‑level singleton that the rest of the code imports.
metrics = _Metrics()
