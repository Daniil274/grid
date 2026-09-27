"""Which failures of a model call are worth another attempt.

Provider errors that pass (timeouts, rate limits, 5xx, transient
messages) are retried with backoff; everything else is final. Also spots an
answer that wrote tool calls as text instead of calling tools.
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
from utils.exceptions import AgentError

logger = logging.getLogger("grid.agent_factory")


class FailureRules:
    """Which failures of a model call are worth another attempt.

    Provider errors that pass (timeouts, rate limits, 5xx, transient
    messages) are retried with backoff; everything else is final. Also spots an
    answer that wrote tool calls as text instead of calling tools.
    """

    @staticmethod
    def _message_looks_transient_provider_error(message: str) -> bool:
        text = (message or "").lower()
        transient_markers = (
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
        return any(marker in text for marker in transient_markers)

    def _is_retriable_agent_exception(self, exc: Exception) -> bool:
        """Decide whether agent execution should be retried indefinitely."""
        if isinstance(exc, AllModelsFailedError):
            return False
        if isinstance(
            exc,
            (
                asyncio.TimeoutError,
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RemoteProtocolError,
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                RateLimitError,
            ),
        ):
            return True
        if isinstance(exc, APIStatusError):
            status_code = getattr(exc, "status_code", None)
            return status_code in {408, 409, 425, 429, 500, 502, 503, 504}
        if isinstance(exc, AgentError):
            return self._message_looks_transient_provider_error(str(exc))
        if isinstance(exc, Exception):
            return self._message_looks_transient_provider_error(str(exc))
        return False

    @staticmethod
    def _retry_backoff_seconds(retry_count: int) -> float:
        """Backoff that rises quickly but stays bounded for endless retries."""
        schedule = [1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 30.0, 45.0, 60.0]
        if retry_count <= 0:
            return schedule[0]
        return schedule[min(retry_count, len(schedule) - 1)]

    # Fallback: stub for manual tool call parsing from response text
    def _detect_malformed_tool_calls(self, output: str) -> bool:
        """
        Detect malformed tool call formats in agent output.

        Returns True if malformed tool calls are detected.
        Examples of malformed formats:
        - <tool_call><function=get_screen><parameter=save_path>...</parameter></function></tool_call>
        """
        # Pattern for malformed tool_call format
        malformed_patterns = [
            r"<tool_call>\s*<function=",  # <tool_call><function=...>
            r"<function=[^>]+>\s*<parameter=",  # <function=name><parameter=...>
        ]

        for pattern in malformed_patterns:
            if re.search(pattern, output):
                logger.warning(
                    f"Detected malformed tool call format in output: {pattern}"
                )
                return True

        return False
