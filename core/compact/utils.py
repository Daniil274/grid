"""
Utility functions for the compact system.

Token estimation matches Claude Code's estimateMessageTokens():
- text blocks: rough_count (len // 4)
- images/documents: 2000 tokens flat
- thinking/redacted_thinking: rough_count of the thinking text
- tool_use: rough_count of name + JSON(input)
- tool_result: rough_count of content string/blocks
- Final result padded by 4/3 (conservative estimate)
"""

import json
import math
import re
from typing import List, Dict, Any, Optional
from datetime import datetime

from .base import CompactMessage

# Flat token estimate for image/document blocks (CC uses 2000)
IMAGE_MAX_TOKEN_SIZE = 2000

# Marker used by time-based microcompact to replace cleared tool results
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"


def rough_token_count(text: str) -> int:
    """
    Rough token estimate: len // 4.
    Mirrors CC's roughTokenCountEstimation (4 chars per token heuristic).
    """
    if not text:
        return 0
    return len(text) // 4


def calculate_tool_result_tokens(block: Dict[str, Any]) -> int:
    """
    Estimate tokens for a tool_result content block.
    Mirrors CC's calculateToolResultTokens().
    """
    content = block.get("content")
    if content is None:
        return 0
    if isinstance(content, str):
        return rough_token_count(content)
    if isinstance(content, list):
        total = 0
        for item in content:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            if t == "text":
                total += rough_token_count(item.get("text", ""))
            elif t in ("image", "document"):
                total += IMAGE_MAX_TOKEN_SIZE
        return total
    return rough_token_count(str(content))


def estimate_message_tokens(messages: List[CompactMessage]) -> int:
    """
    Estimate total tokens for a list of messages.

    Mirrors CC's estimateMessageTokens() exactly:
    - Skips system/attachment messages (only user and assistant)
    - Pads final result by 4/3 (conservative)
    - Handles all block types: text, tool_result, image, document,
      thinking, redacted_thinking, tool_use, and unknown blocks
    """
    total = 0

    for message in messages:
        if message.role not in ("user", "assistant"):
            continue

        content = message.content

        if isinstance(content, str):
            total += rough_token_count(content)
            continue

        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type", "")

            if t == "text":
                total += rough_token_count(block.get("text", ""))

            elif t == "tool_result":
                total += calculate_tool_result_tokens(block)

            elif t in ("image", "document"):
                total += IMAGE_MAX_TOKEN_SIZE

            elif t == "thinking":
                # Count only the thinking text, not wrapper/signature
                total += rough_token_count(block.get("thinking", ""))

            elif t == "redacted_thinking":
                total += rough_token_count(block.get("data", ""))

            elif t == "tool_use":
                # Count name + JSON(input), not id or wrapper
                try:
                    input_str = json.dumps(block.get("input") or {})
                except (TypeError, ValueError):
                    input_str = str(block.get("input", ""))
                total += rough_token_count(block.get("name", "") + input_str)

            else:
                # server_tool_use, web_search_tool_result, etc.
                try:
                    total += rough_token_count(json.dumps(block))
                except (TypeError, ValueError):
                    total += rough_token_count(str(block))

    # Pad by 4/3 (conservative — CC does this too)
    return math.ceil(total * (4 / 3))


# Backward-compatible alias used in agent_factory.py
estimate_messages_tokens = estimate_message_tokens


def strip_images_from_messages(messages: List[CompactMessage]) -> List[CompactMessage]:
    """
    Strip image and document blocks from messages before sending for compaction.

    Images are not needed for generating a conversation summary and can
    cause the compaction API call itself to hit the prompt-too-long limit.
    Replaces image/document blocks with text markers.
    Only user messages are processed (CC: "Only user messages contain images").
    """
    result = []
    for message in messages:
        if message.role != "user" or not message.has_images():
            result.append(message)
            continue

        new_content = _strip_images_from_content(message.content)
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
    return result


def _strip_images_from_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content

    new_blocks = []
    for block in content:
        if not isinstance(block, dict):
            new_blocks.append(block)
            continue
        t = block.get("type", "")
        if t == "image":
            new_blocks.append({"type": "text", "text": "[image]"})
        elif t == "document":
            new_blocks.append({"type": "text", "text": "[document]"})
        elif t == "tool_result" and isinstance(block.get("content"), list):
            stripped = _strip_images_from_content(block["content"])
            new_blocks.append({**block, "content": stripped})
        else:
            new_blocks.append(block)
    return new_blocks


def format_compact_summary(summary: str) -> str:
    """
    Strip the <analysis> scratchpad section and unwrap <summary> tags.
    Mirrors CC's getCompactUserSummaryMessage formatting.
    """
    # Strip analysis block
    formatted = re.sub(r"<analysis>[\s\S]*?</analysis>", "", summary)

    # Extract summary content
    match = re.search(r"<summary>([\s\S]*?)</summary>", formatted)
    if match:
        formatted = match.group(1).strip()

    # Collapse extra blank lines
    formatted = re.sub(r"\n{3,}", "\n\n", formatted)
    return formatted.strip()


def create_compact_boundary_message(
    trigger: str = "manual",
    pre_compact_token_count: int = 0,
) -> CompactMessage:
    """
    Create a system message marking a compact boundary.
    trigger: "auto" or "manual"
    """
    from datetime import datetime
    import uuid as _uuid
    return CompactMessage(
        role="system",
        content=f"[CONTEXT COMPACT BOUNDARY - {trigger}]",
        message_id=f"compact-boundary-{_uuid.uuid4().hex[:8]}",
        uuid=str(_uuid.uuid4()),
        timestamp=datetime.now(),
        metadata={
            "type": "compact_boundary",
            "trigger": trigger,
            "pre_compact_token_count": pre_compact_token_count,
        },
        is_compact_boundary=True,
    )


def is_compact_boundary_message(message: CompactMessage) -> bool:
    """Return True if this message is a compact boundary marker."""
    if message.is_compact_boundary:
        return True
    if message.role == "system" and isinstance(message.content, str):
        return "[CONTEXT COMPACT BOUNDARY" in message.content
    return False


def get_messages_after_compact_boundary(
    messages: List[CompactMessage],
) -> List[CompactMessage]:
    """
    Return messages after the last compact boundary marker.
    Used to avoid re-summarizing already-compacted content.
    Mirrors CC's getMessagesAfterCompactBoundary().
    """
    last_boundary = -1
    for i, msg in enumerate(messages):
        if is_compact_boundary_message(msg):
            last_boundary = i
    if last_boundary >= 0:
        return messages[last_boundary + 1:]
    return messages


def get_user_summary_message(
    summary: str,
    suppress_followup_questions: bool = False,
    transcript_path: Optional[str] = None,
    is_session_memory: bool = False,
) -> str:
    """
    Build the user-facing summary message injected after compaction.
    Mirrors CC's getCompactUserSummaryMessage().
    """
    formatted = format_compact_summary(summary)

    base = (
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier portion of the conversation.\n\n"
        + formatted
    )

    if transcript_path:
        base += (
            f"\n\nIf you need specific details from before compaction "
            f"(like exact code snippets, error messages, or content you generated), "
            f"read the full transcript at: {transcript_path}"
        )

    if is_session_memory:
        base += "\n\nRecent messages are preserved verbatim."

    if suppress_followup_questions:
        base += (
            "\n\nContinue the conversation from where it left off without asking the user "
            "any further questions. Resume directly — do not acknowledge the summary, "
            "do not recap what was happening, do not preface with \"I'll continue\" or similar. "
            "Pick up the last task as if the break never happened."
        )

    return base


# ---------------------------------------------------------------------------
# Backward-compatible aliases for callers that use the old function names
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """
    Backward-compatible alias: estimate tokens for a plain string.
    Callers in post_compact / session_memory_compact use this signature.
    """
    return rough_token_count(text)


def get_message_text(message: CompactMessage) -> str:
    """Backward-compatible alias for CompactMessage.get_text()."""
    return message.get_text()


def create_user_summary_message(
    summary: str,
    recent_messages_preserved: bool = False,
    suppress_followup: bool = False,
    transcript_path: Optional[str] = None,
) -> str:
    """
    Backward-compatible alias for get_user_summary_message().
    Maps old parameter names to the new signature.
    """
    return get_user_summary_message(
        summary=summary,
        suppress_followup_questions=suppress_followup,
        transcript_path=transcript_path,
        is_session_memory=recent_messages_preserved,
    )
