"""
Reactive compact for Grid.

Mirrors Claude Code's reactive compact pattern:

When a context-length-exceeded / prompt-too-long error is caught after an
API call, reactively compact the messages and retry the original request.

Strategy (matches CC's reactiveCompact.ts approach):
1. Detect the error is a context-overflow error.
2. Extract token gap from the error message if possible.
3. Use truncate_head_for_ptl_retry to drop the oldest API-round groups.
4. Repeat until the request succeeds or we run out of messages.

Unlike the previous implementation, this does NOT do LLM summarization —
it only truncates. For full summarization, auto_compact_if_needed or
compact_conversation should be called first. The reactive path is a last-resort
escape hatch for when the API request itself hits the limit.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
from utils.logger import Logger

from .base import CompactMessage
from .grouping import truncate_head_for_ptl_retry
from .utils import estimate_message_tokens, create_compact_boundary_message

logger = Logger.get_logger("compact.reactive")


# Error patterns that indicate context overflow
_PTL_PATTERNS = (
    "prompt_too_long",
    "context_length_exceeded",
    "context window",
    "maximum context length",
    "token limit exceeded",
    "max_tokens",
)


class ReactiveCompactStatus(str, Enum):
    SUCCESS = "success"
    TRIMMED = "trimmed"       # Truncated but no LLM summary
    EXHAUSTED = "exhausted"   # Ran out of messages to drop
    ERROR = "error"           # Not a PTL error


@dataclass
class ReactiveCompactResult:
    status: ReactiveCompactStatus
    messages: List[CompactMessage] = field(default_factory=list)
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0
    attempts: int = 0
    error: Optional[str] = None
    strategy_used: str = "truncate"


def is_prompt_too_long_error(error: Exception) -> bool:
    """
    Check if an exception indicates context overflow.
    Mirrors CC's check for PROMPT_TOO_LONG_ERROR_MESSAGE.
    """
    text = str(error).lower()
    return any(p in text for p in _PTL_PATTERNS)


def get_token_gap_from_error(error: Exception) -> Optional[int]:
    """
    Extract the token gap (tokens over limit) from an error message.
    Mirrors CC's getPromptTooLongTokenGap().
    """
    text = str(error)
    m = re.search(r"context window is (\d+).*?but (\d+)", text)
    if m:
        return int(m.group(2)) - int(m.group(1))
    m = re.search(r"reduced by (\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"exceeds maximum of (\d+)", text)
    if m:
        return int(m.group(1)) // 10
    return None


def reactive_compact(
    messages: List[CompactMessage],
    error: Exception,
    max_attempts: int = 3,
) -> ReactiveCompactResult:
    """
    Reactively compact messages after a context-overflow error.

    Drops the oldest API-round group(s) until the gap is covered, or until
    we've made max_attempts drops. This is a synchronous truncation — no LLM
    call. The caller should retry the original request with result.messages.

    Mirrors CC's reactive compact truncation strategy:
    "Drops the oldest API-round groups until tokenGap is covered.
     Falls back to dropping 20% of groups when the gap is unparseable."

    Args:
        messages: Current messages that caused the overflow.
        error: The exception caught from the API.
        max_attempts: Maximum truncation rounds.

    Returns:
        ReactiveCompactResult with the (possibly truncated) message list.
    """
    if not is_prompt_too_long_error(error):
        return ReactiveCompactResult(
            status=ReactiveCompactStatus.ERROR,
            messages=messages,
            error=f"Not a context-overflow error: {error}",
        )

    tokens_before = estimate_message_tokens(messages)
    current = messages
    attempts = 0

    for attempt in range(max_attempts):
        token_gap = get_token_gap_from_error(error)
        truncated = truncate_head_for_ptl_retry(current, token_gap)

        if truncated is None:
            # Cannot truncate further
            logger.warning(
                f"Reactive compact: cannot truncate further after {attempt} attempts"
            )
            return ReactiveCompactResult(
                status=ReactiveCompactStatus.EXHAUSTED,
                messages=current,
                tokens_before=tokens_before,
                tokens_after=estimate_message_tokens(current),
                tokens_saved=tokens_before - estimate_message_tokens(current),
                attempts=attempt,
            )

        current = truncated
        attempts = attempt + 1

        logger.info(
            f"Reactive compact attempt {attempts}: "
            f"dropped messages, now {len(current)} messages"
        )

        # Single truncation is enough — caller retries and may hit another PTL,
        # which will call reactive_compact again.
        break

    tokens_after = estimate_message_tokens(current)

    return ReactiveCompactResult(
        status=ReactiveCompactStatus.TRIMMED,
        messages=current,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        tokens_saved=tokens_before - tokens_after,
        attempts=attempts,
        strategy_used="truncate",
    )


async def reactive_compact_on_prompt_too_long(
    messages: List[CompactMessage],
    error: Exception,
    max_attempts: int = 3,
) -> ReactiveCompactResult:
    """
    Async entry point for reactive compaction (for API compatibility with agent_factory).
    Delegates to the synchronous reactive_compact().
    """
    return reactive_compact(messages, error, max_attempts=max_attempts)


def is_reactive_mode() -> bool:
    """
    True if only reactive compaction is enabled (auto-compact disabled).
    Mirrors CC's isReactiveOnlyMode().
    """
    from .auto_compact import is_auto_compact_enabled
    return not is_auto_compact_enabled()


__all__ = [
    "ReactiveCompactStatus",
    "ReactiveCompactResult",
    "is_prompt_too_long_error",
    "get_token_gap_from_error",
    "reactive_compact",
    "reactive_compact_on_prompt_too_long",
    "is_reactive_mode",
]
