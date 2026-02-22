#!/usr/bin/env python3
"""
Голосовой ассистент — постоянно слушает микрофон.
Ждёт ключевых слов "Начало запроса" и "Конец запроса".
Ответ агента синтезируется в речь и воспроизводится через динамик.

Запуск:
    python voice_agent/voice_assistant.py
    python voice_agent/voice_assistant.py --config voice_agent/voice_agent.yaml
"""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import List, Optional

import numpy as np

# Добавляем корень проекта в PYTHONPATH
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

logger = logging.getLogger("voice_assistant")
logger.setLevel(logging.INFO)

class VoiceAssistant:
    """Голосовой ассистент с детекцией ключевых слов через Whisper STT."""

    def __init__(self, config_path: str = "voice_agent/voice_agent.yaml"):
        import yaml
        from core.config import Config

        config_file = str(_ROOT / config_path)

        # Читаем raw YAML для кастомных секций (voice, voice_agent не в GridConfig схеме)
        with open(config_file, "r", encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f)

        self.config = Config(config_file)

        va_cfg = raw_cfg.get("voice_agent", {})
        self._kw_cfg = va_cfg.get("keywords", {})
        self._rec_cfg = va_cfg.get("recording", {})
        self._tts_cfg = va_cfg.get("tts", {})

        self._voice_cfg: dict = raw_cfg.get("voice", {})

        # Ключевые слова (lowercase)
        self._kw_start: List[str] = [k.lower() for k in self._kw_cfg.get("start", ["начало"])]
        self._kw_end: List[str] = [k.lower() for k in self._kw_cfg.get("end", ["конец"])]

        # Параметры записи
        self._sample_rate: int = self._rec_cfg.get("sample_rate", 16000)
        self._chunk_duration: float = self._rec_cfg.get("chunk_duration", 0.5)
        self._silence_threshold: float = self._rec_cfg.get("silence_threshold", 0.008)
        self._silence_chunks_end: int = self._rec_cfg.get("silence_chunks_end", 4)
        self._auto_end_silence_seconds: float = self._rec_cfg.get("auto_end_silence_seconds", 2.5)
        self._max_record_seconds: float = self._rec_cfg.get("max_record_seconds", 120)
        # None = системный default; int = индекс устройства; str = имя (substring match)
        self._device: Optional[int] = self._rec_cfg.get("device", None)

        # TTS
        self._tts_speaker: str = self._tts_cfg.get(
            "speaker",
            self._voice_cfg.get("tts", {}).get("speaker", "xenia"),
        )
        self._tts_dir = _ROOT / self._tts_cfg.get("temp_dir", "voice_agent/tts_temp")
        self._tts_dir.mkdir(parents=True, exist_ok=True)

        # Зависимости (инициализируются в startup)
        self._sp = None       # SpeechProcessor
        self._factory = None  # AgentFactory

        # Нативная частота устройства (определяется в listen_loop, может ≠ 16000)
        self._input_sample_rate: int = self._sample_rate

        # Машина состояний
        self._state: str = "waiting"  # waiting | recording | processing

    # ─────────────────────────────────────────────────────────────────────────
    # Startup / warmup
    # ─────────────────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Прогреть все модели при старте — нулевая задержка при первом запросе."""
        from core.agent_factory import AgentFactory
        from core.speech_processor import get_speech_processor

        logger.info("🚀 Запуск голосового ассистента...")

        if not self._voice_cfg.get("enabled"):
            raise RuntimeError("voice.enabled = false в конфиге, выход")

        # Инициализация SpeechProcessor и прогрев (Whisper + Silero JIT)
        self._sp = get_speech_processor(self._voice_cfg)
        logger.info("📦 Прогрев моделей STT + TTS (может занять минуту)...")
        await self._sp.warmup()
        logger.info("✅ Модели готовы")

        # AgentFactory — отдельная от Telegram
        self._factory = AgentFactory(config=self.config)
        logger.info("🤖 AgentFactory инициализирован")
        
        # Запускаем начальный контекст, чтобы он был готов к работе
        try:
            self._factory.context_manager.start_new_context()
        except Exception as e:
            logger.error(f"Не удалось запустить новый контекст: {e}")
        logger.info("🧠 Контекст сессии инициализирован")

        kw_display = " / ".join(f'"{k}"' for k in self._kw_start[:2])
        logger.info(f"🎙️  Готов. Скажи {kw_display} для начала запроса")

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _contains_keyword(self, text: str, keywords: List[str]) -> bool:
        t = text.lower().strip()
        return any(kw in t for kw in keywords)

    def _save_wav(self, audio: np.ndarray) -> str:
        """Сохранить numpy float32 массив в временный WAV файл (16kHz для Whisper).
        Ресемплирует если нативная частота устройства отличается от 16000 Hz."""
        # Ресемплинг с нативной частоты устройства до 16000 Hz (если нужно)
        if self._input_sample_rate != self._sample_rate:
            from math import gcd
            from scipy.signal import resample_poly
            g = gcd(self._sample_rate, self._input_sample_rate)
            audio = resample_poly(audio, self._sample_rate // g, self._input_sample_rate // g)

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", dir=self._tts_dir, delete=False)
        tmp_path = tmp.name
        tmp.close()
        pcm = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)   # 16-bit
            wf.setframerate(self._sample_rate)
            wf.writeframes(pcm.tobytes())
        return tmp_path

    async def _transcribe(self, audio: np.ndarray) -> str:
        """STT: numpy аудио → текст через Whisper."""
        if audio.size == 0:
            return ""
        wav_path = self._save_wav(audio)
        try:
            text = await self._sp.transcribe(wav_path)
            return (text or "").strip()
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass

    def _sync_play(self, path: str) -> None:
        """Воспроизвести аудиофайл (WAV или OGG) синхронно."""
        try:
            import sounddevice as sd
            import soundfile as sf
            data, sr = sf.read(path)
            sd.play(data, sr)
            sd.wait()
        except Exception as e:
            logger.debug(f"soundfile/sounddevice playback failed ({e}), trying ffplay")
            try:
                subprocess.run(
                    ["ffplay", "-nodisp", "-autoexit", path],
                    capture_output=True,
                    check=False,
                )
            except FileNotFoundError:
                # Последний шанс: aplay (Linux/RPi)
                subprocess.run(["aplay", path], capture_output=True, check=False)

    async def _speak(self, text: str) -> None:
        """TTS + воспроизведение: выбирает SSML или plain text по наличию тега."""
        loop = asyncio.get_event_loop()
        speaker = self._tts_speaker
        tts_dir = str(self._tts_dir)

        # Очистка некорректного SSML: агент иногда ставит текст/теги за пределами <speak>
        if "<speak>" in text:
            # Извлекаем все, что внутри <speak>...</speak>, если агент написал что-то снаружи
            import re
            
            # Удаляем любые случайные markdown-блоки, если LLM их добавила (например, ```xml)
            text = re.sub(r'```[a-zA-Z]*\n', '', text)
            text = text.replace('```', '')
            
            # Ищем содержимое внутри <speak> (с учетом того, что агент мог забыть закрыть тег)
            match = re.search(r'<speak>(.*?)(?:</speak>|$)', text, re.DOTALL | re.IGNORECASE)
            if match:
                # Берем только то, что внутри первого найденного <speak>
                inner_content = match.group(1).strip()
                
                # Если агент умудрился вставить другие <speak> внутрь (что тоже ломает XML), вычищаем их
                inner_content = inner_content.replace('<speak>', '').replace('</speak>', '')
                
                # Добавляем корневые теги
                text = f"<speak>{inner_content}</speak>"
            else:
                # Если регулярка почему-то не сработала
                text = text.replace('<speak>', '').replace('</speak>', '')
                text = f"<speak>{text}</speak>"
        else:
            # Если тега <speak> вообще нет, но текст пришел - мы можем просто озвучить как обычный текст
            pass

        try:
            if "<speak>" in text:
                # Очищаем невалидные символы/теги, которые Silero может не проглотить
                # Разрешенные: p, s, break, prosody, speak
                import re
                
                # Удаляем все закрывающие теги кроме разрешенных
                # Удаляем все открывающие теги кроме разрешенных
                
                # Попытка синтеза
                try:
                    audio_path = await self._sp.synthesize_ssml(text, tts_dir, speaker=speaker)
                except Exception as e:
                    logger.warning(f"SSML parsing failed ({e}), fallback to plain text")
                    # Если SSML сломан, очищаем все теги и отправляем как текст
                    clean_text = re.sub(r'<[^>]+>', '', text)
                    audio_path = await self._sp.synthesize(clean_text, tts_dir, speaker=speaker)
            else:
                tts_text = text
                audio_path = await self._sp.synthesize(tts_text, tts_dir, speaker=speaker)

            await loop.run_in_executor(None, self._sync_play, audio_path)
        except Exception as e:
            logger.error(f"❌ Ошибка TTS/воспроизведения: {e}")
        finally:
            try:
                if "audio_path" in dir():
                    os.remove(audio_path)  # type: ignore[name-defined]
            except Exception:
                pass

    async def _run_agent(self, text: str) -> str:
        """Запустить агента с текстом запроса, получить текстовый ответ."""
        try:
            # Используем флаг use_active_context, чтобы не создавать новый контекст каждый раз
            response = await self._factory.run_agent(
                agent_key="voice_assistant",
                message=text,
                stream=False,
                use_active_context=True
            )
            return response or ""
        except Exception as e:
            logger.error(f"❌ Ошибка агента: {e}")
            return "<speak><p>Извини, произошла ошибка при обработке запроса.</p></speak>"

    async def _process_request(self, audio: np.ndarray) -> None:
        """Обработать полный запрос: STT → Agent → TTS → play."""
        try:
            # STT
            logger.info("🔄 Распознаю речь...")
            text = await self._transcribe(audio)

            if not text:
                logger.info("⚠  Речь не распознана")
                await self._speak("<speak><p>Не расслышал, попробуй ещё раз.</p></speak>")
                return

            # Очистка текста от ключевых слов
            import re
            clean_text = text
            for kw in self._kw_start + self._kw_end:
                # Удаляем ключевые слова (с учетом регистра)
                pattern = re.compile(re.escape(kw), re.IGNORECASE)
                clean_text = pattern.sub('', clean_text)
            
            # Убираем лишние знаки препинания по краям, которые могли остаться от вырезанных слов
            clean_text = clean_text.strip(' ,.-!?;:\n')

            if not clean_text:
                logger.info(f"⚠  В запросе были только ключевые слова: '{text}'")
                return

            logger.info(f"🎤 Запрос: {clean_text} (оригинал: '{text}')")

            # Agent
            logger.info("🤔 Думаю...")
            response = await self._run_agent(clean_text)
            preview = response[:80].replace("\n", " ")
            logger.info(f"💬 Ответ: {preview}{'...' if len(response) > 80 else ''}")

            # TTS + play
            logger.info("🔊 Озвучиваю...")
            await self._speak(response)

        finally:
            self._state = "waiting"
            kw_display = " / ".join(f'"{k}"' for k in self._kw_start[:2])
            logger.info(f"👂 Слушаю. Скажи {kw_display} для нового запроса")

    # ─────────────────────────────────────────────────────────────────────────
    # Main listen loop
    # ─────────────────────────────────────────────────────────────────────────

    async def listen_loop(self) -> None:
        """Основной цикл прослушивания микрофона."""
        import sounddevice as sd

        loop = asyncio.get_event_loop()
        audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()

        def callback(indata, frames, time_info, status):
            if status:
                logger.debug(f"sounddevice status: {status}")
            loop.call_soon_threadsafe(audio_queue.put_nowait, indata[:, 0].copy())

        # Определяем нативную частоту устройства
        device_info = sd.query_devices(self._device, kind="input") if self._device is not None else sd.query_devices(kind="input")
        self._input_sample_rate = int(device_info["default_samplerate"])

        # chunk_samples считаем на нативной частоте устройства
        chunk_samples = int(self._input_sample_rate * self._chunk_duration)
        max_record_chunks = int(self._max_record_seconds / self._chunk_duration)
        auto_end_chunks = (int(self._auto_end_silence_seconds / self._chunk_duration)
                           if self._auto_end_silence_seconds > 0 else 0)

        resample_note = (f", ресемплинг {self._input_sample_rate}→{self._sample_rate} Hz"
                         if self._input_sample_rate != self._sample_rate else "")
        dev_idx = device_info["index"] if self._device is not None else sd.default.device[0]
        logger.info(f"👂 Слушаю микрофон: [{dev_idx}] {device_info['name']} "
                    f"(native={self._input_sample_rate} Hz, chunk={chunk_samples}{resample_note}, "
                    f"threshold={self._silence_threshold})")

        # Накопленные буферы
        current_segment: List[np.ndarray] = []
        recording_buffer: List[np.ndarray] = []
        silence_count: int = 0
        recorded_chunks: int = 0
        recording_silence_chunks: int = 0  # счётчик тишины для авто-завершения

        with sd.InputStream(
            samplerate=self._input_sample_rate,
            channels=1,
            dtype="float32",
            blocksize=chunk_samples,
            device=self._device,
            callback=callback,
        ):
            while True:
                chunk: np.ndarray = await audio_queue.get()

                # В режиме PROCESSING — просто дренируем очередь
                if self._state == "processing":
                    continue

                rms = float(np.sqrt(np.mean(chunk ** 2)))
                
                # Логируем пиковый RMS каждые 5 секунд для помощи в настройке
                if not hasattr(self, "_last_rms_log"):
                    self._last_rms_log = time.time()
                    self._max_rms_recent = 0.0
                self._max_rms_recent = max(self._max_rms_recent, rms)
                
                if time.time() - self._last_rms_log > 5.0:
                    if self._max_rms_recent > 0.0001 and self._state == "waiting":
                        if self._max_rms_recent < self._silence_threshold and self._max_rms_recent > 0.0005:
                            logger.warning(f"🎤 Твой голос слишком тихий (макс RMS {self._max_rms_recent:.4f} < порог {self._silence_threshold}). Уменьши silence_threshold в voice_agent.yaml!")
                    self._last_rms_log = time.time()
                    self._max_rms_recent = 0.0

                is_speech = rms > self._silence_threshold

                if is_speech:
                    silence_count = 0
                    current_segment.append(chunk)
                    if self._state == "recording":
                        recording_buffer.append(chunk)
                        recorded_chunks += 1
                        recording_silence_chunks = 0
                else:
                    silence_count += 1
                    if self._state == "recording":
                        # Включаем тишину в запись, чтобы не резать слова
                        recording_buffer.append(chunk)
                        recorded_chunks += 1
                        recording_silence_chunks += 1
                        # Авто-завершение по тишине (без кодового слова)
                        if auto_end_chunks > 0 and recording_silence_chunks >= auto_end_chunks and recording_buffer:
                            logger.info(f"🛑 Авто-завершение (тишина {self._auto_end_silence_seconds}с)")
                            full_audio = np.concatenate(recording_buffer)
                            self._state = "processing"
                            asyncio.create_task(self._process_request(full_audio))
                            current_segment = []
                            recording_buffer = []
                            recorded_chunks = 0
                            recording_silence_chunks = 0

                # Конец речевого сегмента — достаточно тишины
                segment_ended = (
                    silence_count >= self._silence_chunks_end
                    and current_segment
                )

                if segment_ended:
                    segment_audio = np.concatenate(current_segment)
                    current_segment = []
                    silence_count = 0

                    # Транскрибируем сегмент для детекции ключевых слов
                    text = await self._transcribe(segment_audio)

                    if self._state == "waiting":
                        if text and self._contains_keyword(text, self._kw_start):
                            logger.info(f"🎙️  Начало запроса — записываю... (услышал: '{text}')")
                            self._state = "recording"
                            # Сохраняем сегмент с ключевым словом, чтобы не потерять текст, сказанный без паузы
                            recording_buffer = [segment_audio]
                            recorded_chunks = len(segment_audio) // chunk_samples
                            recording_silence_chunks = 0
                            
                            if self._contains_keyword(text, self._kw_end):
                                logger.info("🛑 Конец запроса (услышан сразу же)")
                                full_audio = np.concatenate(recording_buffer)
                                self._state = "processing"
                                asyncio.create_task(self._process_request(full_audio))
                                current_segment = []
                                recording_buffer = []
                        else:
                            if text:
                                logger.info(f"idle: '{text}'")

                    elif self._state == "recording":
                        if text and self._contains_keyword(text, self._kw_end):
                            logger.info("🛑 Конец запроса")
                            full_audio = np.concatenate(recording_buffer) if recording_buffer else np.array([], dtype=np.float32)
                            self._state = "processing"
                            asyncio.create_task(self._process_request(full_audio))
                            current_segment = []
                            recording_buffer = []
                        else:
                            if text:
                                logger.debug(f"recording: '{text}'")

                # Защита от бесконечной записи
                if self._state == "recording" and recorded_chunks >= max_record_chunks:
                    logger.warning(f"⚠  Превышен лимит записи ({self._max_record_seconds}с) — принудительно завершаю")
                    full_audio = np.concatenate(recording_buffer) if recording_buffer else np.array([], dtype=np.float32)
                    self._state = "processing"
                    asyncio.create_task(self._process_request(full_audio))
                    current_segment = []
                    recording_buffer = []

    # ─────────────────────────────────────────────────────────────────────────
    # Entry point
    # ─────────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Запустить ассистент: прогрев → бесконечный цикл прослушивания."""
        await self.startup()
        try:
            await self.listen_loop()
        except KeyboardInterrupt:
            logger.info("👋 Остановка по Ctrl+C")
        except Exception as e:
            logger.exception(f"❌ Критическая ошибка: {e}")
            raise


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _list_audio_devices() -> None:
    """Вывести список доступных аудиоустройств ввода."""
    import sounddevice as sd
    devices = sd.query_devices()
    default_in = sd.default.device[0]
    print("\nДоступные устройства ввода (микрофоны):")
    print(f"{'Idx':>4}  {'Имя':<50}  {'Вх.кан':>7}  {'Частота':>8}")
    print("-" * 76)
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            marker = "* " if i == default_in else "  "
            print(f"{marker}{i:>3}  {d['name']:<50}  {d['max_input_channels']:>7}  {int(d['default_samplerate']):>8}")
    print("\n* — системное устройство по умолчанию")
    print("Используй --device <индекс> для выбора устройства\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Голосовой ассистент")
    parser.add_argument(
        "--config",
        default="voice_agent/voice_agent.yaml",
        help="Путь к конфигу (по умолчанию: voice_agent/voice_agent.yaml)",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        metavar="IDX",
        help="Индекс микрофона (см. --list-devices). Переопределяет recording.device в конфиге.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Показать список доступных микрофонов и выйти",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Уровень логирования",
    )
    args = parser.parse_args()

    if args.list_devices:
        _list_audio_devices()
        return

    from utils.logger import Logger
    Logger.configure(
        level=args.log_level,
        log_dir=str(_ROOT / "logs"),
        enable_console=True,
        enable_json=True,
        enable_legacy_logs=True,
        force_reconfigure=True,
    )
    # Приглушить шумные логи зависимостей
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("httpcore").setLevel(logging.ERROR)
    logging.getLogger("openai").setLevel(logging.ERROR)
    logging.getLogger("faster_whisper").setLevel(logging.ERROR)

    assistant = VoiceAssistant(config_path=args.config)

    # CLI --device переопределяет настройку из конфига
    if args.device is not None:
        assistant._device = args.device

    asyncio.run(assistant.run())


if __name__ == "__main__":
    main()

