"""Media files as a tool hands them to a model: images, audio, video, PDF.

Reading a picture as text gave the model its bytes as mojibake; an agent could
not look at a screenshot the user dropped into its workspace. Here a file is
recognized by its first bytes (not its name) and becomes the tool output a
model reads in that modality - an image, or a file part carrying audio, video
or a PDF as a data URL. Which model gets it is core.fallback_model's choice:
only one that accepts the modality, and when none of the agent's models does,
a note telling the agent so.

Sizes are bounded: a request carries the whole file, and history sends it
again with every later call until the media budget drops it
(core.image_window). Formats are the ones providers accept; anything else is
named, with what to do about it, rather than sent.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any, Callable, List, Optional, Union

from agents.tool import ToolOutputFileContent, ToolOutputImage, ToolOutputText

from core.modalities import AUDIO, IMAGE, NAMES, PDF, VIDEO, modality_of_mime

logger = logging.getLogger("grid.media")

#: Longest side of an image a model gets; larger ones are scaled down.
MAX_IMAGE_SIDE = 2048
#: Largest image file read at all (it is scaled before it is sent).
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
_LIMITS = {IMAGE: MAX_IMAGE_BYTES, AUDIO: MAX_AUDIO_BYTES, VIDEO: MAX_VIDEO_BYTES, PDF: MAX_PDF_BYTES}

#: Image formats models read as they are; others are converted to PNG.
_IMAGE_FORMATS = {"image/png", "image/jpeg", "image/gif", "image/webp"}
#: Audio formats providers take (OpenRouter's list), by MIME type: the ``format`` they are sent as.
AUDIO_FORMATS = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/mp4": "m4a",
    "audio/aiff": "aiff",
}
#: Video formats providers take (OpenRouter's list), by the MIME type they are sent as.
VIDEO_FORMATS = {"video/mp4", "video/mpeg", "video/mov", "video/webm"}

_HEAD = 64


def sniff(head: bytes) -> Optional[str]:
    """The MIME type of a file by its first bytes; None for anything not a media file."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM") and head[6:10] == b"\x00\x00\x00\x00" and _bmp_header(head):
        return "image/bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video/x-msvideo"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "audio/aiff"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/mov"
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif"):
            return "image/heif"
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm" if b"webm" in head else "video/x-matroska"
    if head[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return "video/mpeg"
    if len(head) > 2 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        return "audio/aac"  # ADTS
    if _mpeg_audio_frame(head):
        return "audio/mpeg"
    return None


def _bmp_header(head: bytes) -> bool:
    return len(head) >= 18 and int.from_bytes(head[14:18], "little") in (12, 40, 52, 56, 64, 108, 124)


def _mpeg_audio_frame(head: bytes) -> bool:
    """An MP3 without ID3 tag: a frame header with valid layer, bitrate and rate
    (and not the FF FE that opens UTF-16 text)."""
    if len(head) < 3 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0 or head[1] == 0xFE:
        return False
    layer, bitrate, rate = (head[1] >> 1) & 3, head[2] >> 4, (head[2] >> 2) & 3
    return layer != 0 and bitrate not in (0, 15) and rate != 3


def looks_binary(head: bytes) -> bool:
    """Whether a file that is no media is still not text: a NUL in its first bytes."""
    return b"\x00" in head


def _data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _image_bytes(data: bytes, mime: str) -> tuple[bytes, str, str]:
    """The image as a model gets it: within MAX_IMAGE_SIDE and in a format it reads.

    Returns (bytes, MIME type, what was done to it).
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        width, height = image.size
        scaled = max(width, height) > MAX_IMAGE_SIDE
        if not scaled and mime in _IMAGE_FORMATS:
            return data, mime, f"{width}x{height}"
        if scaled:
            ratio = MAX_IMAGE_SIDE / max(width, height)
            image = image.resize((max(1, round(width * ratio)), max(1, round(height * ratio))))
        if image.mode not in ("RGB", "RGBA", "L", "LA"):
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        note = f"{width}x{height}" + (f", scaled to {image.size[0]}x{image.size[1]}" if scaled else "")
        return out.getvalue(), "image/png", note


def _size(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


MediaOutput = List[Union[ToolOutputText, ToolOutputImage, ToolOutputFileContent]]


def media_output(
    path: Path,
    visible: str,
    *,
    open_file: Callable[[Path, str], Any] = open,
) -> Optional[Union[MediaOutput, str]]:
    """What a model is handed for the file at *path*: None when it is no media file.

    A list of tool output parts - a line naming the file, then the media - or,
    for a media file that cannot be sent (too large, a format providers do not
    take), the reason as text. *visible* is the path as the agent named it;
    *open_file* opens the file (utils.confined_fs.open_file for a workspace).
    """
    with open_file(path, "rb") as handle:
        head = handle.read(_HEAD)
    mime = sniff(head)
    modality = modality_of_mime(mime)
    if modality is None:
        return None
    size = path.stat().st_size
    name = NAMES[modality]
    if size > _LIMITS[modality]:
        return (
            f"❌ {visible} is a {name} ({mime}, {_size(size)}), larger than the {_size(_LIMITS[modality])} "
            f"a model can be sent. Cut or compress it first (for example with ffmpeg), then read the result."
        )
    with open_file(path, "rb") as handle:
        data = handle.read()
    header = f"📎 {visible}: {name}, {mime}, {_size(size)}"

    if modality == IMAGE:
        try:
            data, mime, shape = _image_bytes(data, mime)
        except Exception as exc:  # a damaged image, or a format Pillow cannot open
            return f"❌ {visible} looks like an image ({mime}) but cannot be decoded: {exc}"
        return [ToolOutputText(text=f"{header}, {shape}"), ToolOutputImage(image_url=_data_url(mime, data), detail="auto")]

    if modality == AUDIO and mime not in AUDIO_FORMATS:
        return (
            f"❌ {visible} is audio in a format models are not sent ({mime}); "
            f"convert it to one of {', '.join(sorted(set(AUDIO_FORMATS.values())))} first."
        )
    if modality == VIDEO and mime not in VIDEO_FORMATS:
        return (
            f"❌ {visible} is video in a format models are not sent ({mime}); "
            "convert it to mp4 or webm first."
        )
    return [ToolOutputText(text=header), ToolOutputFileContent(file_data=_data_url(mime, data), filename=path.name)]
