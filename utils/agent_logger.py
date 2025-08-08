"""
Детальное логирование агентов с полными сообщениями, историей и промптами.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum

from .logger import Logger


class AgentLogLevel(Enum):
    """Уровни логирования агентов."""
    BASIC = "basic"           # Только основные события
    DETAILED = "detailed"     # Детальная информация
    FULL = "full"            # Полная информация включая промпты и историю


@dataclass
class AgentMessage:
    """Структура сообщения агента."""
    role: str  # user, assistant, system
    content: str
    timestamp: str
    agent_name: Optional[str] = None
    message_id: Optional[str] = None


@dataclass
class AgentPrompt:
    """Структура промпта агента."""
    agent_name: str
    prompt_type: str  # base, context, full
    content: str
    timestamp: str
    context_path: Optional[str] = None


@dataclass
class AgentExecution:
    """Структура выполнения агента."""
    agent_name: str
    start_time: str
    input_message: str
    end_time: Optional[str] = None
    duration: Optional[float] = None
    output_message: Optional[str] = None
    error: Optional[str] = None
    tools_used: List[str] = None
    prompt_used: Optional[str] = None
    conversation_history: List[AgentMessage] = None
    execution_id: Optional[str] = None


class AgentLogger:
    """Детальный логгер для агентов."""
    
    def __init__(self, log_dir: str = "logs", level: AgentLogLevel = AgentLogLevel.FULL):
        """
        Инициализация логгера агентов.
        
        Args:
            log_dir: Директория для логов
            level: Уровень детализации логирования
        """
        self.log_dir = Path(log_dir)
        self.level = level
        self.logger = Logger(__name__)
        
        # Создаем директории для логов
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "agents").mkdir(exist_ok=True)
        (self.log_dir / "prompts").mkdir(exist_ok=True)
        (self.log_dir / "conversations").mkdir(exist_ok=True)
        
        # Текущее выполнение
        self.current_execution: Optional[AgentExecution] = None
        self.conversation_history: List[AgentMessage] = []
        
    def start_execution(self, agent_name: str, input_message: str, 
                       context_path: Optional[str] = None) -> str:
        """
        Начать логирование выполнения агента.
        
        Args:
            agent_name: Имя агента
            input_message: Входное сообщение
            context_path: Путь контекста
            
        Returns:
            ID выполнения
        """
        execution_id = f"{agent_name}_{int(time.time())}"
        
        self.current_execution = AgentExecution(
            agent_name=agent_name,
            start_time=datetime.now().isoformat(),
            input_message=input_message,
            execution_id=execution_id,
            tools_used=[],
            conversation_history=[]
        )
        
        # Добавляем входное сообщение в историю
        self._add_message("user", input_message, agent_name)
        
        # Логируем начало выполнения
        self._log_execution_start()
        
        return execution_id
    
    def end_execution(self, output_message: str, duration: float, 
                     tools_used: List[str] = None) -> None:
        """
        Завершить логирование выполнения агента.
        
        Args:
            output_message: Выходное сообщение
            duration: Длительность выполнения
            tools_used: Использованные инструменты
        """
        if not self.current_execution:
            return
            
        self.current_execution.end_time = datetime.now().isoformat()
        self.current_execution.duration = duration
        self.current_execution.output_message = output_message
        
        if tools_used:
            self.current_execution.tools_used = tools_used
        
        # Добавляем выходное сообщение в историю
        self._add_message("assistant", output_message, self.current_execution.agent_name)
        
        # Логируем завершение выполнения
        self._log_execution_end()
        
        # Сохраняем полную информацию
        self._save_execution_details()
        
        # Очищаем текущее выполнение
        self.current_execution = None
    
    def log_error(self, error: Exception) -> None:
        """
        Логировать ошибку выполнения.
        
        Args:
            error: Исключение
        """
        if not self.current_execution:
            return
            
        self.current_execution.error = str(error)
        self.current_execution.end_time = datetime.now().isoformat()
        
        # Логируем ошибку
        self._log_execution_error(error)
        
        # Сохраняем информацию об ошибке
        self._save_execution_details()
        
        # Очищаем текущее выполнение
        self.current_execution = None
    
    def log_prompt(self, agent_name: str, prompt_type: str, content: str,
                  context_path: Optional[str] = None) -> None:
        """
        Логировать промпт агента.
        
        Args:
            agent_name: Имя агента
            prompt_type: Тип промпта
            content: Содержимое промпта
            context_path: Путь контекста
        """
        if self.level == AgentLogLevel.BASIC:
            return
            
        prompt = AgentPrompt(
            agent_name=agent_name,
            prompt_type=prompt_type,
            content=content,
            timestamp=datetime.now().isoformat(),
            context_path=context_path
        )
        
        # Сохраняем промпт в отдельный файл
        self._save_prompt(prompt)
        
        # Добавляем в текущее выполнение
        if self.current_execution:
            self.current_execution.prompt_used = content
    
    def log_tool_call(self, tool_name: str, args: Dict[str, Any], 
                     result: Optional[str] = None, error: Optional[str] = None) -> None:
        """
        Логировать вызов инструмента.
        
        Args:
            tool_name: Имя инструмента
            args: Аргументы
            result: Результат
            error: Ошибка
        """
        if not self.current_execution:
            return
            
        # Добавляем инструмент в список использованных
        if tool_name not in self.current_execution.tools_used:
            self.current_execution.tools_used.append(tool_name)
        
        # Логируем вызов инструмента
        tool_info = {
            "tool_name": tool_name,
            "args": args,
            "result": result,
            "error": error,
            "timestamp": datetime.now().isoformat()
        }
        
        self.logger.info(
            f"TOOL_CALL | {tool_name}",
            tool_info=tool_info,
            event_type="tool_call"
        )
    
    def _add_message(self, role: str, content: str, agent_name: Optional[str] = None) -> None:
        """Добавить сообщение в историю."""
        message = AgentMessage(
            role=role,
            content=content,
            timestamp=datetime.now().isoformat(),
            agent_name=agent_name
        )
        
        self.conversation_history.append(message)
        
        if self.current_execution:
            self.current_execution.conversation_history.append(message)
    
    def _log_execution_start(self) -> None:
        """Логировать начало выполнения."""
        if not self.current_execution:
            return
            
        self.logger.info(
            f"AGENT_EXECUTION_START | {self.current_execution.agent_name}",
            execution_id=self.current_execution.execution_id,
            agent_name=self.current_execution.agent_name,
            input_length=len(self.current_execution.input_message),
            event_type="execution_start"
        )
    
    def _log_execution_end(self) -> None:
        """Логировать завершение выполнения."""
        if not self.current_execution:
            return
            
        self.logger.info(
            f"AGENT_EXECUTION_END | {self.current_execution.agent_name} | {self.current_execution.duration:.2f}s",
            execution_id=self.current_execution.execution_id,
            agent_name=self.current_execution.agent_name,
            duration=self.current_execution.duration,
            output_length=len(self.current_execution.output_message or ""),
            tools_count=len(self.current_execution.tools_used or []),
            event_type="execution_end"
        )
    
    def _log_execution_error(self, error: Exception) -> None:
        """Логировать ошибку выполнения."""
        if not self.current_execution:
            return
            
        self.logger.error(
            f"AGENT_EXECUTION_ERROR | {self.current_execution.agent_name} | {error}",
            execution_id=self.current_execution.execution_id,
            agent_name=self.current_execution.agent_name,
            error=str(error),
            error_type=type(error).__name__,
            event_type="execution_error"
        )
    
    def _save_execution_details(self) -> None:
        """Сохранить детальную информацию о выполнении."""
        if not self.current_execution or self.level == AgentLogLevel.BASIC:
            return
            
        # Создаем имя файла
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"execution_{self.current_execution.agent_name}_{timestamp}.json"
        filepath = self.log_dir / "agents" / filename
        
        # Сохраняем в JSON
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(asdict(self.current_execution), f, ensure_ascii=False, indent=2)
    
    def _save_prompt(self, prompt: AgentPrompt) -> None:
        """Сохранить промпт в отдельный файл."""
        if self.level == AgentLogLevel.BASIC:
            return
            
        # Создаем имя файла
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"prompt_{prompt.agent_name}_{prompt.prompt_type}_{timestamp}.json"
        filepath = self.log_dir / "prompts" / filename
        
        # Сохраняем в JSON
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(asdict(prompt), f, ensure_ascii=False, indent=2)
    
    def save_conversation_history(self, conversation_id: Optional[str] = None) -> None:
        """
        Сохранить историю разговора.
        
        Args:
            conversation_id: ID разговора
        """
        if not self.conversation_history:
            return
            
        if not conversation_id:
            conversation_id = f"conversation_{int(time.time())}"
        
        # Создаем имя файла
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"conversation_{conversation_id}_{timestamp}.json"
        filepath = self.log_dir / "conversations" / filename
        
        # Сохраняем историю
        conversation_data = {
            "conversation_id": conversation_id,
            "created_at": datetime.now().isoformat(),
            "messages": [asdict(msg) for msg in self.conversation_history]
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(conversation_data, f, ensure_ascii=False, indent=2)
    
    def clear_history(self) -> None:
        """Очистить историю разговора."""
        self.conversation_history.clear()
        if self.current_execution:
            self.current_execution.conversation_history.clear()


# Глобальный экземпляр логгера агентов
_agent_logger: Optional[AgentLogger] = None


def get_agent_logger() -> AgentLogger:
    """Получить глобальный экземпляр логгера агентов."""
    global _agent_logger
    if _agent_logger is None:
        _agent_logger = AgentLogger()
    return _agent_logger


def configure_agent_logger(log_dir: str = "logs", level: AgentLogLevel = AgentLogLevel.FULL) -> None:
    """
    Настроить глобальный логгер агентов.
    
    Args:
        log_dir: Директория для логов
        level: Уровень детализации
    """
    global _agent_logger
    _agent_logger = AgentLogger(log_dir, level)


# Удобные функции для быстрого доступа
def log_agent_start(agent_name: str, input_message: str, context_path: Optional[str] = None) -> str:
    """Логировать начало выполнения агента."""
    return get_agent_logger().start_execution(agent_name, input_message, context_path)


def log_agent_end(output_message: str, duration: float, tools_used: List[str] = None) -> None:
    """Логировать завершение выполнения агента."""
    get_agent_logger().end_execution(output_message, duration, tools_used)


def log_agent_error(error: Exception) -> None:
    """Логировать ошибку агента."""
    get_agent_logger().log_error(error)


def log_agent_prompt(agent_name: str, prompt_type: str, content: str, 
                    context_path: Optional[str] = None) -> None:
    """Логировать промпт агента."""
    get_agent_logger().log_prompt(agent_name, prompt_type, content, context_path)


def log_tool_call(tool_name: str, args: Dict[str, Any], 
                 result: Optional[str] = None, error: Optional[str] = None) -> None:
    """Логировать вызов инструмента."""
    get_agent_logger().log_tool_call(tool_name, args, result, error) 