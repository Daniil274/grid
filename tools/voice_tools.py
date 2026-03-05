"""
Голосовые инструменты для агентов — синтез и отправка голосовых сообщений в Telegram.

Инструмент send_voice_reply:
  - Синтезирует речь из текста через Silero TTS
  - Отправляет голосовое сообщение пользователю напрямую в чат
  - Используется агентом когда нужно ответить голосом (например, на голосовой запрос)
"""

import logging
import os
from pathlib import Path
from typing import Any

from agents import function_tool, RunContextWrapper

logger = logging.getLogger("tools.voice")


@function_tool
async def send_voice_reply(ctx: RunContextWrapper[Any], text: str, speaker: str = "xenia") -> str:
    """
    Синтезирует речь из текста и отправляет голосовое сообщение пользователю в Telegram.

    Используй этот инструмент чтобы ответить пользователю голосом.
    По умолчанию используй его когда пользователь прислал голосовое сообщение.

    Args:
        text: Текст для синтеза речи (можно любой длины, длинный текст автоматически разбивается на части)
        speaker: Голос Silero — xenia (женский, по умолч.), aidar, kseniya, baya, eugene

    Returns:
        Статус отправки
    """
    from core.voice_context import get_voice_context
    from core.speech_processor import get_existing_speech_processor

    voice_ctx = get_voice_context()
    if not voice_ctx:
        return "Голосовой канал недоступен (бот запущен не в Telegram-режиме)"

    bot, chat_id, user_workspace = voice_ctx

    # SP уже инициализирован при старте бота — не читаем config.yaml заново
    sp = get_existing_speech_processor()
    if sp is None:
        return "Синтез речи недоступен (SpeechProcessor не инициализирован — проверь voice.enabled в config.yaml)"

    # Ограничить длину (Silero плохо с длинными текстами) — теперь обрабатывается внутри synthesize
    tts_text = text

    # Синтезировать речь (speaker передаётся напрямую в модель)
    tts_dir = Path(user_workspace) / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)

    try:
        audio_path = await sp.synthesize(tts_text, str(tts_dir), speaker=speaker or None)
    except Exception as e:
        logger.error(f"TTS synthesis failed: {e}")
        return f"Ошибка синтеза речи: {e}"

    # Отправить в Telegram
    try:
        with open(audio_path, "rb") as f:
            if audio_path.endswith(".ogg"):
                await bot.send_voice(chat_id=chat_id, voice=f)
            else:
                await bot.send_audio(chat_id=chat_id, audio=f)
        effective_speaker = speaker or sp._config.get("tts", {}).get("speaker", "xenia")
        logger.info(f"Голосовой ответ отправлен в chat_id={chat_id}, файл={audio_path}")
        return f"Голосовое сообщение отправлено ({len(tts_text)} символов, голос: {effective_speaker}). Текст: {tts_text}"
    except Exception as e:
        logger.error(f"Ошибка отправки голосового сообщения: {e}")
        return f"Ошибка отправки: {e}"
    finally:
        # Удалить временный файл после отправки
        try:
            os.remove(audio_path)
        except Exception:
            pass


@function_tool
async def send_voice_ssml(ctx: RunContextWrapper[Any], ssml_text: str, speaker: str = "xenia") -> str:
    """
    Синтезирует речь из SSML-разметки и отправляет голосовое сообщение в Telegram.

    SSML позволяет управлять темпом, высотой тона, паузами и структурой речи.
    Используй когда нужен выразительный голосовой ответ с интонацией.

    Поддерживаемые теги Silero v5:
      <speak>...</speak>               — корневой элемент (обязателен)
      <p>...</p>                       — абзац (пауза между абзацами)
      <s>...</s>                       — предложение (пауза между предложениями)
      <break time="500ms"/>            — явная пауза (500ms, 1s, и т.д.)
      <prosody rate="slow">...</prosody>   — темп: x-slow, slow, medium, fast, x-fast
      <prosody pitch="high">...</prosody>  — тон: x-low, low, medium, high, x-high

    Пример:
      <speak><p>Привет! <prosody rate="slow">Говорю медленно.</prosody></p>
      <break time="500ms"/><p>А теперь быстро.</p></speak>

    Args:
        ssml_text: Текст с SSML-разметкой (должен начинаться с <speak>)
        speaker: Голос — xenia (по умолч.), aidar, kseniya, baya, eugene

    Returns:
        Статус отправки
    """
    from core.voice_context import get_voice_context
    from core.speech_processor import get_existing_speech_processor

    voice_ctx = get_voice_context()
    if not voice_ctx:
        return "Голосовой канал недоступен (бот запущен не в Telegram-режиме)"

    bot, chat_id, user_workspace = voice_ctx

    sp = get_existing_speech_processor()
    if sp is None:
        return "Синтез речи недоступен (SpeechProcessor не инициализирован — проверь voice.enabled в config.yaml)"

    tts_dir = Path(user_workspace) / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)

    try:
        audio_path = await sp.synthesize_ssml(ssml_text, str(tts_dir), speaker=speaker or None)
    except Exception as e:
        logger.error(f"SSML TTS synthesis failed: {e}")
        return f"Ошибка SSML синтеза: {e}"

    try:
        with open(audio_path, "rb") as f:
            if audio_path.endswith(".ogg"):
                await bot.send_voice(chat_id=chat_id, voice=f)
            else:
                await bot.send_audio(chat_id=chat_id, audio=f)
        effective_speaker = speaker or sp._config.get("tts", {}).get("speaker", "xenia")
        logger.info(f"SSML голосовой ответ отправлен в chat_id={chat_id}")
        return f"SSML голосовое сообщение отправлено (голос: {effective_speaker})"
    except Exception as e:
        logger.error(f"Ошибка отправки SSML голосового сообщения: {e}")
        return f"Ошибка отправки: {e}"
    finally:
        try:
            os.remove(audio_path)
        except Exception:
            pass


VOICE_TOOLS = {
    "send_voice_reply": send_voice_reply,
    "send_voice_ssml": send_voice_ssml,
}
