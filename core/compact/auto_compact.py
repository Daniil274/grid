"""
Auto compact for Grid.

Mirrors Claude Code's autoCompact.ts:

- getEffectiveContextWindowSize: context_window - MAX_OUTPUT_TOKENS_FOR_SUMMARY
- getAutoCompactThreshold: effective_window - AUTOCOMPACT_BUFFER_TOKENS
- calculateTokenWarningState: warning/error/blocking thresholds
- isAutoCompactEnabled: env-var override check
- shouldAutoCompact: token count check + recursion guard
- autoCompactIfNeeded: circuit breaker → try SM compact → try full compact

Key difference from previous implementation:
- No module-level global _state singleton (was not thread-safe).
- Tracking state is passed as a parameter (AutoCompactTrackingState dataclass),
  threaded through by the caller (agent_factory.py).
- Works purely async-natively, no asyncio.get_event_loop() hacks.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional, Any, TYPE_CHECKING

from .base import (
    CompactMessage,
    CompactionResult,
    CompactionStatus,
    CompactionStrategy,
    TokenWarningState,
)
from .utils import estimate_message_tokens
from .session_memory_compact import (
    try_session_memory_compact,
    set_last_summarized_message_uuid,
)
from .micro_compact import reset_microcompact_state
from .post_compact import run_post_compact_cleanup

if TYPE_CHECKING:
    from schemas import CompactConfig

logger = logging.getLogger("compact.auto")


# ---------------------------------------------------------------------------
# Default constants — used as fallback when config is not provided.
# In production, values are taken from compact.* in config.yaml.
# ---------------------------------------------------------------------------

AUTOCOMPACT_BUFFER_TOKENS = 13_000
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS = 20_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3


# ---------------------------------------------------------------------------
# Tracking state (passed as parameter, not module-level singleton)
# ---------------------------------------------------------------------------

@dataclass
class AutoCompactTrackingState:
    """
    Per-session tracking state for auto-compact.
    Mirrors CC's AutoCompactTrackingState.
    Caller (agent_factory) is responsible for threading this through turns.
    """
    compacted: bool = False
    turn_counter: int = 0
    turn_id: str = ""
    consecutive_failures: int = 0


# ---------------------------------------------------------------------------
# Threshold helpers
# ---------------------------------------------------------------------------

def _auto_cfg(compact_cfg):
    """Returns CompactAutoConfig if CompactConfig is provided, otherwise None."""
    return getattr(compact_cfg, "auto", None) if compact_cfg is not None else None


def _clamp_reserved_summary_tokens(value: int, context_window: int) -> int:
    """Keep the compact reserve useful for threshold math, even with oversized configs."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = MAX_OUTPUT_TOKENS_FOR_SUMMARY
    cap = max(1_000, min(MAX_OUTPUT_TOKENS_FOR_SUMMARY, context_window // 2))
    return max(1_000, min(parsed, cap))


def get_effective_context_window_size(
    context_window: int,
    compact_cfg: "Optional[CompactConfig]" = None,
) -> int:
    """
    Reserve max_output_tokens_for_summary tokens for LLM response during compaction.
    Read from config.yaml → compact.auto.max_output_tokens_for_summary.
    """
    auto = _auto_cfg(compact_cfg)
    reserve = (
        auto.max_output_tokens_for_summary
        if auto is not None
        else MAX_OUTPUT_TOKENS_FOR_SUMMARY
    )
    reserve = _clamp_reserved_summary_tokens(reserve, context_window)
    return context_window - reserve


def get_auto_compact_threshold(
    context_window: int,
    compact_cfg: "Optional[CompactConfig]" = None,
) -> int:
    """
    Token threshold for auto-compact = effective_window - buffer_tokens.
    Read from config.yaml → compact.auto.buffer_tokens.
    Supports env override CLAUDE_AUTOCOMPACT_PCT_OVERRIDE for testing.
    """
    auto = _auto_cfg(compact_cfg)
    effective = get_effective_context_window_size(context_window, compact_cfg)
    buffer = auto.buffer_tokens if auto is not None else AUTOCOMPACT_BUFFER_TOKENS
    threshold = effective - buffer

    pct_override = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    if pct_override:
        try:
            pct = float(pct_override)
            if 0 < pct <= 100:
                pct_threshold = int(effective * pct / 100)
                threshold = min(pct_threshold, threshold)
        except ValueError:
            pass

    return threshold


def calculate_token_warning_state(
    token_usage: int,
    context_window: int,
    compact_cfg: "Optional[CompactConfig]" = None,
) -> TokenWarningState:
    """
    Calculates warning/error/blocking thresholds.
    All thresholds are read from config.yaml → compact.auto.*.
    """
    auto = _auto_cfg(compact_cfg)
    auto_compact_threshold = get_auto_compact_threshold(context_window, compact_cfg)
    effective_window = get_effective_context_window_size(context_window, compact_cfg)

    enabled = is_auto_compact_enabled(compact_cfg)
    threshold_for_pct = auto_compact_threshold if enabled else effective_window

    percent_left = max(
        0,
        round(((threshold_for_pct - token_usage) / max(1, threshold_for_pct)) * 100),
    )

    warn_buf = auto.warning_buffer_tokens if auto is not None else WARNING_THRESHOLD_BUFFER_TOKENS
    err_buf = auto.error_buffer_tokens if auto is not None else ERROR_THRESHOLD_BUFFER_TOKENS
    manual_buf = auto.manual_buffer_tokens if auto is not None else MANUAL_COMPACT_BUFFER_TOKENS

    warning_threshold = auto_compact_threshold - warn_buf
    error_threshold = auto_compact_threshold - err_buf
    blocking_limit = effective_window - manual_buf

    blocking_override = os.environ.get("CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE")
    if blocking_override:
        try:
            parsed = int(blocking_override)
            if parsed > 0:
                blocking_limit = parsed
        except ValueError:
            pass

    return TokenWarningState(
        percent_left=float(percent_left),
        is_above_warning_threshold=token_usage >= warning_threshold,
        is_above_error_threshold=token_usage >= error_threshold,
        is_above_auto_compact_threshold=(
            enabled and token_usage >= auto_compact_threshold
        ),
        is_at_blocking_limit=token_usage >= blocking_limit,
    )


def is_auto_compact_enabled(compact_cfg: "Optional[CompactConfig]" = None) -> bool:
    """
    Checks if auto-compact is enabled.
    Priority: env vars > config.yaml (compact.auto.enabled) > True.
    """
    if os.environ.get("DISABLE_COMPACT", "").lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("DISABLE_AUTO_COMPACT", "").lower() in ("1", "true", "yes"):
        return False
    if compact_cfg is not None and not compact_cfg.enabled:
        return False
    auto = _auto_cfg(compact_cfg)
    if auto is not None and not auto.enabled:
        return False
    return True


def should_auto_compact(
    messages: List[CompactMessage],
    context_window: int,
    is_subagent: bool = False,
    compact_cfg: "Optional[CompactConfig]" = None,
) -> bool:
    """Checks whether auto-compact should run right now."""
    if is_subagent:
        return False
    if not is_auto_compact_enabled(compact_cfg):
        return False

    warning = calculate_token_warning_state(
        estimate_message_tokens(messages), context_window, compact_cfg
    )
    return warning.is_above_auto_compact_threshold


async def auto_compact_if_needed(
    messages: List[CompactMessage],
    context_window: int,
    llm_client: Optional[Any] = None,
    model: Optional[str] = None,
    custom_instructions: Optional[str] = None,
    tracking: Optional[AutoCompactTrackingState] = None,
    is_subagent: bool = False,
    compact_cfg: "Optional[CompactConfig]" = None,
) -> dict:
    """
    Run auto-compact if token usage exceeds threshold.

    Mirrors CC's autoCompactIfNeeded():
    1. Circuit breaker: skip if consecutive_failures >= MAX.
    2. shouldAutoCompact check.
    3. Try session memory compact (cheap, no LLM).
    4. Fall back to full LLM compact.

    Args:
        messages: Current conversation messages.
        context_window: Full context window size for the model.
        llm_client: AsyncOpenAI-compatible client (required for full compact).
        model: Model identifier (required for full compact).
        custom_instructions: Optional custom instructions for full compact.
        tracking: Per-session tracking state (circuit breaker lives here).
        is_subagent: Whether the caller is a sub-agent (skip if True).

    Returns:
        dict with keys:
            "was_compacted": bool
            "compaction_result": Optional[CompactionResult]
            "consecutive_failures": int (updated value)
    """
    if os.environ.get("DISABLE_COMPACT", "").lower() in ("1", "true", "yes"):
        return {"was_compacted": False, "consecutive_failures": 0}
    if compact_cfg is not None and not compact_cfg.enabled:
        return {"was_compacted": False, "consecutive_failures": 0}

    # Circuit breaker
    consecutive = tracking.consecutive_failures if tracking else 0
    auto = _auto_cfg(compact_cfg)
    max_failures = (
        auto.max_consecutive_failures
        if auto is not None
        else MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
    )
    if consecutive >= max_failures:
        logger.debug(
            f"Auto-compact circuit breaker: {consecutive} consecutive failures, skipping"
        )
        return {"was_compacted": False, "consecutive_failures": consecutive}

    if not should_auto_compact(messages, context_window, is_subagent, compact_cfg):
        return {"was_compacted": False, "consecutive_failures": consecutive}

    threshold = get_auto_compact_threshold(context_window, compact_cfg)

    # --- Strategy 1: Session memory compact (no LLM call) ---
    sm_result = try_session_memory_compact(messages, auto_compact_threshold=threshold)
    if sm_result:
        # On SM compact success, reset lastSummarizedMessageUuid —
        # SM compact prunes messages and the old UUID won't exist anymore
        set_last_summarized_message_uuid(None)
        run_post_compact_cleanup()
        return {
            "was_compacted": True,
            "compaction_result": sm_result,
            "consecutive_failures": 0,
        }

    # --- Strategy 2: Full LLM compact ---
    if llm_client is None or model is None:
        logger.warning(
            "Auto-compact: no LLM client/model provided, cannot run full compact"
        )
        return {"was_compacted": False, "consecutive_failures": consecutive}

    try:
        from .compact_conversation import compact_conversation

        result = await compact_conversation(
            messages=messages,
            llm_client=llm_client,
            model=model,
            custom_instructions=custom_instructions,
            suppress_followup_questions=True,
            is_auto_compact=True,
            max_output_tokens=(
                compact_cfg.summary_max_output_tokens
                if compact_cfg is not None
                else MAX_OUTPUT_TOKENS_FOR_SUMMARY
            ),
            compact_cfg=compact_cfg,
        )

        if not result.success():
            logger.info(
                "Auto-compact full compact skipped: %s",
                result.user_display_message or result.error_message or result.status.value,
            )
            return {
                "was_compacted": False,
                "compaction_result": result,
                "consecutive_failures": 0,
            }

        # Full compact replaces all messages — reset UUID tracking
        set_last_summarized_message_uuid(None)
        run_post_compact_cleanup()

        return {
            "was_compacted": True,
            "compaction_result": result,
            "consecutive_failures": 0,
        }

    except Exception as e:
        logger.error(f"Auto-compact full compact failed: {e}")
        next_failures = consecutive + 1
        if next_failures >= max_failures:
            logger.warning(
                f"Auto-compact circuit breaker tripped after {next_failures} consecutive failures"
            )
        return {
            "was_compacted": False,
            "consecutive_failures": next_failures,
        }


__all__ = [
    "AutoCompactTrackingState",
    "AUTOCOMPACT_BUFFER_TOKENS",
    "WARNING_THRESHOLD_BUFFER_TOKENS",
    "ERROR_THRESHOLD_BUFFER_TOKENS",
    "MANUAL_COMPACT_BUFFER_TOKENS",
    "MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES",
    "get_effective_context_window_size",
    "get_auto_compact_threshold",
    "calculate_token_warning_state",
    "is_auto_compact_enabled",
    "should_auto_compact",
    "auto_compact_if_needed",
]
