"""The message a caller hands to an agent: plain text or SDK input items.

Callers pass either text or, when the message carries images, a JSON-encoded
SDK message: ``{"role": "user", "content": [{"type": "input_text", ...},
{"type": "input_image", ...}]}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, List, Union

from schemas import ImageContent, ImageUrl, TextContent

IMAGE_PART_TYPES = ("input_image", "image_url")


@dataclass(frozen=True)
class AgentInput:
    """A parsed agent message: what the SDK receives, and whether it has images."""

    items: Union[str, List[Any]]
    multimodal: bool

    @property
    def is_text(self) -> bool:
        return isinstance(self.items, str)


def _has_image(message: Any) -> bool:
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") in IMAGE_PART_TYPES
        for part in content
    )


def parse_agent_input(message: str) -> AgentInput:
    """Text stays text; a JSON SDK message becomes a one-item input list."""
    if not message.lstrip().startswith("{"):
        return AgentInput(message, multimodal=False)
    try:
        parsed = json.loads(message)
    except ValueError:
        return AgentInput(message, multimodal=False)
    if isinstance(parsed, dict) and "role" in parsed:
        return AgentInput([parsed], multimodal=_has_image(parsed))
    # Any other JSON is what the user typed.
    return AgentInput(message, multimodal=False)


def context_content(sdk_message: dict) -> List[Any]:
    """The content of an SDK message as ContextMessage parts (text and images)."""
    parts: List[Any] = []
    for part in sdk_message.get("content") or []:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "input_text":
            parts.append(TextContent(type="text", text=part.get("text", "")))
        elif kind == "input_image":
            parts.append(
                ImageContent(
                    type="image_url",
                    image_url=ImageUrl(
                        url=part.get("image_url", ""), detail=part.get("detail", "auto")
                    ),
                )
            )
        else:
            parts.append(part)
    return parts
