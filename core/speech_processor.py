"""
SpeechProcessor — модуль распознавания и синтеза речи для Telegram бота.

STT: faster-whisper (configurable model size, CUDA или CPU)
TTS: Silero v5_ru (model.pt, голоса: xenia, aidar, kseniya, baya, eugene)

Lazy-load: модели грузятся при первом вызове, не при старте сервера.
Async-safe: CPU/GPU операции выполняются в executor, не блокируя event loop.

Оптимизация холодного старта:
  warmup() — вызывается при старте бота, загружает модели заранее + прогревает
  JIT Silero, чтобы первый реальный запрос был без задержки загрузки.
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("grid.speech_processor")

# ─── Транслитерация латиницы в кириллицу (для русской Silero) ──────────────
try:
    from transliterate import translit as _translit

    def _latin_to_cyrillic(text: str) -> str:
        return _translit(text, "ru")

    _HAS_TRANSLITERATE = True
except ImportError:
    _HAS_TRANSLITERATE = False
    # Каждый символ Latin → соответствующий Cyrillic (строки одинаковой длины: 44)
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
    """Заменяет латинские слова кириллической транслитерацией для Silero."""
    def replace_latin_word(m):
        word = m.group(0)
        if not re.search(r"[a-zA-Z]", word):
            return word
        return _latin_to_cyrillic(word)
    return re.sub(r"[a-zA-Z0-9]+", replace_latin_word, text)


# ─── Основной класс ─────────────────────────────────────────────────────────

class SpeechProcessor:
    """
    Обёртка над Whisper (STT) и Silero (TTS).

    Использование:
        sp = get_speech_processor(config["voice"])
        text = await sp.transcribe("/path/to/voice.ogg")
        audio = await sp.synthesize(text, "/path/to/out/dir")
    """

    def __init__(self, config: dict):
        self._config = config
        self._whisper = None    # lazy / прогревается через warmup()
        self._silero = None     # lazy / прогревается через warmup()

    # ── Приватные методы загрузки моделей ────────────────────────────────────

    def _load_whisper_model(self) -> None:
        """Загружает модель Whisper (idempotent)."""
        if self._whisper is not None:
            return
        logger.info("Загружаю модель Whisper...")
        from faster_whisper import WhisperModel
        stt = self._config.get("stt", {})
        self._whisper = WhisperModel(
            stt.get("model_size", "large-v3"),
            device=stt.get("device", "cuda"),
            compute_type=stt.get("compute_type", "float16"),
        )
        logger.info("Whisper загружен.")

    def _wav_to_ogg(self, wav_path: str) -> Optional[str]:
        """
        Конвертирует WAV в OGG Opus для голосового сообщения в Telegram.
        Сначала пробует pydub, при ошибке (например pyaudioop в Python 3.13) — ffmpeg.
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
            logger.debug("pydub WAV→OGG не удался: %s", e)
        # 2) ffmpeg (достаточно для голосового пузыря в Telegram)
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
        Загружает модель Silero TTS и прогревает JIT (idempotent).
        JIT-компиляция происходит при первом apply_tts() — выполняем её здесь
        с коротким текстом, чтобы реальный запрос пользователя не тормозил.
        """
        if self._silero is not None:
            return
        logger.info("Загружаю модель Silero TTS...")
        import torch
        tts_cfg = self._config.get("tts", {})
        model_path = tts_cfg.get("model_path", "speech-text/model.pt")

        # Абсолютный путь: относительно корня проекта (рядом с core/)
        if not Path(model_path).is_absolute():
            model_path = Path(__file__).parent.parent / model_path

        if not Path(model_path).exists():
            raise FileNotFoundError(f"Silero model не найден: {model_path}")

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._silero = torch.package.PackageImporter(str(model_path)).load_pickle(
            "tts_models", "model"
        )
        self._silero.to(device)
        logger.info(f"Silero TTS загружен, device={device}.")

        # Прогрев JIT: первый apply_tts компилирует граф — делаем это сейчас,
        # а не во время запроса пользователя.
        logger.info("Прогрев JIT Silero...")
        try:
            sample_rate = tts_cfg.get("sample_rate", 48000)
            self._silero.apply_tts(text="Привет.", speaker="xenia", sample_rate=sample_rate)
            logger.info("JIT Silero прогрет.")
        except Exception as e:
            logger.warning(f"JIT-прогрев не удался (не критично): {e}")

    # ── STT ──────────────────────────────────────────────────────────────────

    async def transcribe(self, audio_path: str) -> str:
        """
        Распознаёт речь из аудиофайла (OGG, WAV, MP3, ...).
        Запускает Whisper в executor, не блокирует event loop.
        Возвращает пустую строку при ошибке — не бросает исключение.
        """
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self._sync_transcribe, audio_path)
            return result
        except Exception as e:
            logger.error(f"STT transcribe failed: {e}")
            return ""

    def _sync_transcribe(self, path: str) -> str:
        """Синхронный вызов Whisper (вызывается в executor)."""
        self._load_whisper_model()
        segments, info = self._whisper.transcribe(path, beam_size=5)
        text = " ".join(s.text.strip() for s in segments).strip()
        logger.info(f"STT: '{info.language}' ({info.language_probability:.2f}) → {len(text)} chars")
        return text

    # ── TTS ──────────────────────────────────────────────────────────────────

    async def synthesize(self, text: str, out_dir: str, speaker: Optional[str] = None) -> str:
        """
        Синтезирует речь из текста.
        Возвращает путь к аудиофайлу (OGG Opus или WAV как fallback).
        Бросает исключение при ошибке (вызывающий код должен обработать).

        Args:
            text: Текст для синтеза
            out_dir: Директория для сохранения файла
            speaker: Голос (переопределяет config). None = брать из config.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_synthesize, text, out_dir, speaker)

    def _sync_synthesize(self, text: str, out_dir: str, speaker: Optional[str] = None) -> str:
        """Синхронный вызов Silero (вызывается в executor)."""
        import numpy as np
        import re

        self._load_silero_model()

        tts_cfg = self._config.get("tts", {})
        effective_speaker = speaker or tts_cfg.get("speaker", "xenia")
        sample_rate = tts_cfg.get("sample_rate", 48000)

        prepared = prepare_text_for_tts(text).strip()
        
        # Разбиваем длинный текст на чанки, чтобы избежать "Model couldn't generate your text, probably it's too long"
        # Максимальная длина для Silero ~800-1000 символов.
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
                # Если одно предложение длиннее max_chars (бывает редко, но на всякий случай)
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
                logger.warning(f"Ошибка синтеза чанка '{chunk[:30]}...': {e}")

        if not audio_chunks:
            raise RuntimeError("Не удалось синтезировать ни один чанк текста.")

        # Объединяем аудио чанки с небольшой паузой между ними (например, 200 мс)
        pause = np.zeros(int(sample_rate * 0.2), dtype=np.float32)
        audio = audio_chunks[0]
        for ac in audio_chunks[1:]:
            audio = np.concatenate([audio, pause, ac])

        # Сохранить WAV
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

        # Конвертировать в OGG Opus (голосовое сообщение в Telegram)
        ogg_path = self._wav_to_ogg(wav_path)
        if ogg_path:
            logger.info(f"TTS синтез → OGG: {ogg_path}")
            return ogg_path
        logger.warning("OGG конвертация недоступна (pydub и ffmpeg), отправка WAV")
        return wav_path

    # ── TTS SSML ─────────────────────────────────────────────────────────────

    async def synthesize_ssml(self, ssml_text: str, out_dir: str, speaker: Optional[str] = None) -> str:
        """
        Синтезирует речь из SSML-разметки.
        Возвращает путь к аудиофайлу (OGG Opus или WAV как fallback).
        Silero v5 поддерживает: <speak>, <p>, <s>, <break>, <prosody rate|pitch>.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_synthesize_ssml, ssml_text, out_dir, speaker)

    def _sync_synthesize_ssml(self, ssml_text: str, out_dir: str, speaker: Optional[str] = None) -> str:
        """
        Синхронный SSML-синтез через model.save_wav(ssml_text=...).
        save_wav может вернуть несколько WAV-файлов (по одному на сегмент SSML) —
        они склеиваются через pydub, результат конвертируется в OGG Opus.
        """
        self._load_silero_model()

        tts_cfg = self._config.get("tts", {})
        effective_speaker = speaker or tts_cfg.get("speaker", "xenia")
        sample_rate = tts_cfg.get("sample_rate", 48000)

        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        audio_base = str(out_path / f"ssml_{timestamp}.wav")

        # save_wav возвращает список путей (один файл на абзац/сегмент)
        wav_paths = self._silero.save_wav(
            ssml_text=ssml_text,
            speaker=effective_speaker,
            sample_rate=sample_rate,
            audio_path=audio_base,
        )

        if not isinstance(wav_paths, (list, tuple)):
            wav_paths = [str(wav_paths)]
        wav_paths = [str(p) for p in wav_paths]

        # Склеить сегменты если их несколько
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
                logger.warning(f"Не удалось склеить SSML сегменты ({e}), использую первый")
                merged_wav = wav_paths[0]

        logger.info(f"SSML TTS синтез → WAV: {merged_wav}")

        # Конвертировать в OGG Opus (голосовое сообщение в Telegram)
        ogg_path = self._wav_to_ogg(merged_wav)
        if ogg_path:
            logger.info(f"SSML TTS → OGG: {ogg_path}")
            return ogg_path
        logger.warning("OGG конвертация недоступна (pydub и ffmpeg), отправка WAV")
        return merged_wav

    # ── Прогрев (warmup) ─────────────────────────────────────────────────────

    async def warmup(self) -> None:
        """
        Загружает STT и TTS модели в фоновом потоке при старте сервера.
        Устраняет задержку при первом голосовом сообщении пользователя.
        Ошибки логируются но не поднимаются — сервер стартует в любом случае.
        """
        loop = asyncio.get_event_loop()

        logger.info("▶ Начинаю предзагрузку голосовых моделей (STT + TTS)...")
        t0 = time.monotonic()

        # Whisper STT
        try:
            await loop.run_in_executor(None, self._load_whisper_model)
        except Exception as e:
            logger.warning(f"⚠ Whisper warmup не удался: {e}")

        # Silero TTS (включает JIT-прогрев)
        try:
            await loop.run_in_executor(None, self._load_silero_model)
        except Exception as e:
            logger.warning(f"⚠ Silero warmup не удался: {e}")

        elapsed = time.monotonic() - t0
        logger.info(f"✅ Голосовые модели готовы за {elapsed:.1f}с")

    @staticmethod
    def check_dependencies() -> dict:
        """
        Проверяет наличие всех зависимостей для STT и TTS.
        Возвращает словарь с результатами и печатает предупреждения.
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
            warnings.append("faster-whisper не установлен → STT недоступен (pip install faster-whisper)")

        try:
            import torch  # noqa
            results["torch"] = True
        except ImportError:
            warnings.append("torch не установлен → TTS недоступен")

        try:
            import scipy  # noqa
            results["scipy"] = True
        except ImportError:
            warnings.append("scipy не установлен → TTS может не работать (pip install scipy)")

        try:
            import pydub  # noqa
            results["pydub"] = True
            # Проверить ffmpeg
            import subprocess
            r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=3)
            if r.returncode == 0:
                results["ffmpeg"] = True
            else:
                warnings.append("ffmpeg не найден → TTS будет отправлять WAV вместо OGG (голосовой пузырь)")
        except ImportError:
            warnings.append("pydub не установлен → WAV→OGG конвертация недоступна (pip install pydub)")
        except Exception:
            warnings.append("ffmpeg не найден в PATH → TTS будет отправлять WAV вместо OGG (голосовой пузырь)")

        if results["torch"]:
            from pathlib import Path as _Path
            import os as _os
            # Проверить model.pt
            default_path = _Path(__file__).parent.parent / "speech-text" / "model.pt"
            if default_path.exists():
                results["silero_model"] = True
            else:
                warnings.append(f"Silero model.pt не найден: {default_path} → TTS недоступен")

        if warnings:
            logger.warning("⚠️  Voice subsystem: обнаружены проблемы с зависимостями:")
            for w in warnings:
                logger.warning(f"   • {w}")
        else:
            logger.info("✅ Voice subsystem: все зависимости доступны (STT + TTS готовы)")

        return results


# ─── Глобальный синглтон ────────────────────────────────────────────────────

_processor: Optional[SpeechProcessor] = None


def get_speech_processor(config: dict) -> SpeechProcessor:
    """
    Возвращает глобальный синглтон SpeechProcessor.
    При первом вызове создаёт, проверяет зависимости и логирует статус.
    """
    global _processor
    if _processor is None:
        _processor = SpeechProcessor(config)
        # Проверить зависимости сразу при создании — чтобы проблемы были видны в логах
        _processor.check_dependencies()
    return _processor


def get_existing_speech_processor() -> Optional[SpeechProcessor]:
    """
    Возвращает синглтон SpeechProcessor если он уже создан, иначе None.
    Не создаёт новый экземпляр. Используется в инструментах агента,
    чтобы не перечитывать config.yaml при каждом вызове.
    """
    return _processor
