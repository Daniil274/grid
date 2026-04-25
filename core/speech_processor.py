"""
SpeechProcessor — speech recognition and synthesis module for Telegram bot.

STT: faster-whisper (configurable model size, CUDA or CPU)
TTS: Silero v5_ru (model.pt, voices: xenia, aidar, kseniya, baya, eugene)

Lazy-load: models are loaded on first call, not at server startup.
Async-safe: CPU/GPU operations run in executor, without blocking the event loop.

Cold start optimization:
  warmup() — called on bot startup, preloads models + warms up
  JIT Silero so the first real request has no loading delay.
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("grid.speech_processor")

# ─── Transliteration from Latin to Cyrillic (for Russian Silero) ──────────────
try:
    from transliterate import translit as _translit

    def _latin_to_cyrillic(text: str) -> str:
        return _translit(text, "ru")

    _HAS_TRANSLITERATE = True
except ImportError:
    _HAS_TRANSLITERATE = False
    # Each Latin character → corresponding Cyrillic (strings of equal length: 44)
    _LATIN_TO_CYR = str.maketrans(
        "abvgdezijklmnoprstufhcABVGDEZIJKLMNOPRSTUFHC",
        "абвгдезийклмнопрстуфхцАБВГДЕЗИЙКЛМНОПРСТУФХЦ",
    )

    def _latin_to_cyrillic(text: str) -> str:
        s = text
        for lat, cyr in [
            ("sh", "ш"), ("ch", "ч"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"),
            ("zh", "ж"), ("ts", "ц"), ("Sh", "Ш"), ("Ch", "Ч"), ("Zh", "Ж"),
        ]:
            s = s.replace(lat, cyr)
        return s.translate(_LATIN_TO_CYR)


def prepare_text_for_tts(text: str) -> str:
    """Replaces Latin words with Cyrillic transliteration for Silero."""
    def replace_latin_word(m):
        word = m.group(0)
        if not re.search(r"[a-zA-Z]", word):
            return word
        return _latin_to_cyrillic(word)
    return re.sub(r"[a-zA-Z0-9]+", replace_latin_word, text)


# ─── Main class ─────────────────────────────────────────────────────────

class SpeechProcessor:
    """
    Wrapper over Whisper (STT) and Silero (TTS).

    Usage:
        sp = get_speech_processor(config["voice"])
        text = await sp.transcribe("/path/to/voice.ogg")
        audio = await sp.synthesize(text, "/path/to/out/dir")
    """

    def __init__(self, config: dict):
        self._config = config
        self._whisper = None    # lazy / warmed up via warmup()
        self._silero = None     # lazy / warmed up via warmup()

    # ── Private model loading methods ────────────────────────────────────

    def _load_whisper_model(self) -> None:
        """Load the Whisper model (idempotent)."""
        if self._whisper is not None:
            return
        logger.info("Loading Whisper model...")
        from faster_whisper import WhisperModel
        stt = self._config.get("stt", {})
        self._whisper = WhisperModel(
            stt.get("model_size", "large-v3"),
            device=stt.get("device", "cuda"),
            compute_type=stt.get("compute_type", "float16"),
        )
        logger.info("Whisper loaded.")

    def _wav_to_ogg(self, wav_path: str) -> Optional[str]:
        """
        Converts WAV to OGG Opus for Telegram voice messages.
        Tries pydub first, on error (e.g., pyaudioop in Python 3.13) — ffmpeg.
        """
        ogg_path = wav_path.replace(".wav", ".ogg")
        # 1) pydub
        try:
            from pydub import AudioSegment
            seg = AudioSegment.from_wav(wav_path)
            seg.export(ogg_path, format="ogg", codec="libopus")
            try:
                os.remove(wav_path)
            except Exception:
                pass
            return ogg_path
        except Exception as e:
            logger.debug("pydub WAV→OGG failed: %s", e)
        # 2) ffmpeg (sufficient for Telegram voice bubble)
        try:
            import subprocess
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path, "-c:a", "libopus", "-b:a", "64k", ogg_path],
                capture_output=True,
                timeout=30,
            )
            if r.returncode == 0 and Path(ogg_path).exists():
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
                return ogg_path
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug("ffmpeg WAV→OGG: %s", e)
        return None

    def _load_silero_model(self) -> None:
        """
        Loads the Silero TTS model and warms up JIT (idempotent).
        JIT compilation happens on first apply_tts() — we do it here
        with short text so the real user request doesn't slow down.
        """
        if self._silero is not None:
            return
        logger.info("Loading Silero TTS model...")
        import torch
        tts_cfg = self._config.get("tts", {})
        model_path = tts_cfg.get("model_path", "speech-text/model.pt")

        # Absolute path: relative to project root (next to core/)
        if not Path(model_path).is_absolute():
            model_path = Path(__file__).parent.parent / model_path

        if not Path(model_path).exists():
            raise FileNotFoundError(f"Silero model not found: {model_path}")

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._silero = torch.package.PackageImporter(str(model_path)).load_pickle(
            "tts_models", "model"
        )
        self._silero.to(device)
        logger.info(f"Silero TTS loaded, device={device}.")

        # JIT warmup: first apply_tts compiles the graph — do it now,
        # not during the user's request.
        logger.info("Warming up JIT Silero...")
        try:
            sample_rate = tts_cfg.get("sample_rate", 48000)
            self._silero.apply_tts(text="Hello.", speaker="xenia", sample_rate=sample_rate)
            logger.info("JIT Silero warmed up.")
        except Exception as e:
            logger.warning(f"JIT warmup failed (not critical): {e}")

    # ── STT ──────────────────────────────────────────────────────────────────

    async def transcribe(self, audio_path: str) -> str:
        """
        Transcribes speech from an audio file (OGG, WAV, MP3, ...).
        Runs Whisper in an executor, does not block the event loop.
        Returns an empty string on error — does not raise an exception.
        """
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self._sync_transcribe, audio_path)
            return result
        except Exception as e:
            logger.error(f"STT transcribe failed: {e}")
            return ""

    def _sync_transcribe(self, path: str) -> str:
        """Synchronous Whisper call (called in executor)."""
        self._load_whisper_model()
        segments, info = self._whisper.transcribe(path, beam_size=5)
        text = " ".join(s.text.strip() for s in segments).strip()
        logger.info(f"STT: '{info.language}' ({info.language_probability:.2f}) → {len(text)} chars")
        return text

    # ── TTS ──────────────────────────────────────────────────────────────────

    async def synthesize(self, text: str, out_dir: str, speaker: Optional[str] = None) -> str:
        """
        Synthesizes speech from text.
        Returns the path to the audio file (OGG Opus or WAV as fallback).
        Raises an exception on error (caller must handle).

        Args:
            text: Text to synthesize
            out_dir: Directory to save the file
            speaker: Voice (overrides config). None = use config.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_synthesize, text, out_dir, speaker)

    def _sync_synthesize(self, text: str, out_dir: str, speaker: Optional[str] = None) -> str:
        """Synchronous Silero call (called in executor)."""
        import numpy as np
        import re

        self._load_silero_model()

        tts_cfg = self._config.get("tts", {})
        effective_speaker = speaker or tts_cfg.get("speaker", "xenia")
        sample_rate = tts_cfg.get("sample_rate", 48000)

        prepared = prepare_text_for_tts(text).strip()
        
        # Split long text into chunks to avoid "Model couldn't generate your text, probably it's too long"
        # Max length for Silero ~800-1000 characters.
        max_chars = 800
        sentences = re.split(r'(?<=[.!?\n])\s+', prepared)
        chunks = []
        current_chunk = ""
        for s in sentences:
            if not s.strip():
                continue
            if len(current_chunk) + len(s) < max_chars:
                current_chunk += " " + s if current_chunk else s
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                # If a single sentence is longer than max_chars (rare, but just in case)
                if len(s) >= max_chars:
                    words = s.split()
                    temp_chunk = ""
                    for w in words:
                        if len(temp_chunk) + len(w) < max_chars:
                            temp_chunk += " " + w if temp_chunk else w
                        else:
                            chunks.append(temp_chunk.strip())
                            temp_chunk = w
                    current_chunk = temp_chunk
                else:
                    current_chunk = s
        if current_chunk:
            chunks.append(current_chunk.strip())

        audio_chunks = []
        for chunk in chunks:
            if not chunk.strip():
                continue
            try:
                chunk_audio = self._silero.apply_tts(
                    text=chunk,
                    speaker=effective_speaker,
                    sample_rate=sample_rate,
                )
                import torch as _torch
                if _torch.is_tensor(chunk_audio):
                    chunk_audio = chunk_audio.cpu().numpy()
                audio_chunks.append(np.asarray(chunk_audio, dtype=np.float32))
            except Exception as e:
                logger.warning(f"Chunk synthesis error '{chunk[:30]}...': {e}")

        if not audio_chunks:
            raise RuntimeError("Failed to synthesize any text chunk.")

        # Merge audio chunks with a short pause between them (e.g., 200 ms)
        pause = np.zeros(int(sample_rate * 0.2), dtype=np.float32)
        audio = audio_chunks[0]
        for ac in audio_chunks[1:]:
            audio = np.concatenate([audio, pause, ac])

        # Save WAV
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        wav_path = str(out_path / f"tts_{timestamp}.wav")

        try:
            import scipy.io.wavfile as wavfile
            audio_int16 = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            wavfile.write(wav_path, sample_rate, audio_int16)
        except ImportError:
            import soundfile as sf
            sf.write(wav_path, audio, sample_rate)

        # Convert to OGG Opus (Telegram voice message)
        ogg_path = self._wav_to_ogg(wav_path)
        if ogg_path:
            logger.info(f"TTS synthesis → OGG: {ogg_path}")
            return ogg_path
        logger.warning("OGG conversion unavailable (pydub and ffmpeg), sending WAV")
        return wav_path

    # ── TTS SSML ─────────────────────────────────────────────────────────────

    async def synthesize_ssml(self, ssml_text: str, out_dir: str, speaker: Optional[str] = None) -> str:
        """
        Synthesizes speech from SSML markup.
        Returns the path to the audio file (OGG Opus or WAV as fallback).
        Silero v5 supports: <speak>, <p>, <s>, <break>, <prosody rate|pitch>.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_synthesize_ssml, ssml_text, out_dir, speaker)

    def _sync_synthesize_ssml(self, ssml_text: str, out_dir: str, speaker: Optional[str] = None) -> str:
        """
        Synchronous SSML synthesis via model.save_wav(ssml_text=...).
        save_wav may return multiple WAV files (one per SSML segment) —
        they are merged via pydub, result is converted to OGG Opus.
        """
        self._load_silero_model()

        tts_cfg = self._config.get("tts", {})
        effective_speaker = speaker or tts_cfg.get("speaker", "xenia")
        sample_rate = tts_cfg.get("sample_rate", 48000)

        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        audio_base = str(out_path / f"ssml_{timestamp}.wav")

        # save_wav returns a list of paths (one file per paragraph/segment)
        wav_paths = self._silero.save_wav(
            ssml_text=ssml_text,
            speaker=effective_speaker,
            sample_rate=sample_rate,
            audio_path=audio_base,
        )

        if not isinstance(wav_paths, (list, tuple)):
            wav_paths = [str(wav_paths)]
        wav_paths = [str(p) for p in wav_paths]

        # Merge segments if there are multiple
        if len(wav_paths) == 1:
            merged_wav = wav_paths[0]
        else:
            merged_wav = audio_base.replace(".wav", "_merged.wav")
            try:
                from pydub import AudioSegment
                combined = sum(
                    (AudioSegment.from_wav(p) for p in wav_paths),
                    AudioSegment.empty(),
                )
                combined.export(merged_wav, format="wav")
                for p in wav_paths:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Failed to merge SSML segments ({e}), using first")
                merged_wav = wav_paths[0]

        logger.info(f"SSML TTS synthesis → WAV: {merged_wav}")

        # Convert to OGG Opus (Telegram voice message)
        ogg_path = self._wav_to_ogg(merged_wav)
        if ogg_path:
            logger.info(f"SSML TTS → OGG: {ogg_path}")
            return ogg_path
        logger.warning("OGG conversion unavailable (pydub and ffmpeg), sending WAV")
        return merged_wav

    # ── Warmup ─────────────────────────────────────────────────────────────

    async def warmup(self) -> None:
        """
        Loads STT and TTS models in a background thread at server startup.
        Eliminates delay on the user's first voice message.
        Errors are logged but not raised — the server starts regardless.
        """
        loop = asyncio.get_event_loop()

        logger.info("▶ Starting preload of voice models (STT + TTS)...")
        t0 = time.monotonic()

        # Whisper STT
        try:
            await loop.run_in_executor(None, self._load_whisper_model)
        except Exception as e:
            logger.warning(f"⚠ Whisper warmup failed: {e}")

        # Silero TTS (includes JIT warmup)
        try:
            await loop.run_in_executor(None, self._load_silero_model)
        except Exception as e:
            logger.warning(f"⚠ Silero warmup failed: {e}")

        elapsed = time.monotonic() - t0
        logger.info(f"✅ Voice models ready in {elapsed:.1f}s")

    @staticmethod
    def check_dependencies() -> dict:
        """
        Checks the availability of all STT and TTS dependencies.
        Returns a dictionary with results and prints warnings.
        """
        results = {
            "faster_whisper": False,
            "torch": False,
            "scipy": False,
            "pydub": False,
            "ffmpeg": False,
            "silero_model": False,
        }
        warnings = []

        try:
            import faster_whisper  # noqa
            results["faster_whisper"] = True
        except ImportError:
            warnings.append("faster-whisper not installed → STT unavailable (pip install faster-whisper)")

        try:
            import torch  # noqa
            results["torch"] = True
        except ImportError:
            warnings.append("torch not installed → TTS unavailable")

        try:
            import scipy  # noqa
            results["scipy"] = True
        except ImportError:
            warnings.append("scipy not installed → TTS may not work (pip install scipy)")

        try:
            import pydub  # noqa
            results["pydub"] = True
            # Check ffmpeg
            import subprocess
            r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=3)
            if r.returncode == 0:
                results["ffmpeg"] = True
            else:
                warnings.append("ffmpeg not found → TTS will send WAV instead of OGG (voice bubble)")
        except ImportError:
            warnings.append("pydub not installed → WAV→OGG conversion unavailable (pip install pydub)")
        except Exception:
            warnings.append("ffmpeg not found in PATH → TTS will send WAV instead of OGG (voice bubble)")

        if results["torch"]:
            from pathlib import Path as _Path
            import os as _os
            # Check model.pt
            default_path = _Path(__file__).parent.parent / "speech-text" / "model.pt"
            if default_path.exists():
                results["silero_model"] = True
            else:
                warnings.append(f"Silero model.pt not found: {default_path} → TTS unavailable")

        if warnings:
            logger.warning("⚠️  Voice subsystem: dependency issues detected:")
            for w in warnings:
                logger.warning(f"   • {w}")
        else:
            logger.info("✅ Voice subsystem: all dependencies available (STT + TTS ready)")

        return results


# ─── Global singleton ────────────────────────────────────────────────────

_processor: Optional[SpeechProcessor] = None


def get_speech_processor(config: dict) -> SpeechProcessor:
    """
    Returns the global SpeechProcessor singleton.
    On first call, creates it, checks dependencies, and logs status.
    """
    global _processor
    if _processor is None:
        _processor = SpeechProcessor(config)
        # Check dependencies immediately on creation — so issues are visible in logs
        _processor.check_dependencies()
    return _processor


def get_existing_speech_processor() -> Optional[SpeechProcessor]:
    """
    Returns the SpeechProcessor singleton if already created, otherwise None.
    Does not create a new instance. Used in agent tools
    to avoid re-reading config.yaml on every call.
    """
    return _processor
