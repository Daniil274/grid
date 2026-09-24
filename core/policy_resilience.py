"""Bounded delivery of policy checks; a valid verdict is never retried.

All state belongs to one gate/event loop. Routes are operator configured and
receive identical questions and context. No action classification happens here.
"""

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from schemas.action_policy import ActionValidatorConfig


def failure_reason(exc: BaseException) -> str:
    """Stable categories only: exception messages may contain secrets."""
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "invalid_answer"
    return type(exc).__name__


def retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 429, 500, 502, 503, 504)
    return isinstance(
        exc,
        (asyncio.TimeoutError, httpx.TransportError, ValueError, KeyError, TypeError),
    )


def retry_after(exc: BaseException) -> float:
    if not isinstance(exc, httpx.HTTPStatusError):
        return 0
    if exc.response.status_code not in (429, 503):
        return 0
    value = exc.response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError, OSError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


@dataclass
class Route:
    validator: Any
    failures: int = 0
    open_until: float = 0
    retry_at: float = 0
    probing: bool = False
    failure_generation: int = 0

    @property
    def key(self):
        return getattr(getattr(self.validator, "config", None), "model", None)

    @property
    def model(self):
        return getattr(getattr(self.validator, "model", None), "model_name", None)


@dataclass
class Judgment:
    verdicts: dict = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    queue_ms: float = 0
    model: str | None = None


class PolicyRunner:
    def __init__(self, config: ActionValidatorConfig, validators: list):
        self.config = config
        self.routes = [Route(validator) for validator in validators]
        self._slots = asyncio.Semaphore(config.max_concurrency)

    async def evaluate(self, packet: dict, *, chain: bool) -> Judgment:
        result = Judgment()
        started = time.monotonic()
        acquired = False

        async def bounded():
            nonlocal acquired
            async with self._slots:
                acquired = True
                result.queue_ms = round((time.monotonic() - started) * 1000, 3)
                await self._judge(
                    packet, chain, result, started + self.config.timeout_seconds
                )

        try:
            # Includes queue, backoff, Retry-After and every provider request.
            await asyncio.wait_for(bounded(), timeout=self.config.timeout_seconds)
        except asyncio.TimeoutError:
            if not acquired:
                result.queue_ms = round((time.monotonic() - started) * 1000, 3)
            result.failures.append(
                "queue_timeout" if not acquired else "budget_timeout"
            )
        return result

    async def _judge(
        self, packet: dict, chain: bool, result: Judgment, deadline: float
    ):
        counts = [0] * len(self.routes)
        disabled = set()
        next_try = [0.0] * len(self.routes)
        expected = {"action", "chain"} if chain else {"action"}
        while len(result.attempts) < self.config.max_attempts:
            now = time.monotonic()
            candidates = [
                i
                for i, route in enumerate(self.routes)
                if i not in disabled and not route.probing and route.open_until <= now
            ]
            if not candidates:
                result.failures.append("routes_unavailable")
                return
            ready = [
                i
                for i in candidates
                if max(next_try[i], self.routes[i].retry_at) <= now
            ]
            if not ready:
                delay = (
                    min(max(next_try[i], self.routes[i].retry_at) for i in candidates)
                    - now
                )
                await asyncio.sleep(delay)
                continue
            # Try the reserve before repeating a failed primary. Never seek a
            # second opinion after a valid deny/review (or allow).
            index = min(ready, key=lambda i: (counts[i], i))
            route = self.routes[index]
            generation = route.failure_generation
            probe = bool(route.open_until)
            if probe:
                route.probing = True
            counts[index] += 1
            event = {"route": route.key, "model": route.model, "attempt": counts[index]}
            result.attempts.append(event)
            started = time.monotonic()
            try:
                # A provider timeout larger than the whole budget must not
                # starve the reserve. Its own request timeout may be shorter.
                attempts_left = self.config.max_attempts - len(result.attempts) + 1
                if index > 0 and all(
                    i in disabled or self.routes[i].open_until > started
                    for i in range(index)
                ):
                    # Once preceding routes are known down, do not reserve half
                    # the deadline for them. The chat reserve has its own cap.
                    attempts_left = 1
                attempt_budget = max(0, deadline - started) / attempts_left
                answer = await asyncio.wait_for(
                    route.validator.evaluate(packet, chain=chain),
                    timeout=attempt_budget,
                )
                if not isinstance(answer, dict) or set(answer) != expected:
                    raise ValueError("Invalid validator answer set")
                if any(
                    value not in ("allow", "deny", "review")
                    for value in answer.values()
                ):
                    raise ValueError("Invalid validator verdict")
                result.verdicts = answer
                result.model = route.model
                event["outcome"] = "answered"
                # An older in-flight success must not erase a newer outage or
                # server cooldown observed by a concurrent judgment.
                if route.failure_generation == generation:
                    route.failures = 0
                    route.open_until = 0
                return
            except asyncio.CancelledError:
                event["outcome"] = "cancelled"
                raise
            except Exception as exc:
                reason = failure_reason(exc)
                result.failures.append(reason)
                event["outcome"] = reason
                route.failures += 1
                route.failure_generation += 1
                can_retry = retryable(exc)
                now = time.monotonic()
                if not can_retry:
                    disabled.add(index)
                if (
                    not can_retry
                    or probe
                    or route.failures >= self.config.circuit_failure_threshold
                ):
                    route.open_until = now + self.config.circuit_cooldown_seconds
                # Respect a server's cooldown across concurrent judgments, too.
                route.retry_at = max(route.retry_at, now + retry_after(exc))
                backoff = self.config.retry_backoff_seconds * 2 ** (counts[index] - 1)
                next_try[index] = now + random.uniform(backoff / 2, backoff)
            finally:
                event["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
                if probe:
                    route.probing = False
