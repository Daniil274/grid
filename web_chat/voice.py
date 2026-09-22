"""Web chat voice API.

Thin, per-app service around :class:`core.speech_processor.SpeechProcessor`:

- STT (faster-whisper) and TTS (Silero) each run on a dedicated single-thread
  executor so native calls never block the event loop.
- One busy slot per engine (no unbounded backlog): a request while an engine
  is busy gets HTTP 429.
- The busy slot is released from the worker thread's ``finally`` block, so it
  stays held until the native call returns even if the awaiting asyncio task
  is cancelled.
- Temporary directories live entirely inside the worker thread and are
  cleaned up on success, error and cancellation.
- No models are loaded or downloaded at import/startup/status time; status
  uses :func:`importlib.util.find_spec` only (never imports torch or
  faster-whisper).
- Configuration is re-read from ``runtime.config_dict()`` on each request and
  deep-merged over defaults without mutating the defaults. Model/device
  changes of an already loaded model require a service restart.

Routes (registered via ``register_voice_routes(app, runtime)``):

- ``GET  /api/voice/status``
- ``POST /api/voice/transcribe``  — raw ``audio/wav`` body (not multipart),
  mono PCM16, 8–48 kHz, ≤ 60 s, ≤ 4 MiB → ``{"text", "elapsed_ms"}``.
- ``POST /api/voice/synthesize`` — ``{"text"}`` ≤ 4000 chars → WAV bytes.
- ``POST /api/voice/warmup``     — ``{"tts": false}``, STT always, TTS
  optional, executed off the event loop.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import logging
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
import httpx

from core.speech_processor import SpeechProcessor
from core import windows_speech

logger = logging.getLogger("grid.web_chat.voice")

ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_VOICE_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "stt": {
        "model_size": "small",
        "device": "cuda",
        "compute_type": "int8_float16",
        "language": "ru",
        "beam_size": 1,
    },
    "tts": {
        "backend": "silero",
        "model_path": "speech-text/model.pt",  # resolved against repo root
        "device": "cpu",
        "speaker": "xenia",
        "sample_rate": 24000,
    },
}

OPENROUTER_TTS_URL = "https://openrouter.ai/api/v1/audio/speech"
TEMPORARY_TTS_STATUSES = {429, 502, 503, 524, 529}

MAX_UPLOAD_SIZE = 4 * 1024 * 1024  # 4 MiB
MAX_AUDIO_DURATION_SEC = 60
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 48000
MAX_TTS_TEXT_LENGTH = 4000

TRANSCRIBE_CONTENT_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/vnd.wave",
    "application/octet-stream",
}

_STATUS_FIELDS = (
    "enabled",
    "stt_available",
    "tts_available",
    "stt_ready",
    "tts_ready",
    "issues",
    "stt_model",
    "stt_device",
    "tts_device",
)


class VoiceInputError(ValueError):
    """Invalid client input (bad WAV, wrong type, too large...)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class VoiceBusyError(RuntimeError):
    """An engine is already processing a request (backlog is bounded to 1)."""


class VoiceDisabledError(RuntimeError):
    """Voice features are disabled in configuration."""


class VoiceUnavailableError(RuntimeError):
    """A dependency or model is missing (actionable message)."""


class SynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TTS_TEXT_LENGTH)


class WarmupRequest(BaseModel):
    tts: bool = False


def _module_available(name: str) -> bool:
    """Check importability without importing (no torch/faster_whisper import)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` over ``base`` into a new dict (no mutation)."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def validate_wav(wav_bytes: bytes) -> Dict[str, Any]:
    """Validate a raw WAV payload: RIFF header, PCM16 mono, sample rate,
    declared vs. actual samples and duration. Raises :class:`VoiceInputError`.

    Returns metadata (duration_sec, sample_rate, channels, bits_per_sample,
    data_size).
    """
    if len(wav_bytes) > MAX_UPLOAD_SIZE:
        raise VoiceInputError(
            f"Audio payload exceeds {MAX_UPLOAD_SIZE} bytes", status_code=413
        )
    if len(wav_bytes) < 12:
        raise VoiceInputError("WAV payload too short")
    if wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        raise VoiceInputError("Invalid WAV: expected RIFF/WAVE header")

    riff_size = struct.unpack_from("<I", wav_bytes, 4)[0]
    if riff_size + 8 != len(wav_bytes):
        raise VoiceInputError("Truncated WAV: RIFF size exceeds available bytes")

    fmt: Optional[tuple] = None
    data_size: Optional[int] = None
    data_offset: Optional[int] = None
    offset = 12
    while offset + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", wav_bytes, offset + 4)[0]
        chunk_data_start = offset + 8
        if chunk_data_start + chunk_size > len(wav_bytes):
            raise VoiceInputError(
                f"Truncated WAV: incomplete '{chunk_id.decode('ascii', 'replace').strip() or '?'}' chunk"
            )
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise VoiceInputError("Invalid WAV: fmt chunk too small")
            (
                audio_format,
                channels,
                sample_rate,
                _byte_rate,
                block_align,
                bits_per_sample,
            ) = struct.unpack_from("<HHIIHH", wav_bytes, chunk_data_start)
            fmt = (audio_format, channels, sample_rate, block_align, bits_per_sample)
        elif chunk_id == b"data":
            data_size = chunk_size
            data_offset = chunk_data_start
        offset = chunk_data_start + chunk_size + (chunk_size & 1)  # word aligned

    if fmt is None:
        raise VoiceInputError("Invalid WAV: missing fmt chunk")
    if data_size is None or data_offset is None:
        raise VoiceInputError("Invalid WAV: missing data chunk")

    audio_format, channels, sample_rate, block_align, bits_per_sample = fmt
    if audio_format != 1:
        raise VoiceInputError(f"WAV must be PCM (format 1), got format {audio_format}")
    if channels != 1:
        raise VoiceInputError(f"WAV must be mono, got {channels} channels")
    if bits_per_sample != 16:
        raise VoiceInputError(f"WAV must be 16-bit PCM, got {bits_per_sample}-bit")
    if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
        raise VoiceInputError(
            f"WAV sample rate {sample_rate} out of range "
            f"{MIN_SAMPLE_RATE}..{MAX_SAMPLE_RATE} Hz"
        )
    if block_align != channels * (bits_per_sample // 8):
        raise VoiceInputError(f"Invalid WAV: block align {block_align} for mono PCM16")
    if data_size % block_align:
        raise VoiceInputError("Invalid WAV: incomplete PCM frame")

    actual_samples = len(wav_bytes) - data_offset
    if actual_samples < data_size:
        raise VoiceInputError(
            "Truncated WAV: fewer audio samples than the data chunk declares"
        )
    duration_sec = data_size / float(sample_rate * block_align)
    if data_size == 0 or duration_sec <= 0:
        raise VoiceInputError("WAV contains no audio samples")
    if duration_sec > MAX_AUDIO_DURATION_SEC:
        raise VoiceInputError(
            f"Audio duration {duration_sec:.1f}s exceeds limit of "
            f"{MAX_AUDIO_DURATION_SEC}s"
        )

    return {
        "duration_sec": duration_sec,
        "sample_rate": sample_rate,
        "channels": channels,
        "bits_per_sample": bits_per_sample,
        "data_size": data_size,
    }


def _silence_wav(duration_sec: float = 0.05, sample_rate: int = 16000) -> bytes:
    """Tiny valid mono PCM16 WAV used for STT warmup."""
    frames = max(1, int(sample_rate * duration_sec))
    data_size = frames * 2
    return b"".join(
        (
            b"RIFF",
            struct.pack("<I", 36 + data_size),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16),
            b"data",
            struct.pack("<I", data_size),
            b"\x00" * data_size,
        )
    )


class VoiceService:
    """Per-application voice backend owning one core SpeechProcessor."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._processor = SpeechProcessor(self.voice_config())
        self._stt_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="grid-voice-stt"
        )
        self._tts_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="grid-voice-tts"
        )
        self._busy_guard = threading.Lock()
        self._stt_busy = False
        self._tts_busy = False
        self._stt_ready = False
        self._tts_ready = False
        self._closed = False

    # ── Configuration ─────────────────────────────────────────────────────

    @property
    def processor(self) -> SpeechProcessor:
        return self._processor

    def _config_parent(self) -> Path:
        try:
            config_path = Path(str(getattr(self._runtime, "config_path", "") or ""))
        except Exception:
            config_path = Path()
        parent = config_path.parent if str(config_path) else Path()
        return parent if str(parent) else ROOT_DIR

    def voice_config(self) -> Dict[str, Any]:
        """Freshly read + merged voice config (defaults are never mutated)."""
        try:
            raw = self._runtime.config_dict().get("voice") or {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        config = _deep_merge(DEFAULT_VOICE_CONFIG, raw)

        tts_raw = raw.get("tts") if isinstance(raw.get("tts"), dict) else {}
        model_path = tts_raw.get("model_path")
        if model_path:
            path = Path(str(model_path))
            if not path.is_absolute():
                # Explicit relative paths resolve against the config file.
                path = self._config_parent() / path
            config["tts"]["model_path"] = str(path)
        else:
            # Default: repository-bundled Silero model.
            config["tts"]["model_path"] = str(ROOT_DIR / "speech-text" / "model.pt")
        return config

    def _refresh_processor_config(self) -> Dict[str, Any]:
        """Push the (possibly reloaded) config into the owned processor.

        Reloadable without restart: language, beam size, speaker, sample rate.
        Changing model/device of an already loaded model requires a restart.
        """
        config = self.voice_config()
        try:
            self._processor._config = config
        except Exception:  # pragma: no cover - mocked processors in tests
            pass
        return config

    # ── Status (no imports, no model loading) ─────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        config = self.voice_config()
        stt_cfg = config.get("stt", {})
        tts_cfg = config.get("tts", {})

        stt_available = _module_available("faster_whisper")
        torch_available = _module_available("torch")
        model_path = Path(str(tts_cfg.get("model_path", "")))
        tts_model_found = model_path.exists()
        backend = self._tts_backend(config)
        windows_available = backend == "windows" and windows_speech.available()
        openrouter_available = backend == "openrouter" and bool(
            os.getenv(str(tts_cfg.get("api_key_env", "OPENROUTER_API_KEY")))
            and tts_cfg.get("model") and tts_cfg.get("voice")
        )

        issues: list = []
        if not stt_available:
            issues.append(
                "faster-whisper is not installed; STT unavailable "
                "(pip install faster-whisper)"
            )
        if backend == "openrouter" and not openrouter_available:
            issues.append("OpenRouter TTS needs model, voice and its API key environment variable.")
        elif backend == "windows" and not windows_available:
            issues.append("Windows speech is unavailable; install a Russian Windows voice or configure Silero.")
        elif backend == "silero" and not torch_available:
            issues.append("torch is not installed; TTS unavailable (pip install torch)")
        elif backend == "silero" and not tts_model_found:
            issues.append(
                f"Silero TTS model not found at {model_path}; "
                "set voice.tts.model_path or place speech-text/model.pt"
            )
        if not config.get("enabled", True):
            issues.append("Voice features are disabled in configuration")

        status = {
            "enabled": bool(config.get("enabled", True)),
            "stt_available": stt_available,
            "tts_available": openrouter_available or windows_available or (backend == "silero" and torch_available and tts_model_found),
            "tts_backend": backend,
            "stt_ready": self._stt_ready,
            "tts_ready": self._tts_ready,
            "issues": issues,
            "stt_model": stt_cfg.get("model_size", "small"),
            "stt_device": stt_cfg.get("device", "cuda"),
            "tts_device": "remote" if backend == "openrouter" else tts_cfg.get("device", "cpu"),
        }
        return status

    def _tts_backend(self, config: Dict[str, Any]) -> str:
        tts = config.get("tts", {})
        backend = tts.get("backend", "silero")
        if backend == "auto":
            return "silero" if Path(tts["model_path"]).is_file() else ("windows" if windows_speech.available() else "silero")
        if backend not in {"silero", "windows", "openrouter"}:
            raise VoiceUnavailableError("voice.tts.backend must be auto, silero, windows or openrouter")
        return backend

    @staticmethod
    def _openrouter_synthesize(text: str, directory: str, tts: Dict[str, Any]) -> str:
        key_env = str(tts.get("api_key_env", "OPENROUTER_API_KEY"))
        api_key = os.getenv(key_env)
        if not api_key:
            raise VoiceUnavailableError(f"OpenRouter TTS API key is missing in {key_env}")
        spoken_text = text
        for source, pronunciation in (tts.get("pronunciations") or {}).items():
            spoken_text = re.sub(rf"(?<!\w){re.escape(str(source))}(?!\w)", str(pronunciation), spoken_text, flags=re.IGNORECASE)
        body = {
            "model": str(tts.get("model", "x-ai/grok-voice-tts-1.0")),
            "voice": str(tts.get("voice", "eve")),
            "input": spoken_text,
            "response_format": "mp3",
        }
        response = None
        with httpx.Client(timeout=float(tts.get("timeout", 60)), trust_env=False) as client:
            for attempt in range(4):
                response = client.post(
                    str(tts.get("base_url", OPENROUTER_TTS_URL)),
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
                if response.status_code not in TEMPORARY_TTS_STATUSES or attempt == 3:
                    break
                time.sleep(min(4.0, 0.5 * (2**attempt)))
        assert response is not None
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type != "audio/mpeg" or len(response.content) < 1024:
            raise RuntimeError(f"OpenRouter TTS returned {content_type or 'unknown content'} instead of MP3 audio")
        mp3_path = Path(directory) / "openrouter-speech.mp3"
        wav_path = Path(directory) / "openrouter-speech.wav"
        mp3_path.write_bytes(response.content)
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp3_path),
             "-ac", "1", "-ar", str(int(tts.get("sample_rate", 24000))), "-c:a", "pcm_s16le", str(wav_path)],
            capture_output=True, timeout=45,
        )
        if result.returncode or not wav_path.exists():
            detail = result.stderr.decode("utf-8", errors="replace")[-600:]
            raise RuntimeError(f"Could not decode OpenRouter TTS audio: {detail}")
        return str(wav_path)

    def _synthesize_file(self, text: str, directory: str) -> str:
        config = self._processor._config
        backend = self._tts_backend(config)
        if backend == "openrouter":
            return self._openrouter_synthesize(text, directory, config["tts"])
        if backend == "windows":
            return windows_speech.synthesize(text, directory, sample_rate=int(config["tts"].get("sample_rate", 24000)))
        return self._processor._sync_synthesize(text, directory, None, "wav")

    # ── Busy-slot bookkeeping (bounded backlog: exactly one slot per engine)

    def _try_acquire(self, engine: str) -> bool:
        with self._busy_guard:
            if getattr(self, f"_{engine}_busy") or self._closed:
                return False
            setattr(self, f"_{engine}_busy", True)
            return True

    def _release(self, engine: str) -> None:
        with self._busy_guard:
            setattr(self, f"_{engine}_busy", False)

    def is_busy(self, engine: str) -> bool:
        with self._busy_guard:
            return bool(getattr(self, f"_{engine}_busy"))

    async def _run_worker(self, engine: str, func: Callable[..., Any], *args: Any) -> Any:
        """Run ``func`` on the engine's single-thread executor.

        The busy slot is released in the worker thread's ``finally`` block, so
        it remains held until the native call returns even if the awaiting
        asyncio task is cancelled (``asyncio.shield`` keeps the concurrent
        future alive).
        """
        executor: ThreadPoolExecutor = getattr(self, f"_{engine}_executor")
        try:
            future = executor.submit(func, *args)
        except Exception:
            self._release(engine)
            raise
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            # Native work continues to completion in the worker thread; the
            # thread's finally block releases the slot.
            raise

    # ── Worker thread bodies (temp dir lifecycle fully inside the thread) ──

    def _stt_work(self, wav_bytes: bytes) -> Dict[str, Any]:
        try:
            with tempfile.TemporaryDirectory(prefix="grid-voice-stt-") as tmp:
                audio_path = str(Path(tmp) / "audio.wav")
                Path(audio_path).write_bytes(wav_bytes)
                started = time.monotonic()
                text = self._processor._sync_transcribe(audio_path)
                elapsed_ms = round((time.monotonic() - started) * 1000)
            self._stt_ready = True
            return {"text": text, "elapsed_ms": elapsed_ms}
        finally:
            self._release("stt")

    def _tts_work(self, text: str) -> bytes:
        try:
            with tempfile.TemporaryDirectory(prefix="grid-voice-tts-") as tmp:
                out_path = self._synthesize_file(text, tmp)
                wav_bytes = Path(str(out_path)).read_bytes()
            self._tts_ready = True
            return wav_bytes
        finally:
            self._release("tts")

    def _stt_warmup_work(self) -> Dict[str, Any]:
        try:
            with tempfile.TemporaryDirectory(prefix="grid-voice-stt-warmup-") as tmp:
                audio_path = str(Path(tmp) / "silence.wav")
                Path(audio_path).write_bytes(_silence_wav())
                started = time.monotonic()
                self._processor._sync_transcribe(audio_path)
                elapsed_ms = round((time.monotonic() - started) * 1000)
            self._stt_ready = True
            return {"warmed": True, "elapsed_ms": elapsed_ms}
        except Exception as exc:
            logger.warning("STT warmup failed: %s", exc)
            return {"warmed": False, "error": str(exc)}
        finally:
            self._release("stt")

    def _tts_warmup_work(self) -> Dict[str, Any]:
        try:
            with tempfile.TemporaryDirectory(prefix="grid-voice-tts-warmup-") as tmp:
                self._synthesize_file("Проверка.", tmp)
            self._tts_ready = True
            return {"warmed": True}
        except Exception as exc:
            logger.warning("TTS warmup failed: %s", exc)
            return {"warmed": False, "error": str(exc)}
        finally:
            self._release("tts")

    # ── Public operations ─────────────────────────────────────────────────

    def _ensure_enabled(self, config: Dict[str, Any]) -> None:
        if not config.get("enabled", True):
            raise VoiceDisabledError("Voice features are disabled in configuration")

    async def transcribe(self, wav_bytes: bytes) -> Dict[str, Any]:
        config = self._refresh_processor_config()
        self._ensure_enabled(config)
        if not _module_available("faster_whisper"):
            raise VoiceUnavailableError(
                "faster-whisper is not installed; STT unavailable "
                "(pip install faster-whisper)"
            )
        validate_wav(wav_bytes)
        if not self._try_acquire("stt"):
            raise VoiceBusyError("Speech recognition is busy; retry shortly")
        return await self._run_worker("stt", self._stt_work, wav_bytes)

    async def synthesize(self, text: str) -> bytes:
        config = self._refresh_processor_config()
        self._ensure_enabled(config)
        backend = self._tts_backend(config)
        if backend == "openrouter":
            tts = config.get("tts", {})
            if not os.getenv(str(tts.get("api_key_env", "OPENROUTER_API_KEY"))):
                raise VoiceUnavailableError("OpenRouter TTS API key is unavailable.")
        if backend == "windows" and not windows_speech.available():
            raise VoiceUnavailableError("Windows speech is unavailable.")
        if backend == "silero" and not _module_available("torch"):
            raise VoiceUnavailableError(
                "torch is not installed; TTS unavailable (pip install torch)"
            )
        model_path = Path(str(config.get("tts", {}).get("model_path", "")))
        if backend == "silero" and not model_path.exists():
            raise VoiceUnavailableError(
                f"Silero TTS model not found at {model_path}; "
                "set voice.tts.model_path or place speech-text/model.pt"
            )
        if not self._try_acquire("tts"):
            raise VoiceBusyError("Speech synthesis is busy; retry shortly")
        return await self._run_worker("tts", self._tts_work, text)

    async def warmup(self, warm_tts: bool = False) -> Dict[str, Any]:
        config = self._refresh_processor_config()
        self._ensure_enabled(config)
        if not self._try_acquire("stt"):
            raise VoiceBusyError("Speech recognition is busy; retry shortly")
        stt_result = await self._run_worker("stt", self._stt_warmup_work)
        tts_result: Optional[Dict[str, Any]] = None
        if warm_tts:
            if not self._try_acquire("tts"):
                raise VoiceBusyError("Speech synthesis is busy; retry shortly")
            tts_result = await self._run_worker("tts", self._tts_warmup_work)
        return {"stt": stt_result, "tts": tts_result}

    # ── Shutdown (never blocks the event loop) ────────────────────────────

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # wait=False: never blocks the event loop; running native calls are
        # allowed to finish and release their busy slots from their threads.
        for executor in (self._stt_executor, self._tts_executor):
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:  # pragma: no cover
                logger.exception("Failed to shut down voice executor")


def register_voice_routes(app: FastAPI, runtime: Any) -> VoiceService:
    """Register voice routes on ``app`` using ``runtime`` configuration.

    One service per app (stored on ``app.state.grid_voice_service``); calling
    this twice on the same app reuses the existing service. There is no
    global service cache.
    """
    service = getattr(app.state, "grid_voice_service", None)
    if service is not None:
        return service
    service = VoiceService(runtime)
    app.state.grid_voice_service = service
    from web_chat.voice_turns import register_turn_routes
    register_turn_routes(app, runtime)

    def _error_response(exc: Exception, fallback_detail: str) -> HTTPException:
        if isinstance(exc, VoiceInputError):
            return HTTPException(status_code=exc.status_code, detail=str(exc))
        if isinstance(exc, VoiceBusyError):
            return HTTPException(status_code=429, detail=str(exc))
        if isinstance(exc, VoiceDisabledError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, VoiceUnavailableError):
            return HTTPException(status_code=503, detail=str(exc))
        logger.exception("Voice operation failed")
        return HTTPException(status_code=503, detail=f"{fallback_detail}: {exc}")

    @app.get("/api/voice/status")
    async def voice_status() -> JSONResponse:
        return JSONResponse(service.get_status())

    @app.post("/api/voice/transcribe")
    async def voice_transcribe(request: Request) -> JSONResponse:
        content_type = (request.headers.get("content-type") or "").split(";")[0]
        content_type = content_type.strip().lower()
        if content_type == "multipart/form-data":
            raise HTTPException(
                status_code=400,
                detail="Expected raw audio/wav request bytes, not multipart form data",
            )
        if content_type and content_type not in TRANSCRIBE_CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported content type {content_type}; expected audio/wav",
            )

        chunks = []
        total = 0
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise VoiceInputError(
                        f"Audio payload exceeds {MAX_UPLOAD_SIZE} bytes", status_code=413
                    )
                chunks.append(chunk)
        except VoiceInputError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
        wav_bytes = b"".join(chunks)

        try:
            result = await service.transcribe(wav_bytes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error_response(exc, "Speech recognition failed") from exc
        return JSONResponse(result)

    @app.post("/api/voice/synthesize")
    async def voice_synthesize(payload: SynthesizeRequest) -> Response:
        try:
            wav_bytes = await service.synthesize(payload.text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error_response(exc, "Speech synthesis failed") from exc
        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={"Content-Disposition": 'inline; filename="speech.wav"'},
        )

    @app.post("/api/voice/warmup")
    async def voice_warmup(payload: Optional[WarmupRequest] = None) -> JSONResponse:
        warm_tts = bool(payload is not None and payload.tts)
        try:
            result = await service.warmup(warm_tts=warm_tts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error_response(exc, "Voice warmup failed") from exc
        return JSONResponse(result)

    @app.on_event("shutdown")
    async def _voice_shutdown() -> None:
        # shutdown(wait=False) is non-blocking; safe to call on the loop.
        service.close()

    return service
