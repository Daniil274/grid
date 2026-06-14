"""
Time-based microcompact for Grid.

Mirrors Claude Code's maybeTimeBasedMicrocompact():

When the gap since the last assistant message exceeds GAP_THRESHOLD_MINUTES
(default 60 min — the server-side prompt cache TTL), content-clear all but
the most recent KEEP_RECENT compactable tool results.

The server cache has expired anyway, so shrinking old tool result content
before the request reduces what gets rewritten.

Key differences from the previous implementation:
- No global ToolResultState objects or time-based in-state tracking.
- Uses tool_use IDs from assistant messages to identify which tool_results
  to clear in user messages (matches CC's architecture exactly).
- Clearing replaces the content with TIME_BASED_MC_CLEARED_MESSAGE string.
- reset_microcompact_state() is a no-op (no persistent state to reset).
"""

from datetime import datetime
from typing import List, Optional, Set, Iterable
from utils.logger import Logger

from .base import CompactMessage

logger = Logger.get_logger("compact.micro")

# Marker for cleared tool results (matches CC's TIME_BASED_MC_CLEARED_MESSAGE)
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

# Gap threshold: 60 minutes (server cache TTL is ~1 hour)
GAP_THRESHOLD_MINUTES: float = 60.0

# Keep this many most-recent compactable tool results
KEEP_RECENT: int = 5

# Tools whose results can be safely cleared (mirrors CC's COMPACTABLE_TOOLS)
COMPACTABLE_TOOLS: Set[str] = {
    # File reading
    "read_file",
    "read_text_file",
    "read_media_file",
    "read_multiple_files",
    # Directory listing
    "list_directory",
    "list_directory_with_sizes",
    "directory_tree",
    "get_file_info",
    # Search
    "search_files",
    "grep",
    "glob",
    # Shell commands (large output)
    "execute_command",
    "bash",
    # Web
    "web_fetch",
    "web_search",
    # Git (large diffs)
    "git_diff",
    "git_log",
    # File editing (CC clears inputs for these)
    "file_edit",
    "file_write",
    "edit_file",
    "write_file",
}


def _get_micro_cfg(compact_cfg):
    return getattr(compact_cfg, "micro", None) if compact_cfg is not None else None


def _get_compactable_tools(compact_cfg=None) -> Set[str]:
    micro_cfg = _get_micro_cfg(compact_cfg)
    if micro_cfg is not None and getattr(micro_cfg, "compactable_tools", None):
        return set(micro_cfg.compactable_tools)
    return COMPACTABLE_TOOLS


def collect_compactable_tool_ids(
    messages: List[CompactMessage],
    compactable_tools: Optional[Iterable[str]] = None,
) -> List[str]:
    """
    Walk assistant messages and collect tool_use IDs whose tool name is in
    COMPACTABLE_TOOLS, in encounter order.

    Mirrors CC's collectCompactableToolIds().
    """
    tool_names = set(compactable_tools or COMPACTABLE_TOOLS)
    ids: List[str] = []
    for message in messages:
        if message.role != "assistant":
            continue

        content = message.content
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in tool_names:
                bid = block.get("id")
                if bid:
                    ids.append(bid)

    # Also check the tool_uses field (alternative storage)
    seen = set(ids)
    for message in messages:
        if message.role != "assistant":
            continue
        for tu in message.tool_uses:
            if tu.get("name") in tool_names:
                tid = tu.get("id")
                if tid and tid not in seen:
                    ids.append(tid)
                    seen.add(tid)

    return ids


def evaluate_time_based_trigger(
    messages: List[CompactMessage],
    gap_threshold_minutes: float = GAP_THRESHOLD_MINUTES,
) -> Optional[float]:
    """
    Check whether the time-based trigger should fire.

    Returns the gap in minutes if the trigger fires, or None if it doesn't.
    Mirrors CC's evaluateTimeBasedTrigger() (simplified — no querySource check).
    """
    last_assistant = next(
        (m for m in reversed(messages) if m.role == "assistant"), None
    )
    if not last_assistant or not last_assistant.timestamp:
        return None

    gap_minutes = (datetime.now() - last_assistant.timestamp).total_seconds() / 60.0
    if not (gap_minutes == gap_minutes) or gap_minutes < gap_threshold_minutes:  # NaN check
        return None

    return gap_minutes


def microcompact_messages(
    messages: List[CompactMessage],
    gap_threshold_minutes: float = GAP_THRESHOLD_MINUTES,
    keep_recent: int = KEEP_RECENT,
    compact_cfg=None,
) -> dict:
    """
    Perform time-based microcompact on a message list.

    Returns a dict with key "messages" (the processed list).
    When the trigger doesn't fire, messages are returned unchanged.

    Mirrors CC's microcompactMessages() / maybeTimeBasedMicrocompact() flow.

    Args:
        messages: Current conversation messages.
        gap_threshold_minutes: Override for the gap threshold.
        keep_recent: Number of most-recent compactable results to preserve.

    Returns:
        {"messages": List[CompactMessage]}
    """
    micro_cfg = _get_micro_cfg(compact_cfg)
    if compact_cfg is not None and getattr(compact_cfg, "enabled", True) is False:
        return {"messages": messages}
    if micro_cfg is not None:
        if not micro_cfg.enabled:
            return {"messages": messages}
        gap_threshold_minutes = micro_cfg.gap_threshold_minutes
        keep_recent = micro_cfg.preserve_last_n

    gap = evaluate_time_based_trigger(messages, gap_threshold_minutes=gap_threshold_minutes)
    if gap is None:
        return {"messages": messages}

    compactable_ids = collect_compactable_tool_ids(
        messages,
        compactable_tools=_get_compactable_tools(compact_cfg),
    )
    if not compactable_ids:
        return {"messages": messages}

    # Keep the last `keep_recent` IDs; clear the rest
    keep_n = max(1, keep_recent)  # Always keep at least 1 (CC: floor at 1)
    keep_set = set(compactable_ids[-keep_n:])
    clear_set = set(cid for cid in compactable_ids if cid not in keep_set)

    if not clear_set:
        return {"messages": messages}

    tokens_saved = 0
    result: List[CompactMessage] = []

    for message in messages:
        if message.role != "user":
            result.append(message)
            continue

        content = message.content
        if not isinstance(content, list):
            result.append(message)
            continue

        new_content = []
        touched = False

        for block in content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue

            if (
                block.get("type") == "tool_result"
                and block.get("tool_use_id") in clear_set
                and block.get("content") != TIME_BASED_MC_CLEARED_MESSAGE
            ):
                # Estimate tokens saved
                from .utils import calculate_tool_result_tokens
                tokens_saved += calculate_tool_result_tokens(block)
                new_content.append({**block, "content": TIME_BASED_MC_CLEARED_MESSAGE})
                touched = True
            else:
                new_content.append(block)

        if touched:
            new_msg = CompactMessage(
                role=message.role,
                content=new_content,
                message_id=message.message_id,
                uuid=message.uuid,
                timestamp=message.timestamp,
                metadata=message.metadata.copy(),
                is_compact_summary=message.is_compact_summary,
                is_compact_boundary=message.is_compact_boundary,
                tool_uses=message.tool_uses.copy(),
                tool_results=message.tool_results.copy(),
            )
            result.append(new_msg)
        else:
            result.append(message)

    if tokens_saved == 0:
        return {"messages": messages}

    logger.info(
        f"[TIME-BASED MC] gap {gap:.0f}min > {gap_threshold_minutes}min, "
        f"cleared {len(clear_set)} tool results (~{tokens_saved} tokens), "
        f"kept last {len(keep_set)}"
    )

    return {"messages": result}


def reset_microcompact_state() -> None:
    """
    Reset microcompact state after compaction.

    The time-based microcompact has no persistent module-level state to reset
    (unlike the previous implementation). This function exists for API
    compatibility with post_compact.py which calls it after every compaction.
    """
    pass  # No-op: time-based MC is stateless


__all__ = [
    "TIME_BASED_MC_CLEARED_MESSAGE",
    "GAP_THRESHOLD_MINUTES",
    "KEEP_RECENT",
    "COMPACTABLE_TOOLS",
    "collect_compactable_tool_ids",
    "evaluate_time_based_trigger",
    "microcompact_messages",
    "reset_microcompact_state",
]
