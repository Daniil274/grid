"""
Менеджер контекста для агентов.
"""

from typing import List, Dict, Optional
from datetime import datetime

class ContextManager:
    """Управляет контекстом разговора агента."""
    
    def __init__(self, max_history: int = 10):
        """
        Инициализация менеджера контекста.
        
        Args:
            max_history: Максимальное количество сообщений в истории
        """
        self.conversation_history: List[Dict[str, str]] = []
        self.max_history = max_history
    
    def add_message(self, role: str, content: str):
        """
        Добавляет сообщение в историю разговора.
        
        Args:
            role: Роль отправителя ('user' или 'assistant')
            content: Содержание сообщения
        """
        self.conversation_history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        
        # Ограничиваем размер истории
        if len(self.conversation_history) > self.max_history:
            self.conversation_history = self.conversation_history[-self.max_history:]
    
    def clear_history(self):
        """Очищает историю разговора."""
        self.conversation_history.clear()
        print("✅ История разговора очищена")
    
    def get_context_message(self) -> str:
        """
        Формирует контекстное сообщение на основе истории разговора.
        
        Returns:
            str: Контекстное сообщение для агента
        """
        if not self.conversation_history:
            return ""
        
        context_parts = ["📋 Контекст предыдущих сообщений:"]
        
        # Показываем последние сообщения для контекста
        recent_messages = self.conversation_history[-self.max_history:]
        
        for i, msg in enumerate(recent_messages, 1):
            role_emoji = "👤" if msg["role"] == "user" else "🤖"
            # Обрезаем длинные сообщения для читаемости
            content = msg["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            context_parts.append(f"{i}. {role_emoji} {msg['role'].title()}: {content}")
        
        context_parts.append("\nПожалуйста, учитывай этот контекст при ответе.")
        return "\n".join(context_parts)
    
    def get_history_count(self) -> int:
        """Возвращает количество сообщений в истории."""
        return len(self.conversation_history)
    
    def get_last_user_message(self) -> Optional[str]:
        """Возвращает последнее сообщение пользователя."""
        for msg in reversed(self.conversation_history):
            if msg["role"] == "user":
                return msg["content"]
        return None 