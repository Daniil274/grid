"""
LLM-based conversation compaction.

Mirrors Claude Code's compactConversation():
- Strips images from messages before sending to LLM.
- Calls LLM with the compact prompt to generate a structured summary.
- Retries on prompt-too-long by truncating the oldest API-round groups
  (up to MAX_PTL_RETRIES times). Mirrors CC's truncateHeadForPTLRetry loop.
- Returns a CompactionResult with boundary marker + summary message.

The LLM call uses the OpenAI-compatible async client (AsyncOpenAI or compatible),
passed as a parameter — no module-level global state.
"""

import logging
import re
from datetime import datetime
from typing import List, Optional, Any
import uuid as _uuid

from .base import CompactMessage, CompactionResult, CompactionStatus, CompactionStrategy
from .utils import (
    strip_images_from_messages,
    estimate_message_tokens,
    create_compact_boundary_message,
    get_user_summary_message,
)
from .grouping import truncate_head_for_ptl_retry
from .prompts import get_compact_prompt, get_partial_compact_prompt
from .micro_compact import microcompact_messages

logger = logging.getLogger("compact.conversation")

# Maximum retries when the compact request itself hits prompt-too-long
MAX_PTL_RETRIES = 3

# Marker prefix returned by the API when the request was too long
PROMPT_TOO_LONG_ERROR_MESSAGE = "API Error: prompt too long"

ERROR_MESSAGE_NOT_ENOUGH_MESSAGES = "Not enough messages to compact."
ERROR_MESSAGE_PROMPT_TOO_LONG = (
    "Conversation too long to compact. Try removing some messages and compacting again."
)
MAX_SAFE_SUMMARY_OUTPUT_TOKENS = 20_000


def _is_prompt_too_long_response(text: str) -> bool:
    """Check if the summary text indicates a prompt-too-long error from the API."""
    if not text:
        return False
    return text.startswith(PROMPT_TOO_LONG_ERROR_MESSAGE) or (
        "prompt_too_long" in text.lower() and len(text) < 300
    )


def _clamp_summary_output_tokens(value: int) -> int:
    """Keep compact summaries below provider/account max_tokens ceilings."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = MAX_SAFE_SUMMARY_OUTPUT_TOKENS
    return max(1_000, min(parsed, MAX_SAFE_SUMMARY_OUTPUT_TOKENS))


def _extract_token_gap_from_error(error: Exception) -> Optional[int]:
    """
    Extract the token gap from a context-length error message.
    Mirrors CC's getPromptTooLongTokenGap().
    """
    text = str(error)
    # "this model's context window is 200000 tokens, but 201234 were provided"
    m = re.search(r"context window is (\d+).*?but (\d+)", text)
    if m:
        return int(m.group(2)) - int(m.group(1))
    # "reduced by N tokens"
    m = re.search(r"reduced by (\d+)", text)
    if m:
        return int(m.group(1))
    # "exceeds maximum of N"
    m = re.search(r"exceeds maximum of (\d+)", text)
    if m:
        return int(m.group(1)) // 10  # guess 10% over
    return None


async def _call_llm_for_summary(
    messages: List[CompactMessage],
    compact_prompt: str,
    llm_client: Any,
    model: str,
    max_tokens: int = 20_000,
) -> str:
    """
    Call the LLM to generate a conversation summary.

    Converts CompactMessages to OpenAI-format dicts, appends the compact prompt
    as a user message, and returns the assistant's response text.
    """
    # Build OpenAI-format message list
    api_messages = []
    for msg in messages:
        if msg.role == "system":
            api_messages.append({"role": "system", "content": msg.get_text() or ""})
            continue

        content = msg.content
        # Simplify to string for the summarizer (it doesn't need to execute tools)
        if isinstance(content, list):
            text = msg.get_text()
            api_messages.append({"role": msg.role, "content": text or ""})
        else:
            api_messages.append({"role": msg.role, "content": content or ""})

    # Append the compact prompt as a user request
    api_messages.append({"role": "user", "content": compact_prompt})

    response = await llm_client.chat.completions.create(
        model=model,
        messages=api_messages,
        max_tokens=max_tokens,
        temperature=0.0,
    )

    return response.choices[0].message.content or ""


async def compact_conversation(
    messages: List[CompactMessage],
    llm_client: Any,
    model: str,
    custom_instructions: Optional[str] = None,
    suppress_followup_questions: bool = True,
    is_auto_compact: bool = False,
    max_output_tokens: int = 20_000,
    compact_cfg: Optional[Any] = None,
) -> CompactionResult:
    """
    Compact a conversation by generating an LLM summary of older messages.

    Mirrors CC's compactConversation():
    1. Strip images (they inflate tokens without helping the summarizer).
    2. Run microcompact first to pre-shrink tool results.
    3. Build compact prompt and call the LLM.
    4. On prompt-too-long: truncate the oldest groups and retry (up to MAX_PTL_RETRIES).
    5. Return CompactionResult with boundary marker + summary + empty messages_to_keep.

    Args:
        messages: All messages in the current conversation.
        llm_client: Async OpenAI-compatible client (AsyncOpenAI instance).
        model: Model identifier string.
        custom_instructions: Optional user-supplied instructions for the summary.
        suppress_followup_questions: Whether the summary should tell the model
            to resume without asking questions (True for auto-compact).
        is_auto_compact: Whether this was triggered automatically.
        max_output_tokens: Maximum tokens for the LLM summary response.

    Returns:
        CompactionResult on success.

    Raises:
        ValueError: If messages is empty or summary generation fails.
        RuntimeError: If prompt-too-long persists after all retries.
    """
    if not messages:
        raise ValueError(ERROR_MESSAGE_NOT_ENOUGH_MESSAGES)

    tokens_before = estimate_message_tokens(messages)

    # 1. Strip images
    messages = strip_images_from_messages(messages)

    # 2. Run microcompact to pre-shrink (silently — if gap < threshold, no-op)
    if compact_cfg is not None and getattr(compact_cfg, "enabled", True) is False:
        raise ValueError("compact_conversation: compact system is disabled in config")

    if compact_cfg is not None:
        max_output_tokens = getattr(compact_cfg, "summary_max_output_tokens", max_output_tokens)
    max_output_tokens = _clamp_summary_output_tokens(max_output_tokens)

    micro_result = microcompact_messages(messages, compact_cfg=compact_cfg)
    messages_to_summarize = micro_result["messages"]

    # 3. Build compact prompt
    compact_prompt = get_compact_prompt(custom_instructions)

    # 4. PTL retry loop
    ptl_attempts = 0
    summary: Optional[str] = None

    while True:
        try:
            summary = await _call_llm_for_summary(
                messages_to_summarize,
                compact_prompt,
                llm_client,
                model,
                max_tokens=max_output_tokens,
            )
        except Exception as e:
            # Check if this is a context-length error from the API layer
            error_str = str(e).lower()
            is_ptl = any(
                kw in error_str for kw in (
                    "prompt_too_long", "context_length_exceeded",
                    "maximum context", "token limit",
                )
            )
            if not is_ptl:
                raise

            ptl_attempts += 1
            if ptl_attempts > MAX_PTL_RETRIES:
                raise RuntimeError(ERROR_MESSAGE_PROMPT_TOO_LONG) from e

            token_gap = _extract_token_gap_from_error(e)
            truncated = truncate_head_for_ptl_retry(messages_to_summarize, token_gap)
            if truncated is None:
                raise RuntimeError(ERROR_MESSAGE_PROMPT_TOO_LONG) from e

            logger.info(
                f"compact_conversation: PTL retry {ptl_attempts}, "
                f"dropped {len(messages_to_summarize) - len(truncated)} messages"
            )
            messages_to_summarize = truncated
            continue

        # Check if the *response itself* signals PTL (CC's pattern)
        if _is_prompt_too_long_response(summary or ""):
            ptl_attempts += 1
            if ptl_attempts > MAX_PTL_RETRIES:
                raise RuntimeError(ERROR_MESSAGE_PROMPT_TOO_LONG)

            truncated = truncate_head_for_ptl_retry(messages_to_summarize, None)
            if truncated is None:
                raise RuntimeError(ERROR_MESSAGE_PROMPT_TOO_LONG)

            logger.info(
                f"compact_conversation: PTL response retry {ptl_attempts}"
            )
            messages_to_summarize = truncated
            continue

        break  # Success

    if not summary:
        raise ValueError("compact_conversation: LLM returned empty summary")

    # 5. Build result
    trigger = "auto" if is_auto_compact else "manual"
    boundary = create_compact_boundary_message(trigger, tokens_before)

    summary_content = get_user_summary_message(
        summary,
        suppress_followup_questions=suppress_followup_questions,
    )
    summary_msg = CompactMessage(
        role="user",
        content=summary_content,
        message_id=f"compact-summary-{_uuid.uuid4().hex[:8]}",
        uuid=str(_uuid.uuid4()),
        timestamp=datetime.now(),
        metadata={"type": "compact_summary"},
        is_compact_summary=True,
    )

    compacted = [boundary, summary_msg]
    tokens_after = estimate_message_tokens(compacted)

    if tokens_after >= tokens_before:
        logger.info(
            f"compact_conversation: {trigger}, skipped because result is not smaller "
            f"({tokens_before} -> {tokens_after})"
        )
        return CompactionResult(
            status=CompactionStatus.ABORTED,
            strategy=CompactionStrategy.FULL,
            compacted_messages=messages,
            boundary_marker=None,
            summary_messages=[],
            messages_to_keep=messages,
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            tokens_saved=0,
            error_message="Compaction would increase token usage",
            user_display_message="Compaction skipped because the summary was not smaller than the current context",
        )

    logger.info(
        f"compact_conversation: {trigger}, "
        f"tokens {tokens_before} → {tokens_after} "
        f"(saved {tokens_before - tokens_after})"
    )

    return CompactionResult(
        status=CompactionStatus.SUCCESS,
        strategy=CompactionStrategy.FULL,
        compacted_messages=compacted,
        boundary_marker=boundary,
        summary_messages=[summary_msg],
        messages_to_keep=[],
        summary=summary,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        tokens_saved=tokens_before - tokens_after,
        user_display_message=f"Compacted conversation, saved {tokens_before - tokens_after} tokens",
    )


__all__ = [
    "compact_conversation",
    "ERROR_MESSAGE_NOT_ENOUGH_MESSAGES",
    "ERROR_MESSAGE_PROMPT_TOO_LONG",
    "MAX_PTL_RETRIES",
]
