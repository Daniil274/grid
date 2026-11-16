"""Which failures of a model call are worth another attempt.

A provider failure that passes - a timeout, a dropped connection, a rate
limit, a 5xx, or a message saying as much - is retried with a bounded
backoff; any other failure is final. When every model of an agent's fallback
chain has failed, the chain already did its retrying: that is final too.

Also: spotting an answer that wrote tool calls as text instead of calling
the tools, which the turn then corrects (core.factory.turns).
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

from core.fallback_model import AllModelsFailedError
from core.bounded_model import ModelRequestLimit
from core.credentials import credential_error_in

logger = logging.getLogger("grid.agent_factory")

_TRANSIENT_MARKERS = (
    "connection error",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "remote protocol error",
    "server disconnected",
    "connection reset",
    "network",
    "rate limit",
    "overloaded",
    "stream closed",
    "incomplete chunked read",
    "all providers exhausted",
    "upstream_unavailable",
    "upstream unavailable",
    "server_error",
    "server error",
    "provider",
    "retry",
    "unavailable",
)

_TRANSIENT_ERRORS = (
    asyncio.TimeoutError,
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

_TRANSIENT_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

_BACKOFF_SECONDS = (1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 30.0, 45.0, 60.0)

_TOOL_CALLS_AS_TEXT = (
    re.compile(r"<tool_call>\s*<function="),  # <tool_call><function=...>
    re.compile(r"<function=[^>]+>\s*<parameter="),  # <function=name><parameter=...>
)

#: A 429 that names a spent account, not a burst of traffic: Kimi answers
#: ``exceeded_current_quota_error`` ("suspended due to insufficient balance"),
#: OpenAI ``insufficient_quota``. Waiting does not clear it, so retrying the same
#: model only delays the fallback to one that can answer. Falling back is still
#: wanted - this decides retries, not which model runs next.
_SPENT_ACCOUNT_MARKERS = (
    "insufficient balance",
    "insufficient_quota",
    "exceeded_current_quota",
    "suspended due to insufficient",
    "billing details",
)


def is_transient_message(message: str) -> bool:
    """Whether an error's text says the failure will pass."""
    text = (message or "").lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def is_retriable(exc: BaseException) -> bool:
    """Whether a failed model call is worth another attempt."""
    if isinstance(exc, ModelRequestLimit):
        return False
    if isinstance(exc, AllModelsFailedError):
        # A chain shortened to one live model still carries that model's own
        # failure: its transient outage is worth another attempt, though the
        # message names the models that were skipped. Several failures mean at
        # least one was not transient, and the whole is final.
        cause = exc.cause
        if cause is None or credential_error_in(cause) is not None:
            return False  # a refused credential is the key's, not an outage
        return is_retriable(cause)
    if any(marker in str(exc).lower() for marker in _SPENT_ACCOUNT_MARKERS):
        return False
    if isinstance(exc, _TRANSIENT_ERRORS):
        return True
    if isinstance(exc, APIStatusError):
        return getattr(exc, "status_code", None) in _TRANSIENT_STATUS_CODES
    return is_transient_message(str(exc))


def retry_backoff_seconds(retry_count: int) -> float:
    """The wait before retry *retry_count*: rises quickly, never past a minute."""
    return _BACKOFF_SECONDS[min(max(retry_count, 0), len(_BACKOFF_SECONDS) - 1)]


def writes_tool_calls_as_text(output: str) -> bool:
    """Whether an answer wrote tool calls as text, e.g.
    ``<tool_call><function=name><parameter=p>...</parameter></function></tool_call>``,
    which runs nothing."""
    for pattern in _TOOL_CALLS_AS_TEXT:
        if pattern.search(output):
            logger.warning("Detected tool calls written as text: %s", pattern.pattern)
            return True
    return False
