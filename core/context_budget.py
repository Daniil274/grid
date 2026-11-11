"""How much of the model's context window a request takes, and keeping it inside.

What the model reads is the agent's SDK session - every message, tool call and
tool result, images included - plus the instructions and the tool schemas. The
stored chat (``context.json``) is only the text users see, a small part of it, so
every size here is measured on session items.

Grid keeps a request inside the window at two points, both driven by the
system's ``compact`` config and the model's ``context_window`` and ``max_tokens``
(the threshold leaves room for the answer: ``ModelProvider.compact_threshold``):

1. Before each model call (:func:`context_budget_filter`, a
   ``RunConfig.call_model_input_filter``): when the request would pass the
   auto-compact threshold, the outputs of the oldest calls of compactable tools
   (``compact.micro.compactable_tools``) are replaced in that request by a short
   note, oldest first, until it fits or only the newest
   ``compact.micro.preserve_last_n`` are left. Nothing is summarized and no model
   is called, so a long turn - sixty GUI steps that each return a UI tree - keeps
   running; the session still holds every original.
2. Before each turn (``AgentFactory.compact_session``): when the session itself is
   past the threshold, the compaction model summarizes it (see
   :func:`session_transcript`) and the session is replaced by the summary. The
   chat users see is left as it is.

Sizes are estimates: text at ``CHARS_PER_TOKEN`` characters per token and each
image at ``IMAGE_TOKENS``. The thresholds keep a buffer for the error.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, List, Optional, Set

from core.compact.base import CompactMessage
from core.context import COMPACTED_SUMMARY_NOTE, strip_service_context_footer
from core.modalities import part_modality

CHARS_PER_TOKEN = 4
#: A screenshot-sized image costs about this much with common vision models.
IMAGE_TOKENS = 1500
IMAGE_PART_TYPES = frozenset({"input_image", "image_url"})
#: What other media cost, roughly: a few minutes of audio, a short clip, some pages.
#: Counted this way, not by the length of their base64 data.
MEDIA_TOKENS = {"image": IMAGE_TOKENS, "audio": 4000, "video": 10000, "pdf": 4000}

CLEARED_NOTE = (
    "[Output of {tool} cleared to keep the context within the model's window. "
    "Run it again if you need it.]"
)

# The summarizer reads tool calls and results clipped to these lengths: enough
# to know what was done and found, not whole files.
_TRANSCRIPT_ARGUMENT_CHARS = 1500
_TRANSCRIPT_OUTPUT_CHARS = 3000

# What a turn puts in front of the user's own words when tools ran
# automatically first (factory.auto_run) and other agents were talked to
# meanwhile (factory.turns._missed_exchange): injected tool results, the missed
# exchange, and the header that separates them from the request.
AUTO_RUN_RESULT_MARKER = "=== AUTO-RUN TOOL RESULT"
CURRENT_REQUEST_HEADER = "[Current user request]"
CONTINUED_SESSION_NOTE = (
    "This session is being continued from a previous conversation "
    "that ran out of context."
)
# What a previous compaction summary is marked with in the transcript the
# summarizer reads, so it merges that ledger into the new summary instead of
# letting the earlier compaction's facts fall out of the session.
PREVIOUS_COMPACTION_NOTE = "[Previous Compaction Summary — merge and update with newer steps]"

# Input of an attempt rerun after an overflowing session was summarized: the
# summary already holds the request and what was done for it. It lives here so
# the dialogue a compaction preserves can tell it from the user's own words.
OVERFLOW_RETRY_NOTE = (
    "[The conversation was compacted into the summary above to fit the context "
    "window.] Continue the current request from where you stopped; do not redo "
    "steps that already succeeded."
)

# Dialogue a compaction preserves beside the summary: the share of the compact
# threshold it may take, the floor below which it is never clipped down, and
# the characters an older message is clipped to when it must shrink.
DIALOGUE_BUDGET_SHARE = 0.35
DIALOGUE_BUDGET_MIN = 8_000
DIALOGUE_CLIP_CHARS = 2_000
# Tokens a session seeded from the stored conversation may spend on dialogue
# (core.context has no threshold of its own to ask for).
SEED_DIALOGUE_TOKENS = 24_000
# The hot tail of a mid-turn compaction: how many matched tool-call pairs of
# the active turn are kept, and the tokens the whole tail may take.
TOOL_TAIL_PAIRS = 2
TOOL_TAIL_TOKENS = 12_000

# One injected auto-run result: a header line, then the result text, up to the
# next block, a bracketed section (the exchange the agent missed), or the end.
_AUTO_RUN_BLOCK_RE = re.compile(
    r"[ \t]*=== AUTO-RUN TOOL RESULT[^\n]*===\n[\s\S]*?"
    r"(?=\n[ \t]*(?:=== AUTO-RUN TOOL RESULT|\[)|\Z)"
)


def _text_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _tokens(value: Any) -> int:
    """Estimated tokens of any JSON-like value; images count as IMAGE_TOKENS."""
    if isinstance(value, str):
        return _text_tokens(value)
    if isinstance(value, dict):
        modality = part_modality(value)
        if modality is not None:
            return MEDIA_TOKENS[modality]
        return sum(_tokens(item) for key, item in value.items() if key != "id")
    if isinstance(value, (list, tuple)):
        return sum(_tokens(item) for item in value)
    return 0


def item_tokens(item: Any) -> int:
    """Estimated tokens one session item adds to a request."""
    if hasattr(item, "model_dump"):
        item = item.model_dump(exclude_unset=True)
    return _tokens(item)


def request_tokens(items: Iterable[Any], instructions: Optional[str] = None) -> int:
    """Estimated tokens of a request: its items and its instructions."""
    return sum(item_tokens(item) for item in items) + _text_tokens(instructions or "")


def _tool_names(items: List[Any]) -> dict:
    """call_id -> tool name, from the function calls among *items*."""
    names = {}
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call":
            names[item.get("call_id")] = item.get("name") or "tool"
    return names


def clear_old_tool_outputs(
    items: List[Any],
    *,
    budget: int,
    keep_last: int,
    tools: Optional[Set[str]],
    instructions: Optional[str] = None,
) -> List[Any]:
    """*items* with the oldest compactable tool outputs cleared until within *budget*.

    ``tools`` None makes every tool compactable. The newest *keep_last*
    compactable outputs are never cleared, so a request may stay over budget;
    the caller's reactive path handles a real overflow. Items that are not
    cleared are returned as they are; a cleared one is a copy.
    """
    total = request_tokens(items, instructions)
    if total <= budget:
        return items
    names = _tool_names(items)
    candidates = [
        index
        for index, item in enumerate(items)
        if isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and (tools is None or names.get(item.get("call_id")) in tools)
        and not (isinstance(item.get("output"), str) and item["output"].startswith("[Output of "))
    ]
    if keep_last > 0:
        candidates = candidates[:-keep_last]
    result = list(items)
    for index in candidates:
        if total <= budget:
            break
        item = result[index]
        cleared = {**item, "output": CLEARED_NOTE.format(tool=names.get(item.get("call_id"), "a tool"))}
        total -= item_tokens(item) - item_tokens(cleared)
        result[index] = cleared
    return result


def context_budget_filter(
    budget: int, compact_cfg: Any, on_cleared: Optional[Callable[[int], None]] = None
) -> Optional[Callable[[List[Any], Optional[str]], List[Any]]]:
    """The per-call clearing step for a request of at most *budget* tokens - the
    agent's auto-compact threshold - or None when off.

    *on_cleared* hears how many tool outputs a request left out, when any.

    Returns ``apply(items, instructions) -> items``; the factory chains it after
    the image budget in its ``call_model_input_filter``.
    """
    if compact_cfg is None or not compact_cfg.enabled or not compact_cfg.micro.enabled:
        return None
    micro = compact_cfg.micro
    tools = set(micro.compactable_tools) if micro.compactable_tools else None

    def apply(items: List[Any], instructions: Optional[str]) -> List[Any]:
        result = clear_old_tool_outputs(
            items,
            budget=budget,
            keep_last=micro.preserve_last_n,
            tools=tools,
            instructions=instructions,
        )
        if on_cleared is not None and result is not items:
            cleared = sum(1 for before, after in zip(items, result) if before is not after)
            if cleared:
                on_cleared(cleared)
        return result

    return apply


def _clip(text: str, limit: int) -> str:
    """*text* clipped to about *limit* characters, keeping head and tail.

    The end of a tool output is often what matters most - a test summary, a
    traceback's last frames, an ``orchestrate`` status with its context_id, a
    commit hash - so the tail is kept, not only the head.
    """
    if len(text) <= limit:
        return text
    head_len = int(limit * 0.4)
    tail_len = limit - head_len
    return (
        text[:head_len]
        + f"\n… [{len(text) - limit} more characters] …\n"
        + (text[len(text) - tail_len:] if tail_len else "")
    )


def strip_auto_run_preamble(text: str) -> str:
    """The user's own words, without the notes a turn puts in front of them.

    A turn's input starts with what ran automatically first (blocks of
    ``=== AUTO-RUN TOOL RESULT '...' ===``), what other agents and the user
    said meanwhile, and the ``[Current user request]`` header. None of that is
    the user's; the summary needs the request, and the injected results repeat
    what the tool calls in the session already say. What the notes hold besides
    the auto-run results - the missed exchange, above all - stays.
    """
    if not isinstance(text, str) or not text:
        return text
    has_header = CURRENT_REQUEST_HEADER in text
    if not has_header and AUTO_RUN_RESULT_MARKER not in text:
        return text
    notes, _, request = text.partition(CURRENT_REQUEST_HEADER) if has_header else (text, "", "")
    if has_header:
        request = request.lstrip("\n")
    # Notes that are not injected tool results - the exchange the agent missed -
    # are kept; only the auto-run blocks go.
    notes = _AUTO_RUN_BLOCK_RE.sub("", notes).strip()
    if not has_header:
        return notes
    return f"{notes}\n\n{request}" if notes else request


def _is_compaction_summary(text: str) -> bool:
    """Whether *text* is a summary an earlier compaction left in the session."""
    stripped = text.lstrip()
    return (
        stripped.startswith(COMPACTED_SUMMARY_NOTE)
        or stripped.startswith(CONTINUED_SESSION_NOTE)
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        modality = part_modality(part)
        if modality is not None:
            parts.append(f"[{modality}]")
        elif isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def session_transcript(items: Iterable[Any]) -> List[CompactMessage]:
    """The session as the summarizer reads it: turns, tool calls and their results.

    Reasoning is left out; tool arguments and outputs are clipped (head and
    tail); images are named, not sent. A user message's auto-run preamble is
    stripped to the request it ends with, and a previous compaction summary is
    marked so the summarizer merges it instead of dropping it.
    """
    items = [item.model_dump(exclude_unset=True) if hasattr(item, "model_dump") else item for item in items]
    names = _tool_names(items)
    messages: List[CompactMessage] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call":
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, default=str)
            text = f"[Called {item.get('name') or 'tool'}({_clip(arguments, _TRANSCRIPT_ARGUMENT_CHARS)})]"
            messages.append(CompactMessage(role="assistant", content=text))
        elif kind == "function_call_output":
            output = item.get("output")
            output = _content_text(output) if isinstance(output, list) else str(output or "")
            tool = names.get(item.get("call_id"), "tool")
            text = f"[Result of {tool}: {_clip(output, _TRANSCRIPT_OUTPUT_CHARS)}]"
            messages.append(CompactMessage(role="user", content=text))
        elif item.get("role") in ("user", "assistant", "system"):
            text = _content_text(item.get("content"))
            if item.get("role") == "user":
                text = strip_auto_run_preamble(text)
                if _is_compaction_summary(text):
                    text = f"{PREVIOUS_COMPACTION_NOTE}\n{text}"
            if text.strip():
                messages.append(CompactMessage(role=item["role"], content=text))
        # Reasoning and other item kinds carry nothing the summary needs.
    return messages


def _map_text_parts(content: Any, map_text: Callable[[str], str]) -> Any:
    """*content* with *map_text* applied to its text, list parts included.

    Images and other media parts pass through untouched; a text part of a
    multimodal message is mapped on its own.
    """
    if isinstance(content, str):
        return map_text(content)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append({**part, "text": map_text(part["text"])})
            else:
                parts.append(part)
        return parts
    return content


def _clean_dialogue_content(role: Any, content: Any) -> Any:
    """The clean text of a dialogue message: no injected tool results, no footer."""
    if role == "user":
        return _map_text_parts(content, strip_auto_run_preamble)
    return _map_text_parts(content, strip_service_context_footer)


def _is_preservable_dialogue(item: Any) -> bool:
    """Whether *item* is a dialogue message a compaction keeps beside its summary."""
    if not isinstance(item, dict):
        return False
    if item.get("role") not in ("user", "assistant"):
        return False
    if item.get("type", "message") != "message":
        return False
    text = _content_text(item.get("content")).lstrip()
    if not text:
        return False
    return not (
        _is_compaction_summary(text)
        or text.startswith(PREVIOUS_COMPACTION_NOTE)
        or text.startswith(OVERFLOW_RETRY_NOTE)
    )


def _clip_dialogue_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of *item* with a long text clipped, head and tail."""
    return {
        **item,
        "content": _map_text_parts(item.get("content"), lambda text: _clip(text, DIALOGUE_CLIP_CHARS)),
    }


def preserved_dialogue_items(
    items: Iterable[Any],
    *,
    budget: int = 0,
    preserve_recent_messages: int = 8,
) -> List[Dict[str, Any]]:
    """The user and assistant messages of *items*, clean, to keep beside a summary.

    A compaction replaces tool calls and their results with a technical
    summary, but the dialogue itself - what the user asked and what the agent
    answered - is the conversation, and the summary does not repeat it. Every
    user and assistant message is kept, its injected tool results
    (``strip_auto_run_preamble``) and legacy service footer removed; a previous
    compaction's summary, an overflow-rerun note and empty messages are not
    dialogue and are left out.

    ``budget > 0`` bounds what is kept, so a compaction always leaves the
    session well under its threshold: the newest *preserve_recent_messages*
    messages and every user message stay verbatim unless a single message is
    monstrous; older assistant answers are clipped head-and-tail first, older
    messages of any role after that, and only when hundreds of turns cannot
    fit do the oldest pairs go. ``budget`` 0 keeps everything.
    """
    preserved: List[Dict[str, Any]] = []
    for item in items:
        if hasattr(item, "model_dump"):
            item = item.model_dump(exclude_unset=True)
        if not _is_preservable_dialogue(item):
            continue
        cleaned = {**item, "content": _clean_dialogue_content(item["role"], item.get("content"))}
        if _content_text(cleaned.get("content")).strip():
            preserved.append(cleaned)
    if budget <= 0:
        return preserved
    total = sum(item_tokens(message) for message in preserved)
    if total <= budget:
        return preserved
    result = list(preserved)
    recent_start = max(0, len(result) - preserve_recent_messages)
    # Older assistant answers are clipped first - the newest dialogue and
    # every user message stay verbatim as long as the budget allows - then
    # older messages of any role.
    for roles in (("assistant",), ("assistant", "user")):
        for index in range(recent_start):
            if total <= budget:
                return result
            if result[index].get("role") not in roles:
                continue
            clipped = _clip_dialogue_item(result[index])
            if item_tokens(clipped) < item_tokens(result[index]):
                total -= item_tokens(result[index]) - item_tokens(clipped)
                result[index] = clipped
    # Hundreds of turns: clipping cannot shrink enough, so the oldest pairs go
    # until as many turns as fit in the budget remain - the newest message
    # always survives.
    while len(result) > 1 and total > budget:
        total -= item_tokens(result[0])
        result.pop(0)
    # What is left can only be over the budget by one monstrous message - a
    # pasted log in a recent turn - which is clipped rather than dropped.
    for index in range(len(result)):
        if total <= budget:
            break
        clipped = _clip_dialogue_item(result[index])
        if item_tokens(clipped) < item_tokens(result[index]):
            total -= item_tokens(result[index]) - item_tokens(clipped)
            result[index] = clipped
    return result


def _clip_tool_item(item: Dict[str, Any], field: str, limit: int) -> Dict[str, Any]:
    """A copy of a tool item with its *field* (output, arguments) clipped."""
    value = item.get(field)
    if isinstance(value, str):
        return {**item, field: _clip(value, limit)}
    if isinstance(value, list):
        return {**item, field: _map_text_parts(value, lambda text: _clip(text, limit))}
    return item


def recent_turn_tool_tail(
    items: List[Any], *, max_pairs: int = TOOL_TAIL_PAIRS, max_tokens: int = TOOL_TAIL_TOKENS
) -> List[Any]:
    """The newest matched tool-call pairs of the active turn, for a mid-turn compaction.

    A turn compacted between its own tool steps must not lose the results it
    is working from: the last *max_pairs* ``(function_call,
    function_call_output)`` pairs after the session's last user message are
    kept beside the summary, every call with its matching output - never an
    orphan of either, which the model's API rejects. A huge output is clipped
    head-and-tail, the oldest kept pair first, then its arguments, so the whole
    tail stays within *max_tokens*; a tail too big to clip below the budget
    drops its oldest pairs first.
    """
    if max_tokens <= 0 or not items:
        return []
    items = [item.model_dump(exclude_unset=True) if hasattr(item, "model_dump") else item for item in items]
    last_user = -1
    for index, item in enumerate(items):
        if (
            isinstance(item, dict)
            and item.get("role") == "user"
            and item.get("type", "message") == "message"
        ):
            last_user = index
    active = items[last_user + 1:]
    calls: Dict[str, int] = {}
    outputs: Dict[str, int] = {}
    for index, item in enumerate(active):
        if not isinstance(item, dict):
            continue
        call_id = item.get("call_id")
        if item.get("type") == "function_call" and call_id:
            calls.setdefault(str(call_id), index)
        elif item.get("type") == "function_call_output" and str(item.get("call_id")) in calls:
            outputs.setdefault(str(item.get("call_id")), index)
    if not outputs:
        return []
    newest = sorted(outputs.items(), key=lambda pair: pair[1])[-max_pairs:]
    tail: List[Any] = []
    for call_id, _ in newest:
        tail.append(active[calls[call_id]])
        tail.append(active[outputs[call_id]])
    total = request_tokens(tail)
    # A huge result (a file, a test log) is clipped head-and-tail, each round
    # shorter than the last, the oldest kept pair first.
    for limit in (4_000, 2_000, 800, 200, 40):
        if total <= max_tokens:
            break
        for field in ("output", "arguments"):
            if total <= max_tokens:
                break
            kind = "function_call_output" if field == "output" else "function_call"
            for index, item in enumerate(tail):
                if total <= max_tokens:
                    break
                if item.get("type") != kind:
                    continue
                clipped = _clip_tool_item(item, field, limit)
                if item_tokens(clipped) < item_tokens(item):
                    total -= item_tokens(item) - item_tokens(clipped)
                    tail[index] = clipped
    # Still over the budget - pairs too big to clip below it: the oldest kept
    # pairs go, the newest first kept.
    while len(tail) > 2 and total > max_tokens:
        for item in tail[:2]:
            total -= item_tokens(item)
        tail = tail[2:]
    return tail
