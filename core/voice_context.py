"""
Контекст голосового канала для Telegram.

Хранит ссылки на bot и chat_id через ContextVar (asyncio-safe).
Устанавливается bridge перед запуском агента, читается инструментом send_voice_reply.
"""

from contextvars import ContextVar
from typing import Optional, Tuple, Any

# ContextVar живёт в рамках asyncio-задачи и её дочерних задач
_voice_ctx: ContextVar[Optional[Tuple[Any, int, str]]] = ContextVar(
    "telegram_voice_ctx", default=None
)


def set_voice_context(bot: Any, chat_id: int, user_workspace: str) -> None:
    """
    Установить контекст голосового канала перед запуском агента.

    Args:
        bot: экземпляр telegram.Bot
        chat_id: ID чата куда отправлять голосовой ответ
        user_workspace: путь к рабочей директории пользователя (для TTS-файлов)
    """
    _voice_ctx.set((bot, chat_id, user_workspace))


def get_voice_context() -> Optional[Tuple[Any, int, str]]:
    """
    Получить текущий голосовой контекст.

    Returns:
        (bot, chat_id, user_workspace) или None если не установлен
    """
    return _voice_ctx.get()


def clear_voice_context() -> None:
    """Сбросить контекст (вызывается после завершения агента)."""
    _voice_ctx.set(None)
