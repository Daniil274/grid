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
from utils.logger import logging
 
logger = logging.getLogger("grid.memory_tools")

# ============================================================================
# MEMORY TOOLS REGISTRY
# ============================================================================


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
        memory = get_unified_memory()
        if memory is None:
            return "⚠️ Память не настроена. Сообщите администратору."

        logger.info(f"💾 Saving to {'long-term' if is_long_term else 'daily'} memory")
        memory.save_to_memory(content, is_long_term=is_long_term)

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
              Если не указан, указан "null", "none" или пустая строка - возвращается весь контекст памяти.
        days_back: Сколько дней назад искать в дневных заметках (по умолчанию 7)

    Returns:
        str: Найденная информация из памяти или сообщение об отсутствии

    Examples:
        recall_memory("Python")
        recall_memory("предпочтения пользователя", days_back=30)
        recall_memory()  # Получить весь контекст памяти
        recall_memory(query="null")  # Также получить весь контекст
    """
    logger.info(f"recall_memory called: query={query}, days_back={days_back}")

    try:
        memory = get_unified_memory()
        if memory is None:
            return "⚠️ Память не настроена. Сообщите администратору."

        if query:
            logger.info(f"🔍 Searching memory for: {query}")
            result = memory.recall_memory(query, days_back=days_back)
            if result and result.strip():
                return f"🔍 Результаты поиска по запросу '{query}':\n\n{result}"
            else:
                return f"🔍 Ничего не найдено по запросу '{query}'"
        else:
            logger.info("📚 Retrieving full memory context")
            result = memory.get_full_context(last_n_messages=5)
            if result and result.strip():
                return f"📚 Полный контекст памяти:\n\n{result}"
            else:
                return "📚 Память пока пуста"

    except Exception as e:
        logger.error(f"Failed to recall memory: {e}")
        return f"❌ Ошибка при вспоминании: {str(e)}"


@function_tool
def append_daily_note(note: str) -> str:
    """
    Добавить заметку в дневник (daily_notes).

    Это удобный способ быстро добавить заметку в дневной файл.
    Заметки организуются по датам (YYYY-MM-DD.md) и включают timestamp.

    Используйте когда:
    - Нужно зафиксировать текущую активность
    - Сохранить промежуточный результат работы
    - Добавить напоминание или TODO

    Args:
        note: Текст заметки для добавления

    Returns:
        str: Подтверждение добавления заметки

    Examples:
        append_daily_note("Изучал интеграцию nanobot с grid")
        append_daily_note("TODO: добавить тесты для memory tools")
    """
    logger.info(f"append_daily_note called: note_length={len(note)}")

    try:
        memory = get_unified_memory()
        if memory is None:
            return "⚠️ Память не настроена. Сообщите администратору."

        logger.info("📝 Adding daily note")
        memory.save_to_memory(note, is_long_term=False)

        return (
            f"✅ Заметка добавлена в дневник:\n"
            f"{note[:100]}{'...' if len(note) > 100 else ''}"
        )

    except Exception as e:
        logger.error(f"Failed to append daily note: {e}")
        return f"❌ Ошибка добавления заметки: {str(e)}"


@function_tool
def get_daily_notes(days_back: int = 1) -> str:
    """
    Получить дневные заметки за последние N дней.

    Показывает записи из daily_notes/ за указанный период.
    По умолчанию возвращает только сегодняшние заметки.

    Используйте когда:
    - Нужно вспомнить что делали сегодня/вчера
    - Пользователь спрашивает "что было вчера?"
    - Нужен контекст последних действий

    Args:
        days_back: Сколько дней назад показать (по умолчанию 1 = только сегодня)

    Returns:
        str: Дневные заметки за указанный период

    Examples:
        get_daily_notes()  # Сегодняшние заметки
        get_daily_notes(3)  # Заметки за последние 3 дня
        get_daily_notes(7)  # Заметки за неделю
    """
    logger.info(f"get_daily_notes called: days_back={days_back}")

    try:
        memory = get_unified_memory()
        if memory is None:
            return "⚠️ Память не настроена. Сообщите администратору."

        logger.info(f"📅 Retrieving daily notes for last {days_back} days")

        if days_back == 1:
            # Только сегодняшние заметки
            today_notes = memory._read_today()
            if today_notes and today_notes.strip():
                return f"📅 Заметки за сегодня:\n\n{today_notes}"
            else:
                return "📅 Сегодняшних заметок пока нет"
        else:
            # Заметки за несколько дней
            recent_notes = memory._read_recent_daily_notes(days_back=days_back)
            if recent_notes and recent_notes.strip():
                return f"📅 Заметки за последние {days_back} дн.:\n\n{recent_notes}"
            else:
                return f"📅 Заметок за последние {days_back} дн. нет"

    except Exception as e:
        logger.error(f"Failed to get daily notes: {e}")
        return f"❌ Ошибка получения заметок: {str(e)}"


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
        memory = get_unified_memory()
        if memory is None:
            return "⚠️ Память не настроена. Сообщите администратору."

        stats = memory.get_memory_stats()

        # Format stats nicely
        short_term = stats.get("short_term", {})
        long_term = stats.get("long_term", {})

        stats_text = f"""
📊 Статистика памяти:

**Краткосрочная память:**
- Сообщений в истории: {short_term.get('message_count', 0)}
- Операций в истории: {short_term.get('execution_count', 0)}
- Текущий контекст: {short_term.get('current_context_id', 'N/A')}
- Всего контекстов: {short_term.get('context_count', 0)}

**Долгосрочная память:**
- Размер MEMORY.md: {long_term.get('memory_size_chars', 0)} символов
- Размер дневной заметки: {long_term.get('today_size_chars', 0)} символов
- Количество дневных файлов: {long_term.get('daily_files_count', 0)}
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
        memory = get_unified_memory()
        if memory is None:
            return "⚠️ Память не настроена. Сообщите администратору."

        logger.info("🗑️ Clearing conversation history")
        new_context_id = memory.clear_conversation()

        return (
            f"✅ История разговора очищена. Начинаем с чистого листа!\n\n"
            f"💡 Долгосрочная память (MEMORY.md) сохранена.\n"
            f"🆔 Новый контекст: {new_context_id}"
        )

    except Exception as e:
        logger.error(f"Failed to clear conversation: {e}")
        return f"❌ Ошибка очистки истории: {str(e)}"


@function_tool
def delete_memory_entry(entry_text: str, from_long_term: bool = True) -> str:
    """
    Удалить конкретную запись из памяти агента.

    Удаляет первое найденное вхождение указанного текста из файла памяти.
    Для долгосрочной памяти - из MEMORY.md, для дневной - из сегодняшнего файла.

    Используйте когда:
    - Пользователь просит удалить неактуальную информацию
    - Нужно убрать устаревшие или неверные данные
    - Необходимо очистить конкретную запись

    Args:
        entry_text: Точный текст записи или его часть для поиска и удаления
        from_long_term: Если True - удалять из MEMORY.md, если False - из дневных заметок

    Returns:
        str: Подтверждение удаления или сообщение об ошибке

    Examples:
        delete_memory_entry("Пользователь предпочитает Python")
        delete_memory_entry("TODO: добавить тесты", from_long_term=False)
    """
    logger.info(f"delete_memory_entry called: from_long_term={from_long_term}, text_length={len(entry_text)}")

    try:
        memory = get_unified_memory()
        if memory is None:
            return "⚠️ Память не настроена. Сообщите администратору."

        import os
        from datetime import datetime

        # Определяем файл для удаления
        if from_long_term:
            file_path = memory.memory_file
            file_type = "долгосрочной памяти (MEMORY.md)"
        else:
            today = datetime.now().strftime('%Y-%m-%d')
            file_path = os.path.join(memory.daily_notes_dir, f"{today}.md")
            file_type = "дневных заметок"

        # Проверяем существование файла
        if not os.path.exists(file_path):
            return f"⚠️ Файл {file_type} не существует"

        # Читаем файл
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Проверяем наличие текста
        if entry_text not in content:
            return f"⚠️ Текст не найден в {file_type}"

        # Удаляем найденный текст (первое вхождение)
        updated_content = content.replace(entry_text, '', 1)

        # Сохраняем обновленный контент
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(updated_content)

        logger.info(f"✅ Entry deleted from {file_type}")

        return (
            f"✅ Запись удалена из {file_type}:\n"
            f"{entry_text[:100]}{'...' if len(entry_text) > 100 else ''}"
        )

    except Exception as e:
        logger.error(f"Failed to delete memory entry: {e}")
        return f"❌ Ошибка удаления записи: {str(e)}"


@function_tool
def edit_memory_entry(old_text: str, new_text: str, in_long_term: bool = True) -> str:
    """
    Редактировать запись в памяти агента.

    Заменяет первое найденное вхождение старого текста на новый текст.
    Для долгосрочной памяти - в MEMORY.md, для дневной - в сегодняшнем файле.

    Используйте когда:
    - Пользователь просит обновить информацию
    - Нужно исправить неточности в памяти
    - Требуется актуализировать сохраненные данные

    Args:
        old_text: Текст для поиска и замены (или его часть)
        new_text: Новый текст для замены
        in_long_term: Если True - редактировать MEMORY.md, если False - дневные заметки

    Returns:
        str: Подтверждение редактирования или сообщение об ошибке

    Examples:
        edit_memory_entry("Python для backend", "Python и Go для backend")
        edit_memory_entry("TODO: добавить тесты", "✅ Тесты добавлены", in_long_term=False)
    """
    logger.info(f"edit_memory_entry called: in_long_term={in_long_term}, old_length={len(old_text)}, new_length={len(new_text)}")

    try:
        memory = get_unified_memory()
        if memory is None:
            return "⚠️ Память не настроена. Сообщите администратору."

        import os
        from datetime import datetime

        # Определяем файл для редактирования
        if in_long_term:
            file_path = memory.memory_file
            file_type = "долгосрочной памяти (MEMORY.md)"
        else:
            today = datetime.now().strftime('%Y-%m-%d')
            file_path = os.path.join(memory.daily_notes_dir, f"{today}.md")
            file_type = "дневных заметок"

        # Проверяем существование файла
        if not os.path.exists(file_path):
            return f"⚠️ Файл {file_type} не существует"

        # Читаем файл
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Проверяем наличие текста
        if old_text not in content:
            return f"⚠️ Исходный текст не найден в {file_type}"

        # Заменяем текст (первое вхождение)
        updated_content = content.replace(old_text, new_text, 1)

        # Сохраняем обновленный контент
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(updated_content)

        logger.info(f"✅ Entry edited in {file_type}")

        return (
            f"✅ Запись отредактирована в {file_type}:\n\n"
            f"**Было:**\n{old_text[:100]}{'...' if len(old_text) > 100 else ''}\n\n"
            f"**Стало:**\n{new_text[:100]}{'...' if len(new_text) > 100 else ''}"
        )

    except Exception as e:
        logger.error(f"Failed to edit memory entry: {e}")
        return f"❌ Ошибка редактирования записи: {str(e)}"


# Global unified_memory instance
# This will be set by telegram_bridge or other entry points
_unified_memory_instance = None


def set_unified_memory(unified_memory):
    """
    Set global UnifiedMemory instance for use by memory tools.

    This should be called by TelegramBridge or other entry points
    during initialization.

    Args:
        unified_memory: UnifiedMemory instance
    """
    global _unified_memory_instance
    _unified_memory_instance = unified_memory
    logger.info("✅ UnifiedMemory instance registered with memory_tools")


def get_unified_memory():
    """
    Get the global UnifiedMemory instance.

    Returns:
        UnifiedMemory instance or None if not set
    """
    if _unified_memory_instance is None:
        logger.warning("⚠️ UnifiedMemory not set! Call set_unified_memory() first.")
    return _unified_memory_instance


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
    # For now, use global instance
    return get_unified_memory()


def _log_memory_operation(operation: str, details: dict):
    """Log memory operations for debugging and monitoring"""
    logger.debug(f"Memory operation: {operation}", extra=details)


# ============================================================================
# TOOLS REGISTRY для интеграции с function_tools.py
# ============================================================================

MEMORY_TOOLS = {
    "save_memory": save_memory,
    "recall_memory": recall_memory,
    "append_daily_note": append_daily_note,
    "get_daily_notes": get_daily_notes,
    "get_memory_stats": get_memory_stats,
    "clear_conversation_memory": clear_conversation_memory,
    "delete_memory_entry": delete_memory_entry,
    "edit_memory_entry": edit_memory_entry,
}


# Экспорт для использования в других модулях
__all__ = [
    "save_memory",
    "recall_memory",
    "append_daily_note",
    "get_daily_notes",
    "get_memory_stats",
    "clear_conversation_memory",
    "delete_memory_entry",
    "edit_memory_entry",
    "set_unified_memory",
    "get_unified_memory",
    "MEMORY_TOOLS",
]
