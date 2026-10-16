"""What a model request carries besides text, and how to leave a kind of it out.

A request's content parts are text, or media of one modality: an image, an
audio recording, a video or a PDF document - the names models.dev gives what a
model accepts (core.model_catalog). Media come from the user (an attached
image) and from tools (``file_read`` of a picture, a screenshot); in SDK input
items they are ``input_image``, ``input_audio``, ``input_file`` (a video, an
audio file or a PDF by its data URL or file name) and the Chat Completions
shapes of the same (``image_url``, ``video_url``, ``file``).

core.fallback_model sends a request only to the models that accept what it
carries; where none of an agent's models does, the media are replaced by a
note that says so, and the agent learns it lacks that modality.
"""

from __future__ import annotations

import mimetypes
from typing import Any, Callable, Dict, FrozenSet, Iterable, Optional

TEXT = "text"
IMAGE = "image"
AUDIO = "audio"
VIDEO = "video"
PDF = "pdf"
#: Every modality besides text, in the order notes name them.
MEDIA = (IMAGE, AUDIO, VIDEO, PDF)
#: What a model accepts when nothing says otherwise.
TEXT_ONLY: FrozenSet[str] = frozenset({TEXT})

#: How a note names one of a modality.
NAMES: Dict[str, str] = {
    IMAGE: "image",
    AUDIO: "audio recording",
    VIDEO: "video",
    PDF: "PDF document",
}

_IMAGE_TYPES = frozenset({"input_image", "image_url"})
_FILE_TYPES = frozenset({"input_file", "file"})


def modality_of_mime(mime: Optional[str]) -> Optional[str]:
    """The modality of a MIME type; None for one that is none of them."""
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if mime == "application/pdf":
        return PDF
    kind = mime.split("/", 1)[0]
    return {"image": IMAGE, "audio": AUDIO, "video": VIDEO}.get(kind)


def _mime_of_data_url(url: Any) -> Optional[str]:
    if isinstance(url, str) and url.startswith("data:"):
        return url[5:].split(",", 1)[0].split(";", 1)[0] or None
    return None


def _file_modality(part: Dict[str, Any]) -> Optional[str]:
    """The modality of a file part, by its data URL, else by its file name."""
    fields = part.get("file") if isinstance(part.get("file"), dict) else part
    mime = _mime_of_data_url(fields.get("file_data")) or _mime_of_data_url(fields.get("file_url"))
    if mime is None and isinstance(fields.get("filename"), str):
        mime = mimetypes.guess_type(fields["filename"])[0]
    return modality_of_mime(mime)


def part_modality(part: Any) -> Optional[str]:
    """The modality of one content part; None for text and anything else."""
    if not isinstance(part, dict):
        return None
    kind = part.get("type")
    if kind in _IMAGE_TYPES:
        return IMAGE
    if kind == "input_audio":
        return AUDIO
    if kind == "video_url":
        return VIDEO
    if kind in _FILE_TYPES:
        return _file_modality(part)
    return None


def _holders(item: Any) -> Iterable[str]:
    """The keys of *item* that hold content parts: messages ``content``, tool results ``output``."""
    if not isinstance(item, dict):
        return ()
    return [key for key in ("content", "output") if isinstance(item.get(key), list)]


def request_modalities(items: Any) -> FrozenSet[str]:
    """The modalities a request's input carries besides text."""
    found = set()
    if not isinstance(items, list):
        return frozenset()
    for item in items:
        modality = part_modality(item)
        if modality:
            found.add(modality)
        for key in _holders(item):
            found.update(m for m in map(part_modality, item[key]) if m)
    return frozenset(found)


def replace_modalities(
    items: Any, leave_out: FrozenSet[str], note: Callable[[str], str]
) -> Any:
    """*items* with every part of a modality in *leave_out* replaced by
    ``note(modality)`` as text; the caller's items are never changed."""
    if not leave_out or not isinstance(items, list):
        return items

    def replaced(part: Any) -> Any:
        modality = part_modality(part)
        if modality in leave_out:
            return {"type": "input_text", "text": note(modality)}
        return part

    result = []
    for item in items:
        if not isinstance(item, dict):
            result.append(item)
            continue
        if part_modality(item) in leave_out:
            result.append(replaced(item))
            continue
        copy = item
        for key in _holders(item):
            parts = [replaced(part) for part in item[key]]
            if any(new is not old for new, old in zip(parts, item[key])):
                copy = dict(copy) if copy is item else copy
                copy[key] = parts
        result.append(copy)
    return result
