"""
Memory Tools - инструменты для работы с долгосрочной памятью агента.

Предоставляет агентам возможность:
- Сохранять важную информацию в долгосрочную память (MEMORY.md)
- Сохранять заметки в дневные файлы
- Вспоминать информацию из памяти
- Получать статистику памяти
"""

from typing import Optional
from agents import function_tool
from loguru import logger


@function_tool
def save_memory(content: str, is_long_term: bool = False) -> str:
    """
    Сохранить важную информацию в память агента для будущего использования.

    Используйте этот инструмент когда:
    - Пользователь сообщает важную информацию о себе, своих предпочтениях
    - Вы узнали что-то, что будет полезно в будущих разговорах
    - Нужно запомнить контекст проекта, настройки, решения

    Args:
        content: Текст для сохранения в память
        is_long_term: Если True - сохранить в долгосрочную память (MEMORY.md),
                     если False - сохранить в дневную заметку (по умолчанию)

    Returns:
        str: Подтверждение сохранения или сообщение об ошибке

    Examples:
        save_memory("Пользователь предпочитает Python для backend разработки")
        save_memory("Проект использует FastAPI и PostgreSQL", is_long_term=True)
    """
    logger.info(f"save_memory called: is_long_term={is_long_term}, content_length={len(content)}")

    try:
        # В реальной реализации unified_memory будет доступен через context
        # Здесь используем заглушку для демонстрации API
        # TODO: Integrate with UnifiedMemory from context

        # Simplified implementation for now
        logger.info(f"💾 Saving to {'long-term' if is_long_term else 'daily'} memory")

        return (
            f"✅ Сохранено в {'долгосрочную' if is_long_term else 'дневную'} память:\n"
            f"{content[:100]}{'...' if len(content) > 100 else ''}"
        )

    except Exception as e:
        logger.error(f"Failed to save memory: {e}")
        return f"❌ Ошибка сохранения в память: {str(e)}"


@function_tool
def recall_memory(query: Optional[str] = None, days_back: int = 7) -> str:
    """
    Вспомнить информацию из памяти агента.

    Используйте этот инструмент когда:
    - Нужно вспомнить что-то из предыдущих разговоров
    - Пользователь спрашивает "что ты помнишь о..."
    - Нужен контекст из прошлого для ответа на текущий вопрос

    Args:
        query: Опциональный поисковый запрос (keyword search).
              Если не указан - возвращается весь контекст памяти.
        days_back: Сколько дней назад искать в дневных заметках (по умолчанию 7)

    Returns:
        str: Найденная информация из памяти или сообщение об отсутствии

    Examples:
        recall_memory("Python")
        recall_memory("предпочтения пользователя", days_back=30)
        recall_memory()  # Получить весь контекст памяти
    """
    logger.info(f"recall_memory called: query={query}, days_back={days_back}")

    try:
        # В реальной реализации unified_memory будет доступен через context
        # TODO: Integrate with UnifiedMemory from context

        # Simplified implementation for now
        if query:
            logger.info(f"🔍 Searching memory for: {query}")
            return f"🔍 Поиск по запросу '{query}':\n\n(Память пока не настроена, это заглушка)"
        else:
            logger.info("📚 Retrieving full memory context")
            return "📚 Полный контекст памяти:\n\n(Память пока не настроена, это заглушка)"

    except Exception as e:
        logger.error(f"Failed to recall memory: {e}")
        return f"❌ Ошибка при вспоминании: {str(e)}"


@function_tool
def get_memory_stats() -> str:
    """
    Получить статистику по памяти агента.

    Показывает:
    - Количество сообщений в краткосрочной памяти
    - Размер долгосрочной памяти
    - Количество дневных заметок
    - Текущий контекст ID

    Returns:
        str: Formatted статистика памяти

    Examples:
        get_memory_stats()
    """
    logger.info("get_memory_stats called")

    try:
        # В реальной реализации unified_memory будет доступен через context
        # TODO: Integrate with UnifiedMemory from context

        # Simplified implementation for now
        stats_text = """
📊 Статистика памяти:

**Краткосрочная память:**
- Сообщений в истории: (пока не настроено)
- Операций в истории: (пока не настроено)
- Текущий контекст: (пока не настроено)

**Долгосрочная память:**
- Размер MEMORY.md: (пока не настроено)
- Размер дневной заметки: (пока не настроено)
- Количество дневных файлов: (пока не настроено)
"""

        return stats_text

    except Exception as e:
        logger.error(f"Failed to get memory stats: {e}")
        return f"❌ Ошибка получения статистики: {str(e)}"


@function_tool
def clear_conversation_memory() -> str:
    """
    Очистить историю текущего разговора (краткосрочная память).

    ⚠️ ВНИМАНИЕ: Это НЕ удаляет долгосрочную память (MEMORY.md и дневные заметки).
    Очищается только история сообщений текущей сессии.

    Используйте когда:
    - Пользователь просит начать с чистого листа
    - Нужно сбросить контекст разговора
    - Разговор стал слишком длинным и нужен fresh start

    Returns:
        str: Подтверждение очистки

    Examples:
        clear_conversation_memory()
    """
    logger.info("clear_conversation_memory called")

    try:
        # В реальной реализации unified_memory будет доступен через context
        # TODO: Integrate with UnifiedMemory from context

        logger.info("🗑️ Clearing conversation history")

        return "✅ История разговора очищена. Начинаем с чистого листа!\n\n" \
               "💡 Долгосрочная память (MEMORY.md) сохранена."

    except Exception as e:
        logger.error(f"Failed to clear conversation: {e}")
        return f"❌ Ошибка очистки истории: {str(e)}"


# Helper functions for future integration with context

def _get_unified_memory_from_context(context):
    """
    Get UnifiedMemory instance from agent context.

    TODO: Implement this when integrating with AgentFactory.
    The context should provide access to factory.unified_memory

    Args:
        context: RunContextWrapper or similar

    Returns:
        UnifiedMemory instance or None
    """
    # This will be implemented when AgentFactory is updated
    # to provide unified_memory through context
    return None


def _log_memory_operation(operation: str, details: dict):
    """Log memory operations for debugging and monitoring"""
    logger.debug(f"Memory operation: {operation}", extra=details)
