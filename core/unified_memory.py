"""
Unified Memory System - объединяет краткосрочную и долгосрочную память.

Предоставляет:
- Краткосрочная память: последние сообщения (ContextManager)
- Долгосрочная память: MEMORY.md + daily notes (прямая работа с файлами)
- Единый API для агентов
"""

import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Импорт из grid
from core.context import ContextManager


class UnifiedMemory:
    """
    Гибридная система памяти для unified Grid+Nanobot.

    Объединяет:
    - ContextManager (grid): краткосрочная история разговора
    - Файловая система: долгосрочная память (MEMORY.md + дневные заметки)

    Использование:
        memory = UnifiedMemory(
            workspace=Path("./workspace"),
            persist_path=Path("./workspace/persistence")
        )

        # Получить полный контекст для агента
        context = memory.get_full_context()

        # Добавить сообщение
        memory.add_message("user", "Привет!")

        # Сохранить важную информацию
        memory.save_to_memory("Пользователь любит Python", is_long_term=True)
    """

    def __init__(
        self,
        workspace: Path,
        persist_path: Path,
        max_history: int = 15
    ):
        """
        Args:
            workspace: Рабочая директория для долгосрочной памяти
            persist_path: Директория для персистентности ContextManager
            max_history: Максимум сообщений в краткосрочной памяти
        """
        self.workspace = Path(workspace)
        self.persist_path = Path(persist_path)
        self.max_history = max_history

        # Paths for long-term memory
        self.memory_file = self.workspace / "MEMORY.md"
        self.daily_notes_dir = self.workspace / "daily_notes"

        # Ensure directories exist
        self._ensure_directories()

        # Initialize ContextManager (grid) - краткосрочная память
        context_persist_file = self.persist_path / "context.json"
        self.context_manager = ContextManager(
            max_history=max_history,
            persist_path=str(context_persist_file)
        )
        logger.info(f"✅ ContextManager initialized (max_history={max_history})")
        logger.info(f"✅ Long-term memory: {self.memory_file}")

    def _ensure_directories(self):
        """Create necessary directories"""
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.persist_path.mkdir(parents=True, exist_ok=True)
        self.daily_notes_dir.mkdir(parents=True, exist_ok=True)

        # Create MEMORY.md if it doesn't exist
        if not self.memory_file.exists():
            self.memory_file.write_text(
                "# Долгосрочная память\n\n"
                "Здесь сохраняется важная информация для агента.\n\n",
                encoding="utf-8"
            )

    def get_full_context(self, last_n_messages: int = 5) -> str:
        """
        Получить полный контекст для агента.

        Собирает:
        1. Долгосрочную память (MEMORY.md + сегодняшние заметки)
        2. Недавние сообщения из разговора
        3. Недавние операции агентов

        Args:
            last_n_messages: Количество последних сообщений для включения

        Returns:
            Отформатированный контекст в виде markdown
        """
        sections = []

        # 1. Long-term memory (MEMORY.md)
        try:
            memory_content = self._read_long_term()
            if memory_content and memory_content.strip():
                sections.append("=== ДОЛГОСРОЧНАЯ ПАМЯТЬ ===\n" + memory_content)
        except Exception as e:
            logger.error(f"Failed to load long-term memory: {e}")

        # 2. Today's notes
        try:
            today_content = self._read_today()
            if today_content and today_content.strip():
                sections.append("=== ЗАМЕТКИ СЕГОДНЯ ===\n" + today_content)
        except Exception as e:
            logger.debug(f"No today's notes: {e}")

        # 3. Recent conversation от grid
        try:
            conv_context = self.context_manager.get_conversation_context(
                last_n=last_n_messages
            )
            if conv_context:
                sections.append("=== ПОСЛЕДНИЕ СООБЩЕНИЯ ===\n" + conv_context)
        except Exception as e:
            logger.error(f"Failed to load conversation context: {e}")

        # 4. Recent executions от grid
        try:
            recent_execs = self.context_manager.get_recent_executions(limit=3)
            if recent_execs:
                exec_lines = ["=== НЕДАВНИЕ ОПЕРАЦИИ ==="]
                for ex in recent_execs:
                    input_preview = str(ex.input_message)[:50] if ex.input_message else "N/A"
                    output_preview = str(ex.output)[:50] if ex.output else "N/A"
                    exec_lines.append(
                        f"- {ex.agent_name}: {input_preview}... → {output_preview}..."
                    )
                sections.append("\n".join(exec_lines))
        except Exception as e:
            logger.error(f"Failed to load execution history: {e}")

        # Combine all sections
        if sections:
            return "\n\n".join(sections)
        else:
            return ""

    def add_message(self, role: str, content: str, metadata: Optional[dict] = None):
        """
        Добавить сообщение в историю разговора (краткосрочная память).

        Args:
            role: Роль отправителя (user, assistant, system)
            content: Содержание сообщения
            metadata: Опциональные метаданные
        """
        try:
            self.context_manager.add_message(role, content, metadata)
            logger.debug(f"Message added: {role} - {content[:50]}...")
        except Exception as e:
            logger.error(f"Failed to add message: {e}")

    def save_to_memory(self, content: str, is_long_term: bool = False):
        """
        Сохранить информацию в память.

        Args:
            content: Контент для сохранения
            is_long_term: Если True → MEMORY.md, если False → дневная заметка
        """
        try:
            if is_long_term:
                # Append to MEMORY.md
                self._append_to_long_term(content)
                logger.info(f"✅ Saved to long-term memory (MEMORY.md)")
            else:
                # Append to daily note
                self._append_to_today(content)
                logger.info(f"✅ Saved to daily note")
        except Exception as e:
            logger.error(f"Failed to save to memory: {e}")

    def recall_memory(self, query: Optional[str] = None, days_back: int = 7) -> str:
        """
        Вспомнить информацию из памяти.

        Args:
            query: Опциональный поисковый запрос (простой keyword search).
                   Если query="null" или пустая строка - возвращается весь контекст.
            days_back: Сколько дней назад искать в дневных заметках

        Returns:
            Найденная информация или пустая строка
        """
        try:
            # Read long-term memory
            memory_content = self._read_long_term()

            # Read recent daily notes
            recent_notes = self._read_recent_daily_notes(days_back)

            combined = memory_content + "\n\n" + recent_notes

            # Normalize query: treat "null", "None", empty string as None
            if query and query.strip().lower() not in ("null", "none", ""):
                # Simple keyword search
                if query.lower() in combined.lower():
                    return combined
                else:
                    return f"Ничего не найдено по запросу: {query}"
            else:
                # Return all memory if query is None, "null", "none", or empty
                return combined
        except Exception as e:
            logger.error(f"Failed to recall memory: {e}")
            return f"Error recalling memory: {e}"

    def clear_conversation(self):
        """Очистить историю текущего разговора (не затрагивает долгосрочную память)"""
        try:
            context_id = self.context_manager.start_new_context()
            logger.info(f"✅ Conversation cleared, new context: {context_id}")
            return context_id
        except Exception as e:
            logger.error(f"Failed to clear conversation: {e}")
            return None

    def get_memory_stats(self) -> dict:
        """Получить статистику по памяти"""
        stats = {
            "short_term": {
                "current_context_id": self.context_manager.get_current_context_id(),
                "message_count": len(self.context_manager._conversation_history),
                "execution_count": len(self.context_manager._execution_history),
                "context_count": len(self.context_manager.list_context_ids()),
            },
            "long_term": {}
        }

        try:
            long_term_size = len(self._read_long_term())
            today_size = len(self._read_today())
            daily_files = list(self.daily_notes_dir.glob("*.md"))

            stats["long_term"] = {
                "memory_size_chars": long_term_size,
                "today_size_chars": today_size,
                "daily_files_count": len(daily_files),
            }
        except Exception as e:
            stats["long_term"]["error"] = str(e)

        return stats

    # ===== Private methods for file operations =====

    def _read_long_term(self) -> str:
        """Read MEMORY.md"""
        try:
            if self.memory_file.exists():
                return self.memory_file.read_text(encoding="utf-8")
            return ""
        except Exception as e:
            logger.error(f"Failed to read MEMORY.md: {e}")
            return ""

    def _append_to_long_term(self, content: str):
        """Append to MEMORY.md"""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry = f"\n\n## {timestamp}\n\n{content}\n"

            with self.memory_file.open("a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.error(f"Failed to append to MEMORY.md: {e}")
            raise

    def _get_today_file(self) -> Path:
        """Get today's daily note file path"""
        today = datetime.now().strftime("%Y-%m-%d")
        return self.daily_notes_dir / f"{today}.md"

    def _read_today(self) -> str:
        """Read today's daily note"""
        try:
            today_file = self._get_today_file()
            if today_file.exists():
                return today_file.read_text(encoding="utf-8")
            return ""
        except Exception as e:
            logger.debug(f"No today's note: {e}")
            return ""

    def _append_to_today(self, content: str):
        """Append to today's daily note"""
        try:
            today_file = self._get_today_file()
            timestamp = datetime.now().strftime("%H:%M:%S")
            entry = f"\n\n### {timestamp}\n\n{content}\n"

            # Create file if doesn't exist
            if not today_file.exists():
                today_date = datetime.now().strftime("%Y-%m-%d")
                today_file.write_text(
                    f"# Daily Note - {today_date}\n\n",
                    encoding="utf-8"
                )

            with today_file.open("a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.error(f"Failed to append to today's note: {e}")
            raise

    def _read_recent_daily_notes(self, days_back: int = 7) -> str:
        """Read recent daily notes"""
        try:
            daily_files = sorted(self.daily_notes_dir.glob("*.md"), reverse=True)
            recent = daily_files[:days_back]

            contents = []
            for file in recent:
                try:
                    content = file.read_text(encoding="utf-8")
                    contents.append(f"## {file.stem}\n\n{content}")
                except Exception as e:
                    logger.warning(f"Failed to read {file}: {e}")

            return "\n\n".join(contents)
        except Exception as e:
            logger.error(f"Failed to read recent daily notes: {e}")
            return ""
