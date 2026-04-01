"""
Compact System for Grid - Context Management and Compaction.

Aligned with Claude Code's compact architecture:
- base: Core types (CompactMessage, CompactionResult, TokenWarningState, etc.)
- utils: Token estimation, message stripping, boundary helpers
- grouping: API-round grouping (boundary on assistant message_id)
- micro_compact: Time-based tool result clearing (gap > 60min)
- compact_conversation: LLM-based full compaction with PTL retry
- session_memory_compact: Session-memory-based compact (no LLM call)
- auto_compact: Threshold detection + circuit breaker + strategy selection
- reactive_compact: Head-truncation on context-overflow errors
- post_compact: Cache clearing + file/skill restoration
- prompts: LLM prompts for full compaction
"""

# Base types
from .base import (
    CompactionResult,
    CompactionConfig,
    CompactionStrategy,
    CompactionStatus,
    CompactionDirection,
    TokenWarningState,
    CompactMessage,
    CompactGroup,
)

# Utils
from .utils import (
    estimate_message_tokens,
    estimate_messages_tokens,   # alias for agent_factory compatibility
    strip_images_from_messages,
    rough_token_count,
    format_compact_summary,
    create_compact_boundary_message,
    is_compact_boundary_message,
    get_messages_after_compact_boundary,
    get_user_summary_message,
    TIME_BASED_MC_CLEARED_MESSAGE,
)

# Grouping
from .grouping import (
    group_messages_by_api_round,
    calculate_group_tokens,
    truncate_head_for_ptl_retry,
)

# Microcompact (time-based)
from .micro_compact import (
    TIME_BASED_MC_CLEARED_MESSAGE as CLEARED_RESULT_MARKER,  # backward compat
    COMPACTABLE_TOOLS,
    GAP_THRESHOLD_MINUTES,
    KEEP_RECENT,
    collect_compactable_tool_ids,
    evaluate_time_based_trigger,
    microcompact_messages,
    reset_microcompact_state,
)

# Session memory compact
from .session_memory_compact import (
    SessionMemoryCompactConfig,
    set_session_memory_compact_config,
    get_session_memory_compact_config,
    set_session_memory_content,
    get_session_memory_content,
    get_last_summarized_message_uuid,
    set_last_summarized_message_uuid,
    reset_session_memory_state,
    adjust_index_to_preserve_api_invariants,
    calculate_messages_to_keep_index,
    try_session_memory_compact,
)

# Auto compact
from .auto_compact import (
    AutoCompactTrackingState,
    AUTOCOMPACT_BUFFER_TOKENS,
    WARNING_THRESHOLD_BUFFER_TOKENS,
    ERROR_THRESHOLD_BUFFER_TOKENS,
    MANUAL_COMPACT_BUFFER_TOKENS,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    get_effective_context_window_size,
    get_auto_compact_threshold,
    calculate_token_warning_state,
    is_auto_compact_enabled,
    should_auto_compact,
    auto_compact_if_needed,
)

# Reactive compact
from .reactive_compact import (
    ReactiveCompactStatus,
    ReactiveCompactResult,
    is_prompt_too_long_error,
    get_token_gap_from_error,
    reactive_compact,
    reactive_compact_on_prompt_too_long,
    is_reactive_mode,
)

# Post compact
from .post_compact import (
    PostCompactState,
    FileRestoreInfo,
    SkillRestoreInfo,
    get_post_compact_state,
    reset_post_compact_state,
    record_file_read,
    record_skill_invocation,
    run_post_compact_cleanup,
    create_post_compact_file_attachments,
    create_skill_attachments,
    mark_post_compaction,
)

# LLM compact conversation
from .compact_conversation import (
    compact_conversation,
    ERROR_MESSAGE_NOT_ENOUGH_MESSAGES,
    ERROR_MESSAGE_PROMPT_TOO_LONG,
)

# Context for use by agent_factory (kept for backward compatibility)
# CompactContext is now just AutoCompactTrackingState — agent_factory should
# be updated to use AutoCompactTrackingState directly.
from .session_memory_compact import SessionMemoryCompactConfig as CompactContext

__all__ = [
    # Base
    "CompactionResult", "CompactionConfig", "CompactionStrategy",
    "CompactionStatus", "CompactionDirection", "TokenWarningState",
    "CompactMessage", "CompactGroup",
    # Utils
    "estimate_message_tokens", "estimate_messages_tokens",
    "strip_images_from_messages", "rough_token_count",
    "format_compact_summary", "create_compact_boundary_message",
    "is_compact_boundary_message", "get_messages_after_compact_boundary",
    "get_user_summary_message", "TIME_BASED_MC_CLEARED_MESSAGE",
    # Grouping
    "group_messages_by_api_round", "calculate_group_tokens",
    "truncate_head_for_ptl_retry",
    # Microcompact
    "CLEARED_RESULT_MARKER", "COMPACTABLE_TOOLS",
    "GAP_THRESHOLD_MINUTES", "KEEP_RECENT",
    "collect_compactable_tool_ids", "evaluate_time_based_trigger",
    "microcompact_messages", "reset_microcompact_state",
    # Session memory
    "SessionMemoryCompactConfig",
    "set_session_memory_compact_config", "get_session_memory_compact_config",
    "set_session_memory_content", "get_session_memory_content",
    "get_last_summarized_message_uuid", "set_last_summarized_message_uuid",
    "reset_session_memory_state",
    "adjust_index_to_preserve_api_invariants", "calculate_messages_to_keep_index",
    "try_session_memory_compact",
    # Auto compact
    "AutoCompactTrackingState",
    "AUTOCOMPACT_BUFFER_TOKENS", "WARNING_THRESHOLD_BUFFER_TOKENS",
    "ERROR_THRESHOLD_BUFFER_TOKENS", "MANUAL_COMPACT_BUFFER_TOKENS",
    "MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES",
    "get_effective_context_window_size", "get_auto_compact_threshold",
    "calculate_token_warning_state", "is_auto_compact_enabled",
    "should_auto_compact", "auto_compact_if_needed",
    # Reactive
    "ReactiveCompactStatus", "ReactiveCompactResult",
    "is_prompt_too_long_error", "get_token_gap_from_error",
    "reactive_compact", "reactive_compact_on_prompt_too_long", "is_reactive_mode",
    # Post compact
    "PostCompactState", "FileRestoreInfo", "SkillRestoreInfo",
    "get_post_compact_state", "reset_post_compact_state",
    "record_file_read", "record_skill_invocation",
    "run_post_compact_cleanup", "create_post_compact_file_attachments",
    "create_skill_attachments", "mark_post_compaction",
    # LLM compact
    "compact_conversation",
    "ERROR_MESSAGE_NOT_ENOUGH_MESSAGES", "ERROR_MESSAGE_PROMPT_TOO_LONG",
    # Compat
    "CompactContext",
]
