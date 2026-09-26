"""Keep the images a model call carries within a budget.

An agent that looks at the screen adds a screenshot at almost every step, and
its SDK session keeps every one of them - the history must survive stops and
later turns. Resent whole on each model call they make the call slow (eleven
1920x1080 screenshots took 27-39 s to the first token) and then impossible:
providers cap the images of one request ("Too many images in request: 31 > 30").

So before each model call only the newest ``max_images`` images go out; every
older one is replaced in that request by a short text note, in place, so the
turn it belonged to still reads in order. The session is never changed: the
filter works on a copy, and a later request with a larger budget sends them
all again.
"""

from __future__ import annotations

from typing import Any, Callable, List

from agents.run import CallModelData, ModelInputData

#: Content-part types that carry an image in SDK input items.
IMAGE_PART_TYPES = frozenset({"input_image", "image_url"})

OMITTED_NOTE = (
    "[Earlier image omitted: only the {limit} most recent images are sent with each "
    "request. Look again (for example, take a new screenshot) if you need it.]"
)


def _parts_of(item: Any) -> tuple[str, list] | None:
    """The key and list holding an item's content parts, or None.

    Messages keep parts under ``content``; tool results (``function_call_output``
    and the like) under ``output``.
    """
    if not isinstance(item, dict):
        return None
    for key in ("content", "output"):
        parts = item.get(key)
        if isinstance(parts, list):
            return key, parts
    return None


def _is_image(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") in IMAGE_PART_TYPES


def limit_images(items: List[Any], max_images: int) -> List[Any]:
    """*items* with only the newest *max_images* images; older ones become notes.

    Counts from the end, so the newest images always survive. Items without an
    omitted image are returned as they are; an item that loses one is a shallow
    copy with a new parts list, so the caller's items are never modified.
    """
    kept = 0
    result = list(items)
    note = {"type": "input_text", "text": OMITTED_NOTE.format(limit=max_images)}
    for index in range(len(result) - 1, -1, -1):
        found = _parts_of(result[index])
        if found is None:
            continue
        key, parts = found
        new_parts = None
        for position in range(len(parts) - 1, -1, -1):
            if not _is_image(parts[position]):
                continue
            if kept < max_images:
                kept += 1
                continue
            if new_parts is None:
                new_parts = list(parts)
            new_parts[position] = dict(note)
        if new_parts is not None:
            result[index] = {**result[index], key: new_parts}
    return result


def image_budget_filter(max_images: int) -> Callable[[CallModelData], ModelInputData]:
    """A ``RunConfig.call_model_input_filter`` that applies :func:`limit_images`."""

    def apply(data: CallModelData) -> ModelInputData:
        model_data = data.model_data
        return ModelInputData(
            input=limit_images(model_data.input, max_images),
            instructions=model_data.instructions,
        )

    return apply
