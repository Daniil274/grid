"""
Unified Memory System - обёртка над краткосрочной памятью (ContextManager).

Долгосрочная память перенесена в SQLite (MemoryStore / memory_tools_v2).
Здесь остаётся только контекст разговора для совместимости с TelegramBridge и AgentFactory.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from core.context import ContextManager


class UnifiedMemory:
    """
    Обёртка над ContextManager для краткосрочной истории разговора.

    Использование:
        memory = UnifiedMemory(
            workspace=Path("./workspace"),
            persist_path=Path("./workspace/persistence")
        )
        memory.add_message("user", "Привет!")
        context = memory.get_full_context()
    """

    def __init__(
        self,
        workspace: Path,
        persist_path: Path,
        max_history: int = 15
    ):
        """
        Args:
            workspace: Рабочая директория (для совместимости, не создаёт MEMORY.md/daily_notes)
            persist_path: Директория для персистентности ContextManager
            max_history: Максимум сообщений в краткосрочной памяти
        """
        self.workspace = Path(workspace)
        self.persist_path = Path(persist_path)
        self.max_history = max_history

        self.persist_path.mkdir(parents=True, exist_ok=True)

        context_persist_file = self.persist_path / "context.json"
        self.context_manager = ContextManager(
            max_history=max_history,
            persist_path=str(context_persist_file)
        )
        logger.info(f"ContextManager initialized (max_history={max_history})")

    def get_full_context(self, last_n_messages: int = 5) -> str:
        """
        Получить контекст для агента: последние сообщения и недавние операции.
        """
        sections = []

        try:
            conv_context = self.context_manager.get_conversation_context(
                last_n=last_n_messages
            )
            if conv_context:
                sections.append("=== ПОСЛЕДНИЕ СООБЩЕНИЯ ===\n" + conv_context)
        except Exception as e:
            logger.error(f"Failed to load conversation context: {e}")

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

        return "\n\n".join(sections) if sections else ""

    def add_message(self, role: str, content: str, metadata: Optional[dict] = None):
        """Добавить сообщение в историю разговора."""
        try:
            self.context_manager.add_message(role, content, metadata)
        except Exception as e:
            logger.error(f"Failed to add message: {e}")

    def clear_conversation(self):
        """Очистить историю текущего разговора."""
        try:
            context_id = self.context_manager.start_new_context()
            logger.info(f"Conversation cleared, new context: {context_id}")
            return context_id
        except Exception as e:
            logger.error(f"Failed to clear conversation: {e}")
            return None

    def get_memory_stats(self) -> dict:
        """Статистика по краткосрочной памяти (контекст)."""
        return {
            "short_term": {
                "current_context_id": self.context_manager.get_current_context_id(),
                "message_count": len(self.context_manager._conversation_history),
                "execution_count": len(self.context_manager._execution_history),
                "context_count": len(self.context_manager.list_context_ids()),
            },
        }
