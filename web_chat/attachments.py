"""Images attached to a chat message: checked, normalized, kept, handed to the agent.

The browser sends each image as a ``data:`` URL next to the text. Nothing from
it is trusted: the type must be one of :data:`ALLOWED_TYPES`, the payload must
decode as base64 within :data:`MAX_IMAGE_BYTES`, and Pillow must be able to
read it as that image. The model is then given a JPEG re-encoded within the
configured ``image_processing`` size and quality - the same policy the CLI
applies to attached files - so what reaches the model is a known format of
bounded size, never the uploaded bytes.

Every image is also written into the chat's workspace (``uploads/``, through
web_chat.uploads' link-safe writes) as soon as its turn starts: the user's own
bytes, as sent, so the workspace keeps the picture itself and not only a
model-sized copy. The message then names each file in the chat's closing
``Attached files:`` block (the format web_chat/js/lib/files.js reads back and
hides from the user's own words), with the download URL of that chat and the
workspace path an agent's file tools take:

    Attached files:
    - [image-....png](/api/workspace/files/uploads/image-....png?context=<chat>) — workspace path: "uploads/image-....png"

So the picture outlives a model that cannot see it, and its URL opens the file
of this chat's own space. The agent receives the message the way the factory
takes images: a JSON SDK user message with an ``input_text`` part and one
``input_image`` part per image - but only when a model it may run on reads
images (see :func:`runtime_accepts_images`); otherwise the text alone carries
the links.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence

from core.modalities import IMAGE
from schemas.schemas import ImageProcessingConfig

#: Images one message may carry.
MAX_IMAGES = 8
#: Decoded size of one uploaded image, before normalization.
MAX_IMAGE_BYTES = 15 * 1024 * 1024
ALLOWED_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
#: The file the kept copy of each type gets; its bytes stay what the user sent.
SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
# Decompression-bomb guard: a small file must not expand to a huge bitmap.
MAX_PIXELS = 40_000_000

_DATA_URL = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)$", re.IGNORECASE)

#: The header of the block that closes a message with the files it carries; the
#: page reads it back and hides it from the user's own words (js/lib/files.js).
ATTACHED_HEADER = "Attached files:"
#: One line of that block, exactly as the page parses it back.
ATTACHED_LINE = re.compile(
    r'^- \[[^\n]*\]\((/api/workspace/files/[^)\s]+)\) — workspace path: ("(?:[^"\\]|\\.)*")$'
)
#: The folder of a chat's workspace that keeps the files a message carried.
KEPT_DIR = "uploads"
#: The names :func:`save_images` gives its copies; a line of the block whose
#: workspace path is one of them names a copy this module wrote.
KEPT_NAME = re.compile(r"^image-\d{8}-\d{6}-[0-9a-f]{8}-\d+\.(?:png|jpg|jpeg|webp|gif)$")


class AttachmentError(ValueError):
    """An attachment was refused or could not be kept; the message is for the user."""


@dataclass(frozen=True)
class ImageAttachment:
    """One image of a message: the copy the model reads and the bytes kept.

    ``jpeg`` is the normalized ``data:`` URL (bounded size, known format) the
    model and the stored history take. ``data`` is what the user sent, kept in
    the workspace as it was; it is None once only the normalized copy is at
    hand - a message taken back from the queue's stored form.
    """

    jpeg: str
    data: Optional[bytes] = None
    mime: str = "image/jpeg"
    suffix: str = ".jpg"


def _decoded(data_url: Any) -> tuple[bytes, str]:
    """The bytes of one uploaded image ``data:`` URL, and its type."""
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
    return raw, mime


def _normalized(raw: bytes, config: ImageProcessingConfig) -> str:
    """The JPEG ``data:`` URL the model reads for image *raw*."""
    from PIL import Image, UnidentifiedImageError

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


def normalize_image(data_url: Any, config: ImageProcessingConfig) -> str:
    """One uploaded image as a JPEG ``data:`` URL within *config*'s limits."""
    raw, _mime = _decoded(data_url)
    return _normalized(raw, config)


def normalize_images(images: Any, config: Optional[ImageProcessingConfig]) -> List[ImageAttachment]:
    """The images of one message, checked and normalized; [] when there are none."""
    if images is None:
        return []
    if not isinstance(images, list):
        raise AttachmentError("Attachments must be a list of images.")
    if len(images) > MAX_IMAGES:
        raise AttachmentError(f"A message can carry at most {MAX_IMAGES} images.")
    config = config or ImageProcessingConfig()
    normalized: List[ImageAttachment] = []
    for image in images:
        raw, mime = _decoded(image)
        normalized.append(
            ImageAttachment(
                jpeg=_normalized(raw, config),
                data=raw,
                mime=mime,
                suffix=SUFFIXES[mime],
            )
        )
    return normalized


def jpeg_urls(images: Sequence[Any]) -> List[str]:
    """The normalized ``data:`` URLs of *images*: attachments or plain URLs."""
    return [image.jpeg if isinstance(image, ImageAttachment) else image for image in images]


def agent_message(text: str, images: List[str], audio: str | None = None) -> str:
    """What the agent is given: *text*, or a JSON SDK message when there are images."""
    if not images and not audio:
        return text
    content: List[dict] = [{"type": "input_text", "text": text}] if text else []
    content.extend({"type": "input_image", "image_url": url, "detail": "auto"} for url in images)
    if audio:
        content.append({"type": "input_file", "file_data": audio})
    return json.dumps({"role": "user", "content": content}, ensure_ascii=False)


def _jpeg_payload(data_url: str) -> bytes:
    """The bytes of a normalized image's ``data:`` URL."""
    encoded = data_url.partition(",")[2]
    return base64.b64decode(encoded)


def _download_url(path: str, context: Optional[str]) -> str:
    """Where the chat opens *path*: its download URL, scoped to *context*.

    A chat may work in a directory of its own (web_chat/space.py), so a
    workspace file is named by its path and its chat: without ``?context`` the
    route would open the path in the user's workspace, not in this chat's.
    """
    from urllib.parse import quote

    from web_chat.uploads import download_url

    url = download_url(path)
    if not context:
        return url
    return f"{url}?context={quote(str(context), safe='')}"


def save_images(images: Sequence[Any], workspace: Path, context: Optional[str] = None) -> List[dict[str, Any]]:
    """Write *images* into ``<workspace>/uploads``; how to link them.

    Each image is kept as the bytes the user sent (a :class:`ImageAttachment`'s
    ``data``, with its own type and suffix); a plain normalized ``data:`` URL
    is all that is known once a message comes back from the queue's store, and
    its JPEG copy is kept instead. The write goes through
    :class:`web_chat.uploads.Uploads`, which never follows a link and creates
    each file exclusively, and the file is given the workspace's owner when the
    server runs as root (as uploads there are). Returns, per image, its path
    relative to the workspace, its name and the download URL of *context* the
    agent and the page use; blocking file work, so call it off the event loop.
    """

    if not images:
        return []

    from web_chat.uploads import Uploads, keep_ownership

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    saved: List[dict[str, Any]] = []
    with Uploads(workspace) as uploads:
        for index, image in enumerate(images, 1):
            data, suffix, mime = _kept(image)
            name = f"image-{stamp}-{uuid.uuid4().hex[:8]}-{index}{suffix}"
            fd = uploads.create(name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    keep_ownership(workspace, handle.fileno())
            except BaseException:
                try:
                    uploads.unlink(name)
                except OSError:
                    pass
                raise
            path = f"{uploads.prefix}/{name}" if uploads.prefix else name
            saved.append(
                {
                    "path": path,
                    "name": name,
                    "url": _download_url(path, context),
                    "bytes": len(data),
                    "type": mime,
                }
            )
    return saved


def _kept(image: Any) -> tuple[bytes, str, str]:
    """The bytes to keep for one image of a message, with its suffix and type."""
    if isinstance(image, ImageAttachment):
        if image.data is not None:
            return image.data, image.suffix, image.mime
        return _jpeg_payload(image.jpeg), ".jpg", "image/jpeg"
    if isinstance(image, str):
        return _jpeg_payload(image), ".jpg", "image/jpeg"
    raise AttachmentError("An attachment is not an image.")


def _label(name: str) -> str:
    """The link text: brackets delimit a markdown link (js/lib/files.js does the same)."""
    return str(name).replace("[", "(").replace("]", ")")


def attachment_lines(saved: Sequence[dict[str, Any]]) -> List[str]:
    """The ``Attached files:`` lines naming *saved*, as the page reads them back."""
    return [
        f"- [{_label(item['name'])}]({item['url']}) — workspace path: {json.dumps(item['path'], ensure_ascii=False)}"
        for item in saved
    ]


def _closing_block(text: str) -> Optional[tuple[str, List[str]]]:
    """(the text before, the lines) of a closing ``Attached files:`` block, or None."""
    start = text.rfind(ATTACHED_HEADER)
    if start < 0 or (start > 0 and not text[:start].endswith("\n\n")):
        return None
    rest = text[start + len(ATTACHED_HEADER):]
    if not rest.startswith("\n"):
        return None
    lines = rest[1:].split("\n")
    if not lines or any(not line for line in lines):
        return None
    return text[:start], lines


def _is_kept_image(line: str) -> bool:
    """Whether *line* names a copy this module kept in a chat's ``uploads/``."""
    match = ATTACHED_LINE.match(line)
    if match is None:
        return False
    try:
        path = json.loads(match.group(2))
    except ValueError:
        return False
    folder, _, name = str(path).rpartition("/")
    return folder == KEPT_DIR and KEPT_NAME.match(name) is not None


def with_attachments(text: str, saved: Sequence[dict[str, Any]]) -> str:
    """*text* with *saved* named in its closing ``Attached files:`` block.

    The page reads that block back and hides it from the user's own words
    (js/lib/files.js), so the text the agent reads and the page's view agree.
    A message that already has such a block - it may name the documents the
    user attached, and an edited message carries the images kept for the
    version it replaces - keeps its other lines: only the image lines this
    module wrote are replaced, never kept beside the new ones.
    """
    lines = attachment_lines(saved)
    if not lines:
        return text
    text = str(text or "")
    closing = _closing_block(text)
    if closing is None:
        head, existing = (f"{text}\n\n" if text else ""), []
    else:
        head, existing = closing
    merged = [line for line in existing if not _is_kept_image(line)] + lines
    return f"{head}{ATTACHED_HEADER}\n" + "\n".join(merged)


def runtime_accepts_images(runtime: Any) -> bool:
    """Whether a model *runtime* may run on reads pictures.

    The same read core.fallback_model makes: a candidate that is not known to
    refuse a modality is still given it. A runtime that says nothing about its
    models is treated as able - only a model known to refuse is spared images.
    """
    candidates = getattr(getattr(runtime, "model", None), "candidates", None)
    if not candidates:
        return True
    for candidate in candidates:
        accepts = getattr(candidate, "accepts", None)
        if accepts is None or accepts(IMAGE) is not False:
            return True
    return False
