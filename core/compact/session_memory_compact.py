"""
Session Memory Compact for Grid.

Mirrors Claude Code's session memory compaction:

Instead of calling the LLM to summarize (which costs tokens and time),
this path uses pre-existing session memory content as the summary and
preserves a "tail" of recent messages verbatim.

Key algorithm (calculateMessagesToKeepIndex):
1. Start from the message after lastSummarizedMessageUuid.
2. Expand backwards until we have >= sm_min_tokens tokens AND
   >= sm_min_text_block_messages messages with text content.
3. Hard-cap at sm_max_tokens (stop expanding if exceeded).
4. Adjust the start index to avoid splitting tool_use/tool_result pairs.

If no session memory content is available, returns None → caller falls
back to full LLM compact (compact_conversation).

Session memory content in Grid is stored in-memory via set_session_memory_content()
and cleared by reset_session_memory_state(). An external process (background
extraction or manual call) is responsible for populating it.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Set
import uuid as _uuid
from utils.logger import Logger

from .base import (
    CompactMessage,
    CompactionResult,
    CompactionStatus,
    CompactionStrategy,
)
from .utils import (
    estimate_message_tokens,
    create_compact_boundary_message,
    get_user_summary_message,
    is_compact_boundary_message,
)

logger = Logger.get_logger("compact.session_memory")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SessionMemoryCompactConfig:
    """
    Thresholds for session memory compaction.
    Mirrors CC's DEFAULT_SM_COMPACT_CONFIG.
    """
    min_tokens: int = 10_000
    min_text_block_messages: int = 5
    max_tokens: int = 40_000


_sm_config: SessionMemoryCompactConfig = SessionMemoryCompactConfig()


def set_session_memory_compact_config(config: SessionMemoryCompactConfig) -> None:
    global _sm_config
    _sm_config = config


def get_session_memory_compact_config() -> SessionMemoryCompactConfig:
    return _sm_config


# ---------------------------------------------------------------------------
# Session memory content store
# ---------------------------------------------------------------------------

_session_memory_content: Optional[str] = None
_last_summarized_message_uuid: Optional[str] = None


def set_session_memory_content(content: Optional[str]) -> None:
    """Store session memory content (call this from background extraction)."""
    global _session_memory_content
    _session_memory_content = content


def get_session_memory_content() -> Optional[str]:
    """Return current session memory content."""
    return _session_memory_content


def get_last_summarized_message_uuid() -> Optional[str]:
    """Return UUID of the last message that has already been summarized."""
    return _last_summarized_message_uuid


def set_last_summarized_message_uuid(uuid: Optional[str]) -> None:
    """
    Set the UUID of the last summarized message.
    Call this after every compaction (full or session-memory).
    Pass None to reset (e.g., after legacy full compact that replaces all messages).
    """
    global _last_summarized_message_uuid
    _last_summarized_message_uuid = uuid


def reset_session_memory_state() -> None:
    """Reset all session memory state (call after clearing context)."""
    global _session_memory_content, _last_summarized_message_uuid
    _session_memory_content = None
    _last_summarized_message_uuid = None


# ---------------------------------------------------------------------------
# Core algorithm helpers
# ---------------------------------------------------------------------------

def adjust_index_to_preserve_api_invariants(
    messages: List[CompactMessage],
    start_index: int,
) -> int:
    """
    Adjust start_index backwards to avoid splitting tool_use/tool_result pairs
    or thinking blocks that share a message_id with kept assistant messages.

    Mirrors CC's adjustIndexToPreserveAPIInvariants() exactly.

    Step 1: Collect all tool_result IDs from the kept range.
            Walk backwards to find assistant messages with matching tool_use IDs
            that are NOT yet in the kept range — include those too.

    Step 2: Collect all assistant message_ids in the kept range.
            Walk backwards for assistant messages sharing those IDs
            (thinking blocks split across chunks) — include those.
    """
    if start_index <= 0 or start_index >= len(messages):
        return start_index

    adjusted = start_index

    # Step 1: tool_use / tool_result pairing
    all_tool_result_ids: List[str] = []
    for i in range(start_index, len(messages)):
        all_tool_result_ids.extend(messages[i].get_tool_result_ids())

    if all_tool_result_ids:
        # IDs already in the kept range
        in_kept: Set[str] = set()
        for i in range(adjusted, len(messages)):
            in_kept.update(messages[i].get_tool_use_ids())

        needed = set(rid for rid in all_tool_result_ids if rid not in in_kept)

        i = adjusted - 1
        while i >= 0 and needed:
            msg = messages[i]
            if msg.role == "assistant":
                msg_ids = set(msg.get_tool_use_ids())
                if msg_ids & needed:
                    adjusted = i
                    needed -= msg_ids
            i -= 1

    # Step 2: thinking blocks sharing message_id
    kept_msg_ids: Set[str] = set()
    for i in range(adjusted, len(messages)):
        msg = messages[i]
        if msg.role == "assistant" and msg.message_id:
            kept_msg_ids.add(msg.message_id)

    for i in range(adjusted - 1, -1, -1):
        msg = messages[i]
        if (
            msg.role == "assistant"
            and msg.message_id
            and msg.message_id in kept_msg_ids
        ):
            adjusted = i

    return adjusted


def calculate_messages_to_keep_index(
    messages: List[CompactMessage],
    last_summarized_index: int,
) -> int:
    """
    Find the start index of messages to preserve after compaction.

    Mirrors CC's calculateMessagesToKeepIndex() exactly:
    - Starts from the message after last_summarized_index.
    - Counts tokens and text-block messages in the kept range.
    - Expands backwards until min thresholds are met or max cap hit.
    - Floors at the last compact boundary (don't cross it).
    - Adjusts for tool_use/tool_result pairing.

    Args:
        messages: Full message list.
        last_summarized_index: Index of the last message already summarized
            (i.e., already represented in session memory). -1 = not found.

    Returns:
        Start index of messages to keep (slice messages[start:] to get them).
    """
    config = _sm_config

    if not messages:
        return 0

    # Start from the message after the last summarized one.
    # If last_summarized_index == -1 (not found) or == len-1,
    # start with no messages kept (start_index = len(messages)).
    start_index = (
        last_summarized_index + 1
        if last_summarized_index >= 0
        else len(messages)
    )

    # Compute current totals from start_index to end
    total_tokens = 0
    text_count = 0
    for i in range(start_index, len(messages)):
        total_tokens += estimate_message_tokens([messages[i]])
        if messages[i].has_text_blocks():
            text_count += 1

    # Already at or over max cap
    if total_tokens >= config.max_tokens:
        return adjust_index_to_preserve_api_invariants(messages, start_index)

    # Already meets both minimums
    if total_tokens >= config.min_tokens and text_count >= config.min_text_block_messages:
        return adjust_index_to_preserve_api_invariants(messages, start_index)

    # Expand backwards. Floor at the last compact boundary (same as CC).
    floor_idx = 0
    for i in range(len(messages) - 1, -1, -1):
        if is_compact_boundary_message(messages[i]):
            floor_idx = i + 1
            break

    for i in range(start_index - 1, floor_idx - 1, -1):
        msg_tokens = estimate_message_tokens([messages[i]])
        total_tokens += msg_tokens
        if messages[i].has_text_blocks():
            text_count += 1
        start_index = i

        if total_tokens >= config.max_tokens:
            break
        if total_tokens >= config.min_tokens and text_count >= config.min_text_block_messages:
            break

    return adjust_index_to_preserve_api_invariants(messages, start_index)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def try_session_memory_compact(
    messages: List[CompactMessage],
    auto_compact_threshold: Optional[int] = None,
) -> Optional[CompactionResult]:
    """
    Attempt session memory compaction.

    Returns None if session memory compaction cannot be used (no content,
    no valid summarized message boundary, or post-compact would still exceed
    the threshold). The caller should fall back to full compact_conversation.

    Mirrors CC's trySessionMemoryCompaction() logic (sync version —
    Grid's session memory is stored in-memory, no async file I/O needed).

    Args:
        messages: Current conversation messages.
        auto_compact_threshold: When set, abort if post-compact tokens
            would still exceed this threshold (avoids pointless compaction).

    Returns:
        CompactionResult on success, None otherwise.
    """
    session_memory = get_session_memory_content()
    if not session_memory:
        logger.debug("Session memory compact: no session memory content available")
        return None

    last_uuid = get_last_summarized_message_uuid()

    try:
        if last_uuid:
            last_idx = next(
                (i for i, m in enumerate(messages) if m.uuid == last_uuid),
                -1,
            )
            if last_idx == -1:
                logger.debug(
                    "Session memory compact: lastSummarizedMessageUuid not found "
                    "in current messages — falling back to full compact"
                )
                return None
        else:
            # No known boundary: treat all messages as already summarized,
            # keep only the expansion result (like CC's "resumed session" case)
            last_idx = len(messages) - 1

        start_index = calculate_messages_to_keep_index(messages, last_idx)

        # Filter out old compact boundaries from the kept slice
        messages_to_keep = [
            m for m in messages[start_index:]
            if not is_compact_boundary_message(m)
        ]

        tokens_before = estimate_message_tokens(messages)
        trigger = "auto"
        boundary = create_compact_boundary_message(trigger, tokens_before)

        summary_content = get_user_summary_message(
            session_memory,
            suppress_followup_questions=True,
            is_session_memory=True,
        )
        summary_msg = CompactMessage(
            role="user",
            content=summary_content,
            message_id=f"sm-compact-{_uuid.uuid4().hex[:8]}",
            uuid=str(_uuid.uuid4()),
            timestamp=datetime.now(),
            metadata={"type": "compact_summary", "source": "session_memory"},
            is_compact_summary=True,
        )

        compacted = [boundary, summary_msg] + messages_to_keep
        tokens_after = estimate_message_tokens(compacted)

        # Abort if we'd still be over threshold
        if auto_compact_threshold is not None and tokens_after >= auto_compact_threshold:
            logger.debug(
                f"Session memory compact: post-compact {tokens_after} >= "
                f"threshold {auto_compact_threshold}, aborting"
            )
            return None

        logger.info(
            f"Session memory compact: tokens {tokens_before} → {tokens_after}, "
            f"kept {len(messages_to_keep)} messages"
        )

        return CompactionResult(
            status=CompactionStatus.SUCCESS,
            strategy=CompactionStrategy.SESSION_MEMORY,
            compacted_messages=compacted,
            boundary_marker=boundary,
            summary_messages=[summary_msg],
            messages_to_keep=messages_to_keep,
            summary=session_memory,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_saved=tokens_before - tokens_after,
            user_display_message=(
                f"Compacted using session memory, kept {len(messages_to_keep)} recent messages"
            ),
        )

    except Exception as e:
        logger.error(f"Session memory compact failed: {e}")
        return None


__all__ = [
    "SessionMemoryCompactConfig",
    "set_session_memory_compact_config",
    "get_session_memory_compact_config",
    "set_session_memory_content",
    "get_session_memory_content",
    "get_last_summarized_message_uuid",
    "set_last_summarized_message_uuid",
    "reset_session_memory_state",
    "adjust_index_to_preserve_api_invariants",
    "calculate_messages_to_keep_index",
    "try_session_memory_compact",
]
