"""How much of the model's context window a request takes, and keeping it inside.

What the model reads is the agent's SDK session - every message, tool call and
tool result, images included - plus the instructions and the tool schemas. The
stored chat (``context.json``) is only the text users see, a small part of it, so
every size here is measured on session items.

Grid keeps a request inside the window at two points, both driven by the
system's ``compact`` config and the model's ``context_window``:

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
from typing import Any, Callable, Iterable, List, Optional, Set

from core.compact.auto_compact import get_auto_compact_threshold
from core.compact.base import CompactMessage

CHARS_PER_TOKEN = 4
#: A screenshot-sized image costs about this much with common vision models.
IMAGE_TOKENS = 1500
IMAGE_PART_TYPES = frozenset({"input_image", "image_url"})

CLEARED_NOTE = (
    "[Output of {tool} cleared to keep the context within the model's window. "
    "Run it again if you need it.]"
)

# The summarizer reads tool calls and results clipped to these lengths: enough
# to know what was done and found, not whole files.
_TRANSCRIPT_ARGUMENT_CHARS = 1500
_TRANSCRIPT_OUTPUT_CHARS = 3000


def _text_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _tokens(value: Any) -> int:
    """Estimated tokens of any JSON-like value; images count as IMAGE_TOKENS."""
    if isinstance(value, str):
        return _text_tokens(value)
    if isinstance(value, dict):
        if value.get("type") in IMAGE_PART_TYPES:
            return IMAGE_TOKENS
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
    context_window: int, compact_cfg: Any
) -> Optional[Callable[[List[Any], Optional[str]], List[Any]]]:
    """The per-call clearing step for a model with *context_window*, or None when off.

    Returns ``apply(items, instructions) -> items``; the factory chains it after
    the image budget in its ``call_model_input_filter``.
    """
    if compact_cfg is None or not compact_cfg.enabled or not compact_cfg.micro.enabled:
        return None
    budget = get_auto_compact_threshold(context_window, compact_cfg)
    micro = compact_cfg.micro
    tools = set(micro.compactable_tools) if micro.compactable_tools else None

    def apply(items: List[Any], instructions: Optional[str]) -> List[Any]:
        return clear_old_tool_outputs(
            items,
            budget=budget,
            keep_last=micro.preserve_last_n,
            tools=tools,
            instructions=instructions,
        )

    return apply


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more characters]"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") in IMAGE_PART_TYPES:
            parts.append("[image]")
        elif isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def session_transcript(items: Iterable[Any]) -> List[CompactMessage]:
    """The session as the summarizer reads it: turns, tool calls and their results.

    Reasoning is left out; tool arguments and outputs are clipped; images are
    named, not sent.
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
            if text.strip():
                messages.append(CompactMessage(role=item["role"], content=text))
        # Reasoning and other item kinds carry nothing the summary needs.
    return messages
