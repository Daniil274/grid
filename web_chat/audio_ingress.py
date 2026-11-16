"""Validated audio messages and the shared native/ASR policy for chat channels.

No paid transcription endpoint is used. Automatic routing needs speech text;
an explicitly selected runtime may receive the recording without transcription.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any, Literal

from core.media import AUDIO_FORMATS, MAX_AUDIO_BYTES, sniff

_DATA_URL = re.compile(r"data:(audio/[a-z0-9.+-]+);base64,([A-Za-z0-9+/]*={0,2})", re.I)
_ALIASES = {"audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/opus": "audio/ogg", "audio/mp3": "audio/mpeg"}


@dataclass(frozen=True)
class AudioAttachment:
    data: bytes
    mime_type: str

    def __post_init__(self) -> None:
        validate_audio_payload(self.data, self.mime_type, max_bytes=MAX_AUDIO_BYTES)
        object.__setattr__(self, "mime_type", _mime(self.mime_type))

    @property
    def data_url(self) -> str:
        return f"data:{self.mime_type};base64," + base64.b64encode(self.data).decode("ascii")


def _mime(value: str) -> str:
    media = value.partition(";")[0].strip().lower()
    return _ALIASES.get(media, media)


def validate_audio_payload(data: bytes, content_type: str, *, max_bytes: int) -> None:
    """Bound bytes and verify the container, not merely a client-supplied MIME."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("Audio payload is empty")
    if len(data) > max_bytes:
        raise ValueError(f"Audio payload exceeds {max_bytes} bytes")
    media = _mime(content_type)
    if media not in AUDIO_FORMATS:
        raise ValueError(f"Unsupported audio content type: {media or 'missing'}")
    if sniff(data[:64]) != media:
        raise ValueError("Audio file signature does not match its content type")


def parse_audio_data_url(value: object, *, max_bytes: int = MAX_AUDIO_BYTES) -> AudioAttachment:
    if not isinstance(value, str):
        raise ValueError("Audio must be an audio data URL")
    # Reject oversized input before regex scanning or allocating decoded bytes.
    if len(value) > ((max_bytes + 2) // 3) * 4 + 128:
        raise ValueError(f"Audio payload exceeds {max_bytes} bytes")
    match = _DATA_URL.fullmatch(value)
    if match is None:
        raise ValueError("Invalid audio data URL")
    mime, encoded = match.groups()
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid base64 audio payload") from None
    validate_audio_payload(data, mime, max_bytes=max_bytes)
    return AudioAttachment(data, mime)


@dataclass(frozen=True)
class AudioIngress:
    route: Literal["native", "transcribe"]
    reason: str


def route_audio(*, explicitly_selected: bool, runtime_accepts_audio: bool) -> AudioIngress:
    if explicitly_selected and runtime_accepts_audio:
        return AudioIngress("native", "selected runtime accepts audio")
    return AudioIngress("transcribe", "speech text is required for this turn")


def runtime_accepts_audio(runtime: Any, audio: AudioAttachment) -> bool:
    """Inspect available runtime candidates, including transport and refusals.

    Responses and CLI transports do not implement the chat audio normalization.
    OpenAI-compatible providers other than OpenRouter accept WAV/MP3 only here.
    """
    from core.vision_model import VisionChatCompletionsModel

    for candidate in getattr(getattr(runtime, "model", None), "candidates", ()):
        if candidate.accepts("audio") is not True:
            continue
        model = candidate.model
        if not isinstance(model, VisionChatCompletionsModel):
            continue
        host = getattr(getattr(getattr(model, "_client", None), "base_url", None), "host", "")
        if audio.mime_type in {"audio/wav", "audio/mpeg"} or host == "openrouter.ai":
            return True
    return False


async def transcribe_audio(audio: AudioAttachment, service: Any) -> str:
    """Local, free STT; conversion and inference stay off the event loop."""
    if service is None:
        raise RuntimeError("Local speech recognition is unavailable; enable voice and install faster-whisper.")
    wav = audio.data
    if audio.mime_type != "audio/wav":
        from web_chat.telegram.audio import to_wav
        wav = await to_wav(wav)
    result = await service.transcribe(wav)
    text = str((result or {}).get("text") or "").strip()
    if not text:
        raise ValueError("No speech was recognized in the audio message.")
    return text
