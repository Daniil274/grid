"""Tests for the web chat voice backend (web_chat/voice.py).

Uses the real FastAPI routing stack (TestClient / httpx ASGI transport) with a
fully mocked core SpeechProcessor — no GPU, no model downloads, no network.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import struct
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web_chat.voice as voice_module
from web_chat.voice import (
    DEFAULT_VOICE_CONFIG,
    MAX_AUDIO_DURATION_SEC,
    MAX_TTS_TEXT_LENGTH,
    MAX_UPLOAD_SIZE,
    register_voice_routes,
    validate_wav,
)


# ─── Helpers ──────────────────────────────────────────────────────────────

def create_wav_file(
    sample_rate: int = 16000,
    duration_sec: float = 1.0,
    channels: int = 1,
    bits_per_sample: int = 16,
    truncate_bytes: int = 0,
) -> bytes:
    """Create a valid mono PCM16 WAV file (optionally truncated)."""
    num_frames = max(0, int(sample_rate * duration_sec))
    block_align = channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align
    data_size = num_frames * block_align

    wav = io.BytesIO()
    wav.write(b"RIFF")
    wav.write(struct.pack("<I", 36 + data_size))
    wav.write(b"WAVE")
    wav.write(b"fmt ")
    wav.write(struct.pack("<I", 16))
    wav.write(struct.pack("<H", 1))  # PCM
    wav.write(struct.pack("<H", channels))
    wav.write(struct.pack("<I", sample_rate))
    wav.write(struct.pack("<I", byte_rate))
    wav.write(struct.pack("<H", block_align))
    wav.write(struct.pack("<H", bits_per_sample))
    wav.write(b"data")
    wav.write(struct.pack("<I", data_size))
    wav.write(b"\x00" * data_size)

    data = wav.getvalue()
    if truncate_bytes:
        data = data[: max(0, len(data) - truncate_bytes)]
    return data


def make_riff_wav(data_bytes: int = 64) -> bytes:
    """Minimal RIFF/WAVE payload used as fake TTS output."""
    payload = b"\x01" * data_bytes
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(payload))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        + b"data"
        + struct.pack("<I", len(payload))
        + payload
    )


# ─── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def runtime(tmp_path):
    """Mocked WebChatRuntime with a valid voice config and model file."""
    model_path = tmp_path / "models" / "model.pt"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"fake-silero-model")

    rt = MagicMock(name="runtime")
    rt.config_path = tmp_path / "config.yaml"
    rt.config_dict.return_value = {
        "voice": {
            "enabled": True,
            "stt": {
                "model_size": "small",
                "device": "cpu",
                "language": "ru",
                "beam_size": 1,
            },
            "tts": {
                "model_path": str(model_path),
                "device": "cpu",
                "speaker": "xenia",
                "sample_rate": 24000,
            },
        }
    }
    return rt


@pytest.fixture
def deps_available():
    """Pretend torch / faster_whisper are importable without importing them."""
    with patch.object(voice_module, "_module_available", return_value=True):
        yield


@pytest.fixture
def processor_factory():
    """Build a mocked core SpeechProcessor with realistic sync methods."""
    created = []

    def factory() -> MagicMock:
        proc = MagicMock(name="SpeechProcessor")
        proc.transcribed_paths = []
        proc.synthesized_calls = []

        def sync_transcribe(path: str) -> str:
            proc.transcribed_paths.append(path)
            return "mock transcription"

        def sync_synthesize(text, out_dir, speaker=None, format="ogg"):
            proc.synthesized_calls.append((text, out_dir, format))
            out = Path(out_dir) / "speech.wav"
            out.write_bytes(make_riff_wav())
            return str(out)

        proc._sync_transcribe.side_effect = sync_transcribe
        proc._sync_synthesize.side_effect = sync_synthesize
        created.append(proc)
        return proc

    yield factory


@pytest.fixture
def voice_app(runtime, processor_factory, deps_available):
    """FastAPI app with voice routes and a mocked Processor."""
    proc = processor_factory()
    with patch.object(voice_module, "SpeechProcessor", return_value=proc) as mock_cls:
        app = FastAPI()
        service = register_voice_routes(app, runtime)
        app.state.test_processor = proc
        app.state.test_processor_class = mock_cls
        app.state.test_service = service
        yield app


# ─── WAV validation ───────────────────────────────────────────────────────

def test_auto_windows_fallback_uses_local_synthesizer(voice_app, runtime, monkeypatch):
    cfg = runtime.config_dict.return_value["voice"]["tts"]
    cfg["backend"] = "auto"
    Path(cfg["model_path"]).unlink()
    monkeypatch.setattr(voice_module.windows_speech, "available", lambda: True)
    paths = []

    def synth(text, directory, **kwargs):
        path = Path(directory) / "windows.wav"
        path.write_bytes(make_riff_wav())
        paths.append(path)
        return str(path)

    monkeypatch.setattr(voice_module.windows_speech, "synthesize", synth)
    with TestClient(voice_app) as client:
        status = client.get("/api/voice/status").json()
        assert status["tts_available"] and status["tts_backend"] == "windows"
        result = client.post("/api/voice/synthesize", json={"text": "Hello"})
        assert result.status_code == 200 and result.content.startswith(b"RIFF")
    assert paths and not paths[0].exists()


def test_explicit_silero_never_silently_switches_backend(voice_app, runtime, monkeypatch):
    cfg = runtime.config_dict.return_value["voice"]["tts"]
    cfg["backend"] = "silero"
    Path(cfg["model_path"]).unlink()
    monkeypatch.setattr(voice_module.windows_speech, "available", lambda: True)
    with TestClient(voice_app) as client:
        assert client.get("/api/voice/status").json()["tts_available"] is False
        assert client.post("/api/voice/synthesize", json={"text": "Hello"}).status_code == 503


def test_openrouter_backend_reports_remote_and_returns_decoded_wav(voice_app, runtime, monkeypatch):
    cfg = runtime.config_dict.return_value["voice"]["tts"]
    cfg.update({
        "backend": "openrouter",
        "model": "x-ai/grok-voice-tts-1.0",
        "voice": "eve",
        "api_key_env": "TEST_OPENROUTER_KEY",
    })
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    calls = []

    def synth(text, directory, tts):
        calls.append((text, tts["model"], tts["voice"]))
        path = Path(directory) / "remote.wav"
        path.write_bytes(make_riff_wav())
        return str(path)

    monkeypatch.setattr(voice_app.state.test_service, "_openrouter_synthesize", synth)
    with TestClient(voice_app) as client:
        status = client.get("/api/voice/status").json()
        assert status["tts_available"] is True
        assert status["tts_backend"] == "openrouter"
        assert status["tts_device"] == "remote"
        response = client.post("/api/voice/synthesize", json={"text": "Привет"})
    assert response.status_code == 200 and response.content.startswith(b"RIFF")
    assert calls == [("Привет", "x-ai/grok-voice-tts-1.0", "eve")]


def test_openrouter_backend_requires_configured_key(voice_app, runtime, monkeypatch):
    cfg = runtime.config_dict.return_value["voice"]["tts"]
    cfg.update({"backend": "openrouter", "model": "x-ai/grok-voice-tts-1.0", "voice": "eve", "api_key_env": "MISSING_TTS_KEY"})
    monkeypatch.delenv("MISSING_TTS_KEY", raising=False)
    with TestClient(voice_app) as client:
        status = client.get("/api/voice/status").json()
        assert status["tts_available"] is False
        assert client.post("/api/voice/synthesize", json={"text": "Привет"}).status_code == 503

class TestValidateWav:
    def test_valid(self):
        result = validate_wav(create_wav_file(sample_rate=16000, duration_sec=2))
        assert result["channels"] == 1
        assert result["bits_per_sample"] == 16
        assert result["sample_rate"] == 16000
        assert result["duration_sec"] == pytest.approx(2.0)

    def test_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            validate_wav(b"RIFF")

    def test_invalid_header(self):
        with pytest.raises(ValueError, match="RIFF"):
            validate_wav(b"XXXX" + b"\x00" * 40)

    def test_truncated(self):
        wav = create_wav_file(duration_sec=1)
        with pytest.raises(ValueError, match="Truncated"):
            validate_wav(wav[: len(wav) - 100])

    def test_stereo_rejected(self):
        with pytest.raises(ValueError, match="mono"):
            validate_wav(create_wav_file(channels=2))

    def test_non_16bit_rejected(self):
        with pytest.raises(ValueError, match="16-bit"):
            validate_wav(create_wav_file(bits_per_sample=8))

    @pytest.mark.parametrize("rate", [7999, 48001])
    def test_sample_rate_out_of_range(self, rate):
        with pytest.raises(ValueError, match="sample rate"):
            validate_wav(create_wav_file(sample_rate=rate))

    def test_duration_exceeds_limit(self):
        wav = create_wav_file(sample_rate=8000, duration_sec=MAX_AUDIO_DURATION_SEC + 1)
        with pytest.raises(ValueError, match="duration"):
            validate_wav(wav)

    def test_no_samples(self):
        with pytest.raises(ValueError, match="no audio samples"):
            validate_wav(create_wav_file(duration_sec=0))

    def test_size_exceeds_limit(self):
        with pytest.raises(ValueError) as excinfo:
            validate_wav(b"\x00" * (MAX_UPLOAD_SIZE + 1))
        assert excinfo.value.status_code == 413


# ─── Status ───────────────────────────────────────────────────────────────

class TestStatus:
    def test_status_fields(self, voice_app):
        client = TestClient(voice_app)
        resp = client.get("/api/voice/status")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "enabled",
            "stt_available",
            "tts_available",
            "stt_ready",
            "tts_ready",
            "issues",
            "stt_model",
            "stt_device",
            "tts_device",
        ):
            assert key in data
        assert data["enabled"] is True
        assert data["stt_available"] is True
        assert data["tts_available"] is True
        assert data["stt_ready"] is False
        assert data["tts_ready"] is False
        assert data["issues"] == []
        assert data["stt_model"] == "small"
        assert data["stt_device"] == "cpu"
        assert data["tts_device"] == "cpu"

    def test_status_missing_deps(self, voice_app):
        with patch.object(voice_module, "_module_available", return_value=False):
            client = TestClient(voice_app)
            data = client.get("/api/voice/status").json()
        assert data["stt_available"] is False
        assert data["tts_available"] is False
        joined = " ".join(data["issues"])
        assert "faster-whisper" in joined
        assert "torch" in joined

    def test_status_missing_model(self, voice_app, runtime):
        missing = Path(runtime.config_path).parent / "nope" / "model.pt"
        cfg = runtime.config_dict.return_value
        cfg["voice"]["tts"]["model_path"] = str(missing)
        client = TestClient(voice_app)
        data = client.get("/api/voice/status").json()
        assert data["tts_available"] is False
        assert str(missing) in " ".join(data["issues"])

    def test_status_does_not_import_heavy_modules(self, voice_app):
        service = voice_app.state.test_service
        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            root = name.split(".")[0]
            if root in ("torch", "faster_whisper"):
                raise AssertionError(f"status imported heavy module: {name}")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded):
            status = service.get_status()
        assert status["enabled"] is True

    def test_status_after_transcribe_marks_stt_ready(self, voice_app):
        client = TestClient(voice_app)
        wav = create_wav_file()
        assert client.post(
            "/api/voice/transcribe", content=wav, headers={"content-type": "audio/wav"}
        ).status_code == 200
        data = client.get("/api/voice/status").json()
        assert data["stt_ready"] is True
        assert data["tts_ready"] is False

    def test_no_model_load_at_startup_or_status(self, voice_app):
        proc = voice_app.state.test_processor
        proc._sync_transcribe.assert_not_called()
        proc._sync_synthesize.assert_not_called()
        client = TestClient(voice_app)
        assert client.get("/api/voice/status").status_code == 200
        proc._sync_transcribe.assert_not_called()
        proc._sync_synthesize.assert_not_called()


# ─── Transcribe ───────────────────────────────────────────────────────────

class TestTranscribe:
    def test_valid_raw_wav(self, voice_app):
        proc = voice_app.state.test_processor
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            content=create_wav_file(),
            headers={"content-type": "audio/wav"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["text"] == "mock transcription"
        assert isinstance(data["elapsed_ms"], int)
        # TemporaryDirectory lifecycle is inside the thread: cleaned up already.
        assert proc.transcribed_paths, "processor was not invoked"
        assert not Path(proc.transcribed_paths[0]).exists()
        assert not Path(proc.transcribed_paths[0]).parent.exists()

    def test_octet_stream_allowed(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            content=create_wav_file(),
            headers={"content-type": "application/octet-stream"},
        )
        assert resp.status_code == 200

    def test_multipart_rejected(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            files={"file": ("test.wav", create_wav_file(), "audio/wav")},
        )
        assert resp.status_code == 400
        assert "multipart" in resp.json()["detail"].lower()

    def test_wrong_content_type_rejected(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            content=create_wav_file(),
            headers={"content-type": "text/plain"},
        )
        assert resp.status_code == 400

    def test_invalid_wav_body(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            content=b"this is not a wav file",
            headers={"content-type": "audio/wav"},
        )
        assert resp.status_code == 400

    def test_truncated_wav(self, voice_app):
        client = TestClient(voice_app)
        wav = create_wav_file(duration_sec=1)
        resp = client.post(
            "/api/voice/transcribe",
            content=wav[: len(wav) - 50],
            headers={"content-type": "audio/wav"},
        )
        assert resp.status_code == 400
        assert "truncated" in resp.json()["detail"].lower()

    def test_duration_exceeds_limit(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            content=create_wav_file(
                sample_rate=8000, duration_sec=MAX_AUDIO_DURATION_SEC + 1
            ),
            headers={"content-type": "audio/wav"},
        )
        assert resp.status_code == 400
        assert "duration" in resp.json()["detail"].lower()

    def test_payload_too_large(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            content=b"\x00" * (MAX_UPLOAD_SIZE + 1),
            headers={"content-type": "audio/wav"},
        )
        assert resp.status_code == 413

    def test_stereo_rejected(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            content=create_wav_file(channels=2),
            headers={"content-type": "audio/wav"},
        )
        assert resp.status_code == 400
        assert "mono" in resp.json()["detail"].lower()

    def test_inference_error_returns_503(self, voice_app):
        proc = voice_app.state.test_processor
        proc._sync_transcribe.side_effect = RuntimeError("whisper exploded")
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            content=create_wav_file(),
            headers={"content-type": "audio/wav"},
        )
        assert resp.status_code == 503
        assert "whisper exploded" in resp.json()["detail"]

    def test_disabled_returns_403(self, voice_app, runtime):
        runtime.config_dict.return_value["voice"]["enabled"] = False
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/transcribe",
            content=create_wav_file(),
            headers={"content-type": "audio/wav"},
        )
        assert resp.status_code == 403
        assert client.get("/api/voice/status").json()["enabled"] is False

    def test_missing_stt_dependency_returns_503(self, voice_app):
        with patch.object(voice_module, "_module_available", return_value=False):
            client = TestClient(voice_app)
            resp = client.post(
                "/api/voice/transcribe",
                content=create_wav_file(),
                headers={"content-type": "audio/wav"},
            )
        assert resp.status_code == 503
        assert "faster-whisper" in resp.json()["detail"]


# ─── Synthesize ───────────────────────────────────────────────────────────

class TestSynthesize:
    def test_returns_riff_wav(self, voice_app):
        proc = voice_app.state.test_processor
        client = TestClient(voice_app)
        resp = client.post("/api/voice/synthesize", json={"text": "Привет мир"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/wav")
        assert resp.content[:4] == b"RIFF"
        # Web backend must request raw WAV, not the Telegram OGG default.
        assert proc.synthesized_calls
        text, out_dir, fmt = proc.synthesized_calls[0]
        assert text == "Привет мир"
        assert fmt == "wav"
        assert not Path(out_dir).exists()  # temp dir cleaned inside thread

    def test_missing_text_is_422(self, voice_app):
        client = TestClient(voice_app)
        assert client.post("/api/voice/synthesize", json={}).status_code == 422

    def test_invalid_json_is_422(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/synthesize",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422

    def test_text_too_long_is_422(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post(
            "/api/voice/synthesize", json={"text": "x" * (MAX_TTS_TEXT_LENGTH + 1)}
        )
        assert resp.status_code == 422

    def test_empty_text_is_422(self, voice_app):
        client = TestClient(voice_app)
        resp = client.post("/api/voice/synthesize", json={"text": ""})
        assert resp.status_code == 422

    def test_missing_model_returns_actionable_503(self, voice_app, runtime):
        missing = Path(runtime.config_path).parent / "gone" / "model.pt"
        runtime.config_dict.return_value["voice"]["tts"]["model_path"] = str(missing)
        client = TestClient(voice_app)
        resp = client.post("/api/voice/synthesize", json={"text": "hello"})
        assert resp.status_code == 503
        assert str(missing) in resp.json()["detail"]

    def test_inference_error_returns_503(self, voice_app):
        proc = voice_app.state.test_processor
        proc._sync_synthesize.side_effect = RuntimeError("silero exploded")
        client = TestClient(voice_app)
        resp = client.post("/api/voice/synthesize", json={"text": "hello"})
        assert resp.status_code == 503
        assert "silero exploded" in resp.json()["detail"]

    def test_disabled_returns_403(self, voice_app, runtime):
        runtime.config_dict.return_value["voice"]["enabled"] = False
        client = TestClient(voice_app)
        resp = client.post("/api/voice/synthesize", json={"text": "hello"})
        assert resp.status_code == 403

    def test_tts_ready_after_synthesis(self, voice_app):
        client = TestClient(voice_app)
        assert (
            client.post("/api/voice/synthesize", json={"text": "hi"}).status_code == 200
        )
        assert client.get("/api/voice/status").json()["tts_ready"] is True


# ─── Warmup ───────────────────────────────────────────────────────────────

class TestWarmup:
    def test_default_warms_stt_only(self, voice_app):
        proc = voice_app.state.test_processor
        client = TestClient(voice_app)
        resp = client.post("/api/voice/warmup")
        assert resp.status_code == 200
        data = resp.json()
        assert data["stt"]["warmed"] is True
        assert data["tts"] is None
        proc._sync_transcribe.assert_called_once()
        proc._sync_synthesize.assert_not_called()
        # warmup uses a real temp wav that is cleaned up afterwards
        path = proc.transcribed_paths[0]
        assert not Path(path).exists()

    def test_optional_tts_warmup(self, voice_app):
        proc = voice_app.state.test_processor
        client = TestClient(voice_app)
        resp = client.post("/api/voice/warmup", json={"tts": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["stt"]["warmed"] is True
        assert data["tts"]["warmed"] is True
        proc._sync_transcribe.assert_called_once()
        assert proc._sync_synthesize.call_count == 1
        # STT and TTS run on separate single-thread executors.
        service = voice_app.state.test_service
        assert service._stt_executor is not service._tts_executor

    def test_warmup_failure_reported_not_raised(self, voice_app):
        proc = voice_app.state.test_processor
        proc._sync_transcribe.side_effect = RuntimeError("load failed")
        client = TestClient(voice_app)
        resp = client.post("/api/voice/warmup", json={"tts": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["stt"]["warmed"] is False
        assert "load failed" in data["stt"]["error"]

    def test_disabled_returns_403(self, voice_app, runtime):
        runtime.config_dict.return_value["voice"]["enabled"] = False
        client = TestClient(voice_app)
        assert client.post("/api/voice/warmup").status_code == 403


# ─── Busy / cancellation / tempfile lifetime (ASGI transport) ─────────────

class TestBusyAndCancel:
    def test_busy_returns_429_and_cancel_is_cancel_safe(self, voice_app):
        proc = voice_app.state.test_processor
        service = voice_app.state.test_service
        url = "/api/voice/transcribe"
        headers = {"content-type": "audio/wav"}
        wav = create_wav_file()

        started = threading.Event()
        release = threading.Event()
        recorded = {}

        def slow_transcribe(path: str) -> str:
            recorded["path"] = path
            started.set()
            assert release.wait(10), "test timed out waiting for release"
            return "late result"

        proc._sync_transcribe.side_effect = slow_transcribe

        async def scenario() -> None:
            transport = httpx.ASGITransport(app=voice_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                task = asyncio.create_task(
                    client.post(url, content=wav, headers=headers)
                )
                # Wait until the native (mocked) thread work has started.
                assert await asyncio.to_thread(started.wait, 10)

                # While the single STT slot is busy, the next request gets 429.
                busy_resp = await client.post(url, content=wav, headers=headers)
                assert busy_resp.status_code == 429

                # Cancel the in-flight request: the native thread must keep
                # running (busy slot stays held) and clean its temp dir.
                task.cancel()
                with pytest.raises((asyncio.CancelledError, httpx.RequestError)):
                    await task
                assert service.is_busy("stt")

                release.set()
                deadline = time.monotonic() + 10
                while service.is_busy("stt") and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                assert not service.is_busy("stt")
                # TemporaryDirectory removed even though the request was cancelled.
                assert not Path(recorded["path"]).exists()
                assert not Path(recorded["path"]).parent.exists()

                # Slot is free again: a new request succeeds.
                proc._sync_transcribe.side_effect = lambda path: "mock transcription"
                ok = await client.post(url, content=wav, headers=headers)
                assert ok.status_code == 200
                assert ok.json()["text"] == "mock transcription"

        asyncio.run(scenario())

    def test_tts_busy_returns_429(self, voice_app):
        proc = voice_app.state.test_processor
        service = voice_app.state.test_service
        started = threading.Event()
        release = threading.Event()

        def slow_synthesize(text, out_dir, speaker=None, format="ogg"):
            started.set()
            assert release.wait(10)
            out = Path(out_dir) / "speech.wav"
            out.write_bytes(make_riff_wav())
            return str(out)

        proc._sync_synthesize.side_effect = slow_synthesize

        async def scenario() -> None:
            transport = httpx.ASGITransport(app=voice_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                task = asyncio.create_task(
                    client.post("/api/voice/synthesize", json={"text": "hello"})
                )
                assert await asyncio.to_thread(started.wait, 10)
                busy = await client.post("/api/voice/synthesize", json={"text": "again"})
                assert busy.status_code == 429
                release.set()
                resp = await task
                assert resp.status_code == 200
                assert resp.content[:4] == b"RIFF"
                assert not service.is_busy("tts")

        asyncio.run(scenario())

    def test_shutdown_closes_executors_without_blocking(self, voice_app):
        service = voice_app.state.test_service
        with TestClient(voice_app) as client:
            assert client.get("/api/voice/status").status_code == 200
        assert service._closed is True
        assert service._stt_executor._shutdown
        assert service._tts_executor._shutdown
        # close() is idempotent and never blocks the event loop.
        service.close()


# ─── Service isolation and configuration ──────────────────────────────────

class TestServiceAndConfig:
    def test_one_service_per_app_no_global_cache(
        self, runtime, processor_factory, deps_available
    ):
        proc = processor_factory()
        with patch.object(
            voice_module, "SpeechProcessor", return_value=proc
        ) as mock_cls:
            app = FastAPI()
            service = register_voice_routes(app, runtime)
            # Re-registering on the same app reuses the service.
            assert register_voice_routes(app, runtime) is service
            assert mock_cls.call_count == 1

            other_rt = MagicMock()
            other_rt.config_path = runtime.config_path
            other_rt.config_dict.return_value = runtime.config_dict.return_value
            other_app = FastAPI()
            other_service = register_voice_routes(other_app, other_rt)
            assert other_service is not service
            assert mock_cls.call_count == 2

    def test_partial_config_deep_merge_no_default_mutation(
        self, runtime, processor_factory, deps_available
    ):
        defaults_snapshot = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in DEFAULT_VOICE_CONFIG.items()
        }
        runtime.config_dict.return_value = {"voice": {"stt": {"language": "en"}}}
        proc = processor_factory()
        with patch.object(voice_module, "SpeechProcessor", return_value=proc):
            app = FastAPI()
            register_voice_routes(app, runtime)
        client = TestClient(app)
        data = client.get("/api/voice/status").json()
        assert data["stt_model"] == "small"  # default preserved
        assert data["stt_device"] == "cuda"  # default preserved
        assert data["tts_device"] == "cpu"
        # Defaults not mutated by the partial override.
        assert (
            DEFAULT_VOICE_CONFIG["stt"]["language"]
            == defaults_snapshot["stt"]["language"]
        )
        assert (
            DEFAULT_VOICE_CONFIG["stt"]["model_size"]
            == defaults_snapshot["stt"]["model_size"]
        )

    def test_relative_model_path_resolved_against_config_parent(
        self, runtime, processor_factory, deps_available
    ):
        model_rel = Path("voice") / "model.pt"
        model_file = runtime.config_path.parent / model_rel
        model_file.parent.mkdir(parents=True, exist_ok=True)
        model_file.write_bytes(b"fake")
        runtime.config_dict.return_value = {
            "voice": {"tts": {"model_path": str(model_rel)}}
        }
        proc = processor_factory()
        with patch.object(voice_module, "SpeechProcessor", return_value=proc):
            app = FastAPI()
            register_voice_routes(app, runtime)
        client = TestClient(app)
        data = client.get("/api/voice/status").json()
        assert data["tts_available"] is True
        assert data["issues"] == []

    def test_default_model_path_points_to_repo(
        self, runtime, processor_factory, deps_available
    ):
        runtime.config_dict.return_value = {"voice": {}}
        proc = processor_factory()
        with patch.object(voice_module, "SpeechProcessor", return_value=proc):
            app = FastAPI()
            service = register_voice_routes(app, runtime)
        cfg = service.voice_config()
        expected = voice_module.ROOT_DIR / "speech-text" / "model.pt"
        assert cfg["tts"]["model_path"] == str(expected)

    def test_config_reload_is_lazy(self, voice_app, runtime):
        client = TestClient(voice_app)
        runtime.config_dict.return_value["voice"]["stt"]["language"] = "en"
        resp = client.post(
            "/api/voice/transcribe",
            content=create_wav_file(),
            headers={"content-type": "audio/wav"},
        )
        assert resp.status_code == 200
        proc = voice_app.state.test_processor
        assert proc._config["stt"]["language"] == "en"
        assert proc._config["tts"]["speaker"] == "xenia"
