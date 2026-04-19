import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

@dataclass
class ProgressEvent:
    """
    Событие прогресса от агента или инструмента.

    Типы событий:
    - agent_start: Агент начал выполнение
    - agent_end: Агент завершил выполнение
    - tool_call_start: Инструмент начал выполнение
    - tool_call_end: Инструмент завершил выполнение
    - tool_error: Ошибка в инструменте
    - blackboard_post: Запись в blackboard
    - subagent_spawn: Создан подагент
    - error: Ошибка в агенте
    """

    event_type: str
    agent_name: str
    content: str
    parent_id: Optional[str] = None  # Для построения дерева агентов
    status: str = "running"  # 'running', 'completed', 'failed'
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    details: Optional[Dict[str, Any]] = None  # Дополнительные данные для spoiler
    event_id: str = field(default_factory=lambda: f"{int(time.time() * 1000)}")

    def __post_init__(self):
        """Ensure details is a dict"""
        if self.details is None:
            self.details = {}
