"""Voice notes in and out: Ogg/Opus (Telegram) and WAV PCM16 mono (speech).

Telegram sends voice notes as Ogg/Opus; ``VoiceService.transcribe`` accepts
WAV PCM16 8-48 kHz; ``synthesize`` returns WAV, which Telegram will not play
as a voice note. Both directions therefore go through ``ffmpeg`` on a pipe:
nothing is written to disk and no extra Python dependency is added. Without
``ffmpeg`` voice is simply unavailable, and the channel says so.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

FFMPEG = "ffmpeg"
#: Speech recognition is happiest with one channel at 16 kHz.
WAV_RATE = 16000
#: A voice note Telegram plays inline.
OGG_BITRATE = "48k"


class AudioUnavailable(RuntimeError):
    """``ffmpeg`` is not installed, or the audio could not be converted."""


def ffmpeg_path() -> str | None:
    return shutil.which(FFMPEG)


def available() -> bool:
    """Whether voice notes can be converted on this machine."""
    return ffmpeg_path() is not None


def _run(args: list[str], data: bytes, timeout: float = 60.0) -> bytes:
    binary = ffmpeg_path()
    if binary is None:
        raise AudioUnavailable("ffmpeg is not installed")
    try:
        finished = subprocess.run(
            [binary, "-hide_banner", "-loglevel", "error", *args],
            input=data, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioUnavailable(f"ffmpeg could not convert the audio ({type(exc).__name__})") from None
    if finished.returncode != 0 or not finished.stdout:
        # stderr may name the user's file; keep it out of the message.
        raise AudioUnavailable("ffmpeg could not convert the audio")
    return finished.stdout


def to_wav_sync(data: bytes) -> bytes:
    """Ogg/Opus (or anything ffmpeg reads) to WAV PCM16 mono 16 kHz."""
    return _run(
        ["-i", "pipe:0", "-vn", "-ac", "1", "-ar", str(WAV_RATE),
         "-acodec", "pcm_s16le", "-f", "wav", "pipe:1"],
        data,
    )


def to_ogg_sync(data: bytes) -> bytes:
    """WAV from speech synthesis to an Ogg/Opus voice note."""
    return _run(
        ["-i", "pipe:0", "-vn", "-ac", "1", "-ar", str(WAV_RATE),
         "-c:a", "libopus", "-b:a", OGG_BITRATE, "-f", "ogg", "pipe:1"],
        data,
    )


async def to_wav(data: bytes) -> bytes:
    """The conversion off the event loop."""
    return await asyncio.to_thread(to_wav_sync, data)


async def to_ogg(data: bytes) -> bytes:
    return await asyncio.to_thread(to_ogg_sync, data)
