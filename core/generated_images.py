"""Images a model generates as (part of) its answer, kept apart from the text.

Image-generation models served over chat completions - OpenRouter's
``google/gemini-*-image`` and alike, asked with ``modalities: ["image", "text"]``
(ModelConfig.modalities) - return pictures in a non-standard ``images`` field:
``message.images`` in a whole response, ``delta.images`` in a streamed one,
each ``{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}``.
The Agents SDK drops that field, so VisionChatCompletionsModel reads it and
reports every image here.

The images go to the :class:`ImageCollector` of the turn in progress, a context
variable the factory sets around its runs; the SDK starts a run's tasks from
that context, so the model sees it too, and so do sub-agents started inside the
turn. The collector passes each image on at once (the chat shows it as it
arrives) and keeps them for the stored answer. They never enter the answer's
text: a base64 picture there would be resent to the model as text on every
later turn.

Only ``data:`` URLs of PNG, JPEG, WebP or GIF are accepted: the chat renders
them as-is, and a remote URL would make every viewer's browser fetch it.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, List, Optional

logger = logging.getLogger(__name__)

_DATA_URL = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$")
# One generated image; a 4096x4096 PNG stays well within this.
MAX_IMAGE_CHARS = 40 * 1024 * 1024


class ImageCollector:
    """The images generated during one turn, in order, each once."""

    def __init__(self, on_image: Optional[Callable[[str], None]] = None) -> None:
        self.images: List[str] = []
        self._on_image = on_image

    def add(self, url: str) -> None:
        if url in self.images:
            return
        self.images.append(url)
        if self._on_image is not None:
            try:
                self._on_image(url)
            except Exception:  # showing it live must never cost the image
                logger.exception("Could not pass a generated image on")


_COLLECTOR: ContextVar[Optional[ImageCollector]] = ContextVar("grid_generated_images", default=None)


@contextmanager
def collecting(collector: ImageCollector) -> Iterator[ImageCollector]:
    """Send the images generated inside this block to *collector*."""
    token = _COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _COLLECTOR.reset(token)


def generated_so_far() -> List[str]:
    """The images generated so far in the turn in progress."""
    collector = _COLLECTOR.get()
    return list(collector.images) if collector is not None else []


def image_urls(message: Any) -> List[str]:
    """The accepted image URLs in the ``images`` field of a message or delta."""
    if message is None:
        return []
    entries = getattr(message, "images", None)
    if entries is None:
        extra = getattr(message, "model_extra", None) or {}
        entries = extra.get("images")
    if entries is None and isinstance(message, dict):
        entries = message.get("images")
    urls = []
    for entry in entries or []:
        image_url = entry.get("image_url") if isinstance(entry, dict) else getattr(entry, "image_url", None)
        url = image_url.get("url") if isinstance(image_url, dict) else getattr(image_url, "url", image_url)
        if isinstance(url, str) and len(url) <= MAX_IMAGE_CHARS and _DATA_URL.match(url):
            urls.append(url)
        elif url:
            logger.warning("Ignored a generated image that is not an inline PNG/JPEG/WebP/GIF")
    return urls


def report(message: Any) -> None:
    """Hand the images of *message* to the collector of the turn in progress."""
    collector = _COLLECTOR.get()
    urls = image_urls(message)
    if collector is None:
        if urls:
            logger.warning("A model generated %d image(s) outside a turn; they are dropped", len(urls))
        return
    for url in urls:
        collector.add(url)


class ReportingStream:
    """A chat-completions chunk stream that reports the images its deltas carry.

    Chunks pass through unchanged; the SDK only iterates the stream.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    async def __aiter__(self):
        async for chunk in self._stream:
            for choice in getattr(chunk, "choices", None) or []:
                report(getattr(choice, "delta", None))
            yield chunk
