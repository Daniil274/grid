"""Images attached to a chat message: checked, normalized, handed to the agent.

The browser sends each image as a ``data:`` URL next to the text. Nothing from
it is trusted: the type must be one of :data:`ALLOWED_TYPES`, the payload must
decode as base64 within :data:`MAX_IMAGE_BYTES`, and Pillow must be able to
read it as that image. Every image is then re-encoded as JPEG within the
configured ``image_processing`` size and quality - the same policy the CLI
applies to attached files - so what reaches the model, the history and the
page is a known format of bounded size, never the uploaded bytes.

The agent receives the message the way the factory takes images: a JSON SDK
user message with an ``input_text`` part and one ``input_image`` part per image.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
from typing import Any, List, Optional

from schemas.schemas import ImageProcessingConfig

#: Images one message may carry.
MAX_IMAGES = 8
#: Decoded size of one uploaded image, before normalization.
MAX_IMAGE_BYTES = 15 * 1024 * 1024
ALLOWED_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
# Decompression-bomb guard: a small file must not expand to a huge bitmap.
MAX_PIXELS = 40_000_000

_DATA_URL = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)$", re.IGNORECASE)


class AttachmentError(ValueError):
    """An attachment was refused; the message says why, for the user."""


def normalize_image(data_url: Any, config: ImageProcessingConfig) -> str:
    """One uploaded image as a JPEG ``data:`` URL within *config*'s limits."""
    from PIL import Image, UnidentifiedImageError

    if not isinstance(data_url, str):
        raise AttachmentError("An attachment is not an image.")
    match = _DATA_URL.match(data_url.strip())
    if match is None:
        raise AttachmentError("An attachment is not an image data URL.")
    mime = match.group(1).lower()
    if mime not in ALLOWED_TYPES:
        raise AttachmentError(f"Images of type {mime} are not supported (PNG, JPEG, WebP or GIF).")
    encoded = match.group(2)
    if len(encoded) * 3 // 4 > MAX_IMAGE_BYTES:
        raise AttachmentError(f"An image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    try:
        raw = base64.b64decode(re.sub(r"\s", "", encoded), validate=True)
    except (binascii.Error, ValueError):
        raise AttachmentError("An image could not be decoded.") from None

    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise AttachmentError("An image has too many pixels.")
            image.seek(0)  # an animated GIF contributes its first frame
            frame = image.convert("RGBA") if image.mode in ("RGBA", "LA", "P") else image.convert("RGB")
    except AttachmentError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise AttachmentError("An image could not be read.") from None

    if frame.mode == "RGBA":
        # JPEG has no transparency: flatten onto white, as the CLI does.
        background = Image.new("RGB", frame.size, (255, 255, 255))
        background.paste(frame, mask=frame.split()[-1])
        frame = background
    if config.auto_resize:
        frame.thumbnail((config.max_width, config.max_height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    frame.save(output, format="JPEG", quality=config.jpeg_quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def normalize_images(images: Any, config: Optional[ImageProcessingConfig]) -> List[str]:
    """The images of one message, normalized; [] when there are none."""
    if images is None:
        return []
    if not isinstance(images, list):
        raise AttachmentError("Attachments must be a list of images.")
    if len(images) > MAX_IMAGES:
        raise AttachmentError(f"A message can carry at most {MAX_IMAGES} images.")
    config = config or ImageProcessingConfig()
    return [normalize_image(image, config) for image in images]


def agent_message(text: str, images: List[str]) -> str:
    """What the agent is given: *text*, or a JSON SDK message when there are images."""
    if not images:
        return text
    content: List[dict] = [{"type": "input_text", "text": text}] if text else []
    content.extend({"type": "input_image", "image_url": url, "detail": "auto"} for url in images)
    return json.dumps({"role": "user", "content": content}, ensure_ascii=False)
