"""
Message grouping by API round for compaction.

Mirrors Claude Code's groupMessagesByApiRound() exactly:
- Boundary fires when a NEW assistant response begins (different message_id).
- Streaming chunks from the same API response share a message_id → stay in one group.
- This guarantees tool_use/tool_result pairing: every tool_use is resolved
  before the next assistant turn, so the boundary is an API-safe split point.
"""

from typing import List, Optional
from datetime import datetime

from .base import CompactMessage, CompactGroup


def group_messages_by_api_round(messages: List[CompactMessage]) -> List[CompactGroup]:
    """
    Group messages at API-round boundaries.

    A new group starts when an assistant message has a different message_id
    from the previous assistant. This is the sole boundary gate — streaming
    chunks sharing the same message_id stay in the same group.

    Args:
        messages: Flat list of CompactMessage objects.

    Returns:
        List of CompactGroup objects, one per API round-trip.
    """
    groups: List[CompactGroup] = []
    current: List[CompactMessage] = []
    last_assistant_id: Optional[str] = None

    for msg in messages:
        if (
            msg.role == "assistant"
            and msg.message_id != last_assistant_id
            and current
        ):
            groups.append(_make_group(current, last_assistant_id))
            current = [msg]
        else:
            current.append(msg)

        if msg.role == "assistant":
            last_assistant_id = msg.message_id

    if current:
        groups.append(_make_group(current, last_assistant_id))

    return groups


def _make_group(
    messages: List[CompactMessage],
    assistant_id: Optional[str],
) -> CompactGroup:
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    for msg in messages:
        if msg.timestamp:
            if start_time is None or msg.timestamp < start_time:
                start_time = msg.timestamp
            if end_time is None or msg.timestamp > end_time:
                end_time = msg.timestamp
    return CompactGroup(
        messages=messages,
        assistant_id=assistant_id,
        start_time=start_time,
        end_time=end_time,
    )


def calculate_group_tokens(group: CompactGroup, estimate_func) -> None:
    """
    Calculate and set token_count on a single CompactGroup in place.

    Args:
        group: A single CompactGroup (NOT a list).
        estimate_func: estimate_message_tokens(List[CompactMessage]) -> int
    """
    group.token_count = estimate_func(group.messages)


def truncate_head_for_ptl_retry(
    messages: List[CompactMessage],
    token_gap: Optional[int],
    ptl_retry_marker: str = "[earlier conversation truncated for compaction retry]",
) -> Optional[List[CompactMessage]]:
    """
    Drop oldest API-round groups until token_gap is covered.

    Mirrors CC's truncateHeadForPTLRetry():
    - Strips our own synthetic marker from a previous retry before grouping
      (so we don't accumulate markers).
    - Falls back to dropping 20% of groups when token_gap is unknown.
    - Returns None when nothing can be dropped without leaving an empty set.
    - Prepends a synthetic user marker when the result starts with an assistant
      message (the API requires the first message to have role=user).

    Args:
        messages: Current messages to compact.
        token_gap: Estimated tokens over the context limit (may be None).
        ptl_retry_marker: Synthetic marker content injected on previous retries.

    Returns:
        Truncated message list, or None if truncation is impossible.
    """
    from .utils import estimate_message_tokens

    # Strip synthetic marker from a previous retry
    input_msgs = messages
    if (
        messages
        and messages[0].role == "user"
        and messages[0].content == ptl_retry_marker
    ):
        input_msgs = messages[1:]

    groups = group_messages_by_api_round(input_msgs)
    if len(groups) < 2:
        return None

    if token_gap is not None:
        acc = 0
        drop_count = 0
        for group in groups:
            group_tokens = estimate_message_tokens(group.messages)
            acc += group_tokens
            drop_count += 1
            if acc >= token_gap:
                break
    else:
        drop_count = max(1, len(groups) // 5)  # 20%

    # Keep at least one group to summarize
    drop_count = min(drop_count, len(groups) - 1)
    if drop_count < 1:
        return None

    sliced: List[CompactMessage] = []
    for group in groups[drop_count:]:
        sliced.extend(group.messages)

    # API requires first message to be role=user
    if sliced and sliced[0].role == "assistant":
        import uuid as _uuid
        marker = CompactMessage(
            role="user",
            content=ptl_retry_marker,
            uuid=str(_uuid.uuid4()),
        )
        sliced = [marker] + sliced

    return sliced
