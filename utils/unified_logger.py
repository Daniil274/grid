"""
Универсальная система логирования для Grid Agent System.
Объединяет красивое отображение в терминале и детальное логирование в файлы.
"""

import json
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Union, Callable
from enum import Enum
from dataclasses import dataclass, asdict
import logging

from .pretty_logger import PrettyLogger

class LogLevel(Enum):
    """Уровни логирования с числовыми значениями."""
    DEBUG = 0
    INFO = 1
    SUCCESS = 2
    WARNING = 3
    ERROR = 4


class LogTarget(Enum):
    """Цели логирования."""
    CONSOLE = "console"
    FILE = "file"
    BOTH = "both"


class LogEventType(Enum):
    """Типы событий для логирования."""
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    AGENT_ERROR = "agent_error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PROMPT = "prompt"
    CONTEXT = "context"
    SYSTEM = "system"
    # Новые типы для обогащения логов
    REASONING = "reasoning"     # План/мысли/шаги
    DIFF = "diff"               # Текстовые диффы изменений


@dataclass
class LogEvent:
    """Структура события логирования."""
    event_type: LogEventType
    message: str
    agent_name: Optional[str] = None
    tool_name: Optional[str] = None
    duration: Optional[float] = None
    data: Optional[Dict[str, Any]] = None
    timestamp: Optional[str] = None
    level: LogLevel = LogLevel.INFO
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


class UnifiedLogger:
    """
    Универсальный логгер с красивым отображением в терминале и детальным логированием в файлы.
    Поддерживает подписчиков (слушателей) событий для внешнего обогащения логов.
    """
    
    def __init__(self, 
                 log_dir: str = "logs",
                 console_level: LogLevel = LogLevel.INFO,
                 file_level: LogLevel = LogLevel.DEBUG,
                 enable_colors: bool = True):
        """
        Инициализация универсального логгера.
        
        Args:
            log_dir: Директория для логов
            console_level: Уровень логирования для консоли
            file_level: Уровень логирования для файлов
            enable_colors: Включить цвета в консоли
        """
        # Принудительно использовать директорию логов проекта, а не рабочую директорию агента
        self.log_dir = Path("logs") if not Path(log_dir).is_absolute() else Path(log_dir)
        self.console_level = console_level
        self.file_level = file_level
        self.enable_colors = enable_colors
        
        # Создаем директории в каталоге проекта
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "agents").mkdir(exist_ok=True)
        (self.log_dir / "prompts").mkdir(exist_ok=True)
        (self.log_dir / "conversations").mkdir(exist_ok=True)
        
        # Инициализируем компоненты
        self.pretty_logger = PrettyLogger("grid")
        self.pretty_logger.colors_enabled = enable_colors
        
        # Настройка файлового логгера
        self._setup_file_logger()
        
        # Текущее выполнение
        self.current_execution: Optional[Dict[str, Any]] = None
        self.conversation_history: List[Dict[str, Any]] = []
        
        # Thread-local storage
        self._thread_local = threading.local()
        
        # Слушатели событий (обогащатели логов)
        self._listeners: List[Callable[[LogEvent, "UnifiedLogger"], None]] = []
        self._listener_names: set[str] = set()
        self._notify_guard_flag = False  # защита от рекурсии слушателей
        
    def _setup_file_logger(self):
        """Настройка файлового логгера."""
        # Основной лог файл
        self.file_logger = logging.getLogger(f"grid.file.{id(self)}")
        self.file_logger.setLevel(logging.DEBUG)
        
        # Очищаем существующие хендлеры
        for handler in self.file_logger.handlers[:]:
            self.file_logger.removeHandler(handler)
        
        # Создаем timestamped файл для агентов
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        agent_log_file = self.log_dir / f"agents_{timestamp}.log"
        
        file_handler = logging.FileHandler(agent_log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        
        # Форматтер для файлов
        file_formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
        )
        file_handler.setFormatter(file_formatter)
        
        self.file_logger.addHandler(file_handler)
        self.file_logger.propagate = False
        
    def add_listener(self, name: str, callback: Callable[[LogEvent, "UnifiedLogger"], None]) -> None:
        """Зарегистрировать слушателя событий логгера (идемпотентно по имени)."""
        if name in self._listener_names:
            return
        self._listeners.append(callback)
        self._listener_names.add(name)
        
    def remove_listener(self, name: str) -> None:
        """Удалить слушателя по имени."""
        if name not in self._listener_names:
            return
        # Переустановим список без колбэка с таким именем — у нас нет прямого маппинга, поэтому очистим все и добавим заново
        self._listener_names.remove(name)
        # Невозможно удалить по имени без хранения пар; для простоты очистим всех и заново зарегистрируем оставшихся —
        # это допустимо, т.к. слушателей обычно немного
        old_listeners = self._listeners[:]
        self._listeners = []
        for cb in old_listeners:
            # ничего не делаем — внешняя сторона должна перерегистрировать нужных
            pass
        
    def set_current_agent(self, agent_name: str) -> None:
        """Установить текущего агента для потока."""
        self._thread_local.current_agent = agent_name
        self.pretty_logger.set_current_agent(agent_name)
        
    def get_current_agent(self) -> Optional[str]:
        """Получить текущего агента."""
        return self.pretty_logger.get_current_agent()
        
    def clear_current_agent(self) -> None:
        """Очистить текущего агента."""
        self.pretty_logger.clear_current_agent()
        
    def log(self, 
            event_type: LogEventType,
            message: str,
            agent_name: Optional[str] = None,
            tool_name: Optional[str] = None,
            duration: Optional[float] = None,
            data: Optional[Dict[str, Any]] = None,
            level: LogLevel = LogLevel.INFO,
            target: LogTarget = LogTarget.BOTH) -> None:
        """
        Основной метод логирования.
        
        Args:
            event_type: Тип события
            message: Сообщение
            agent_name: Имя агента
            tool_name: Имя инструмента
            duration: Длительность
            data: Дополнительные данные
            level: Уровень логирования
            target: Цель логирования
        """
        # Получаем текущего агента если не указан
        if agent_name is None:
            agent_name = self.get_current_agent()
            
        # Создаем событие
        event = LogEvent(
            event_type=event_type,
            message=message,
            agent_name=agent_name,
            tool_name=tool_name,
            duration=duration,
            data=data,
            level=level
        )
        
        # Логируем в консоль
        if target in [LogTarget.CONSOLE, LogTarget.BOTH] and level.value >= self.console_level.value:
            self._log_to_console(event)
            
        # Логируем в файл
        if target in [LogTarget.FILE, LogTarget.BOTH] and level.value >= self.file_level.value:
            self._log_to_file(event)
            
        # Обновляем текущее выполнение
        self._update_execution(event)
        
        # Уведомляем слушателей (защита от рекурсивного вызова)
        if not (event.data or {}).get("__enriched__"):
            if not self._notify_guard_flag:
                try:
                    self._notify_guard_flag = True
                    for listener in list(self._listeners):
                        try:
                            listener(event, self)
                        except Exception:
                            # Никогда не роняем основной поток из-за слушателя
                            pass
                finally:
                    self._notify_guard_flag = False
        
    def _log_to_console(self, event: LogEvent) -> None:
        """Логирование в консоль с красивым форматированием."""
        if event.event_type == LogEventType.AGENT_START:
            self._console_agent_start(event)
        elif event.event_type == LogEventType.AGENT_END:
            self._console_agent_end(event)
        elif event.event_type == LogEventType.TOOL_CALL:
            self._console_tool_call(event)
        elif event.event_type == LogEventType.TOOL_RESULT:
            self._console_tool_result(event)
        elif event.event_type == LogEventType.AGENT_ERROR:
            self._console_agent_error(event)
        elif event.event_type == LogEventType.PROMPT:
            self._console_prompt(event)
        elif event.event_type in (LogEventType.REASONING, LogEventType.DIFF):
            # Короткое красивое отображение обогащенного контента
            title = "План/рассуждения" if event.event_type == LogEventType.REASONING else "Изменения (diff)"
            self.pretty_logger.info(f"[{event.agent_name or 'Unknown'}] {title} — {event.message}")
            text = (event.data or {}).get("text") or (event.data or {}).get("diff")
            if text:
                # Ограничим для консоли
                preview = text if len(text) < 800 else (text[:800] + "...")
                self.pretty_logger.debug(preview)
        else:
            # Общий случай
            self.pretty_logger.info(event.message)
            
    def _console_agent_start(self, event: LogEvent) -> None:
        """Красивое отображение начала выполнения агента."""
        agent_name = event.agent_name or "Unknown"
        self.pretty_logger.info(f"Обработка сообщения агентом {agent_name}...")
        
        # Создаем операцию для отслеживания
        operation = self.pretty_logger.tool_start(
            "AgentExecution",
            agent=agent_name,
            message_length=len(event.message)
        )
        
        # Сохраняем операцию в thread-local
        self._thread_local.current_operation = operation
        
    def _console_agent_end(self, event: LogEvent) -> None:
        """Красивое отображение завершения выполнения агента."""
        agent_name = event.agent_name or "Unknown"
        duration = event.duration or 0.0
        output = event.data.get('output', '') if event.data else ''
        output_length = len(str(output)) if output is not None else 0
        
        # Получаем сохраненную операцию
        operation = getattr(self._thread_local, 'current_operation', None)
        if operation:
            self.pretty_logger.tool_result(
                operation,
                result=f"Ответ получен ({duration:.2f}с, {output_length} символов)"
            )
        else:
            self.pretty_logger.success(f"Агент {agent_name} завершил работу ({duration:.2f}с)")
            
    def _console_tool_call(self, event: LogEvent) -> None:
        """Красивое отображение вызова инструмента."""
        tool_name = event.tool_name or "Unknown"
        agent_name = event.agent_name or "Unknown"
        
        # Отображаем вызов инструмента
        if event.data and 'args' in event.data:
            args = event.data['args']
            if isinstance(args, dict):
                # Красиво форматируем аргументы
                formatted_args = []
                for key, value in args.items():
                    if isinstance(value, str) and len(value) > 50:
                        formatted_args.append(f"{key}=...({len(value)} chars)")
                    else:
                        formatted_args.append(f"{key}={value}")
                args_str = ", ".join(formatted_args)
            else:
                args_str = str(args)
        else:
            args_str = ""
            
        self.pretty_logger.info(f"[{agent_name}] {tool_name}({args_str})")
        
    def _console_tool_result(self, event: LogEvent) -> None:
        """Красивое отображение результата инструмента."""
        tool_name = event.tool_name or "Unknown"
        agent_name = event.agent_name or "Unknown"
        
        if event.data and 'result' in event.data:
            result = event.data['result']
            if isinstance(result, str) and len(result) > 100:
                result = result[:100] + "..."
            self.pretty_logger.info(f"[{agent_name}] {tool_name} → {result}")
        elif event.data and 'error' in event.data:
            error = event.data['error']
            self.pretty_logger.error(f"[{agent_name}] {tool_name} → Ошибка: {error}")
            
    def _console_agent_error(self, event: LogEvent) -> None:
        """Красивое отображение ошибки агента."""
        agent_name = event.agent_name or "Unknown"
        error_msg = event.data.get('error', 'Неизвестная ошибка') if event.data else 'Неизвестная ошибка'
        self.pretty_logger.error(f"Ошибка агента {agent_name}: {error_msg}")
        
    def _console_prompt(self, event: LogEvent) -> None:
        """Красивое отображение промпта (только в debug режиме)."""
        if self.console_level == LogLevel.DEBUG:
            agent_name = event.agent_name or "Unknown"
            prompt_type = event.data.get('prompt_type', 'unknown') if event.data else 'unknown'
            content = event.data.get('content', '') if event.data else ''
            
            if len(content) > 200:
                content = content[:200] + "..."
                
            self.pretty_logger.debug(f"[{agent_name}] Промпт ({prompt_type}): {content}")
            
    def _log_to_file(self, event: LogEvent) -> None:
        """Логирование в файл с детальной информацией."""
        # Формируем сообщение для файла
        log_message = f"{event.event_type.value.upper()} | {event.message}"
        
        if event.agent_name:
            log_message += f" | Agent: {event.agent_name}"
            
        if event.tool_name:
            log_message += f" | Tool: {event.tool_name}"
            
        if event.duration:
            log_message += f" | Duration: {event.duration:.2f}s"
            
        # Логируем в файл
        if event.level == LogLevel.ERROR:
            self.file_logger.error(log_message)
        elif event.level == LogLevel.WARNING:
            self.file_logger.warning(log_message)
        elif event.level == LogLevel.DEBUG:
            self.file_logger.debug(log_message)
        else:
            self.file_logger.info(log_message)
        
        # Специальная запись «сырого» текста для REASONING/DIFF
        if event.event_type in (LogEventType.REASONING, LogEventType.DIFF):
            raw_text = None
            if event.data:
                raw_text = event.data.get("text") or event.data.get("diff")
            if raw_text:
                # Отдельным блоком без JSON, чтобы удобно читать
                self.file_logger.info(raw_text)
        
        # Если есть дополнительные данные, логируем их отдельно
        if event.data:
            try:
                self.file_logger.debug(f"Data: {json.dumps(event.data, ensure_ascii=False, indent=2)}")
            except Exception:
                # Если данные не сериализуются
                self.file_logger.debug(f"Data: {str(event.data)}")
            
        # Принудительно сбрасываем буфер
        for handler in self.file_logger.handlers:
            handler.flush()
            
    def _update_execution(self, event: LogEvent) -> None:
        """Обновление информации о текущем выполнении."""
        if event.event_type == LogEventType.AGENT_START:
            self.current_execution = {
                'agent_name': event.agent_name,
                'start_time': event.timestamp,
                'input_message': event.message,
                'tools_used': [],
                'conversation_history': []
            }
        elif event.event_type == LogEventType.AGENT_END and self.current_execution:
            self.current_execution['end_time'] = event.timestamp
            self.current_execution['duration'] = event.duration
            self.current_execution['output_message'] = event.data.get('output', '') if event.data else ''
            
            # Сохраняем выполнение
            self._save_execution()
            
        elif event.event_type == LogEventType.TOOL_CALL and self.current_execution:
            tool_name = event.tool_name
            if tool_name and tool_name not in self.current_execution['tools_used']:
                self.current_execution['tools_used'].append(tool_name)
                
    def _save_execution(self) -> None:
        """Сохранить информацию о выполнении."""
        if not self.current_execution:
            return
            
        # Создаем имя файла
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        agent_name = self.current_execution['agent_name'] or 'unknown'
        filename = f"execution_{agent_name}_{timestamp}.json"
        filepath = self.log_dir / "agents" / filename
        
        # Сохраняем в JSON
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.current_execution, f, ensure_ascii=False, indent=2)
            
        # Очищаем текущее выполнение
        self.current_execution = None
        
    # Удобные методы для разных типов событий
    def agent_start(self, agent_name: str, message: str, **kwargs) -> None:
        """Логирование начала выполнения агента."""
        self.log(
            LogEventType.AGENT_START,
            message,
            agent_name=agent_name,
            **kwargs
        )
        
    def agent_end(self, agent_name: str, output: str, duration: float, **kwargs) -> None:
        """Логирование завершения выполнения агента."""
        self.log(
            LogEventType.AGENT_END,
            f"Агент {agent_name} завершил выполнение",
            agent_name=agent_name,
            duration=duration,
            data={'output': output},
            **kwargs
        )
        
    def agent_error(self, agent_name: str, error: Exception, **kwargs) -> None:
        """Логирование ошибки агента."""
        self.log(
            LogEventType.AGENT_ERROR,
            f"Ошибка выполнения агента {agent_name}",
            agent_name=agent_name,
            data={'error': str(error), 'error_type': type(error).__name__},
            level=LogLevel.ERROR,
            **kwargs
        )
        
    def tool_call(self, tool_name: str, args: Dict[str, Any], agent_name: Optional[str] = None, **kwargs) -> None:
        """Логирование вызова инструмента."""
        self.log(
            LogEventType.TOOL_CALL,
            f"Вызов инструмента {tool_name}",
            tool_name=tool_name,
            agent_name=agent_name,
            data={'args': args},
            **kwargs
        )
        
    def tool_result(self, tool_name: str, result: Any, agent_name: Optional[str] = None, **kwargs) -> None:
        """Логирование результата инструмента."""
        # Преобразуем результат в строку для безопасного логирования
        result_str = str(result) if result is not None else ""
        
        self.log(
            LogEventType.TOOL_RESULT,
            f"Результат инструмента {tool_name}",
            tool_name=tool_name,
            agent_name=agent_name,
            data={'result': result_str},
            **kwargs
        )
        
    def tool_error(self, tool_name: str, error: str, agent_name: Optional[str] = None, **kwargs) -> None:
        """Логирование ошибки инструмента."""
        self.log(
            LogEventType.TOOL_RESULT,
            f"Ошибка инструмента {tool_name}",
            tool_name=tool_name,
            agent_name=agent_name,
            data={'error': error},
            level=LogLevel.ERROR,
            **kwargs
        )
        
    def prompt(self, agent_name: str, prompt_type: str, content: str, **kwargs) -> None:
        """Логирование промпта."""
        self.log(
            LogEventType.PROMPT,
            f"Промпт типа {prompt_type}",
            agent_name=agent_name,
            data={'prompt_type': prompt_type, 'content': content},
            level=LogLevel.DEBUG,
            **kwargs
        )
        
    def info(self, message: str, **kwargs) -> None:
        """Общее информационное сообщение."""
        self.log(LogEventType.SYSTEM, message, level=LogLevel.INFO, **kwargs)
        
    def success(self, message: str, **kwargs) -> None:
        """Сообщение об успехе."""
        self.log(LogEventType.SYSTEM, message, level=LogLevel.SUCCESS, **kwargs)
        
    def warning(self, message: str, **kwargs) -> None:
        """Предупреждение."""
        self.log(LogEventType.SYSTEM, message, level=LogLevel.WARNING, **kwargs)
        
    def error(self, message: str, **kwargs) -> None:
        """Ошибка."""
        self.log(LogEventType.SYSTEM, message, level=LogLevel.ERROR, **kwargs)
        
    def debug(self, message: str, **kwargs) -> None:
        """Отладочное сообщение."""
        self.log(LogEventType.SYSTEM, message, level=LogLevel.DEBUG, **kwargs)


# Глобальный экземпляр логгера
_unified_logger: Optional[UnifiedLogger] = None


def get_unified_logger() -> UnifiedLogger:
    """Получить глобальный экземпляр универсального логгера."""
    global _unified_logger
    if _unified_logger is None:
        _unified_logger = UnifiedLogger()
    return _unified_logger


def configure_unified_logger(log_dir: str = "logs",
                           console_level: LogLevel = LogLevel.INFO,
                           file_level: LogLevel = LogLevel.DEBUG,
                           enable_colors: bool = True) -> UnifiedLogger:
    """Настроить глобальный универсальный логгер."""
    global _unified_logger
    _unified_logger = UnifiedLogger(
        log_dir=log_dir,
        console_level=console_level,
        file_level=file_level,
        enable_colors=enable_colors
    )
    return _unified_logger


# Удобные функции для быстрого доступа
def log_agent_start(agent_name: str, message: str, **kwargs) -> None:
    """Логирование начала агента."""
    get_unified_logger().agent_start(agent_name, message, **kwargs)


def log_agent_end(agent_name: str, output: str, duration: float, **kwargs) -> None:
    """Логирование завершения агента."""
    get_unified_logger().agent_end(agent_name, output, duration, **kwargs)


def log_agent_error(agent_name: str, error: Exception, **kwargs) -> None:
    """Логирование ошибки агента."""
    get_unified_logger().agent_error(agent_name, error, **kwargs)


def log_tool_call(tool_name: str, args: Dict[str, Any], agent_name: Optional[str] = None, **kwargs) -> None:
    """Логирование вызова инструмента."""
    get_unified_logger().tool_call(tool_name, args, agent_name, **kwargs)


def log_tool_result(tool_name: str, result: Any, agent_name: Optional[str] = None, **kwargs) -> None:
    """Логирование результата инструмента."""
    get_unified_logger().tool_result(tool_name, result, agent_name, **kwargs)


def log_tool_error(tool_name: str, error: str, agent_name: Optional[str] = None, **kwargs) -> None:
    """Логирование ошибки инструмента."""
    get_unified_logger().tool_error(tool_name, error, agent_name, **kwargs)


def log_prompt(agent_name: str, prompt_type: str, content: str, **kwargs) -> None:
    """Логирование промпта."""
    get_unified_logger().prompt(agent_name, prompt_type, content, **kwargs)


def set_current_agent(agent_name: str) -> None:
    """Установить текущего агента."""
    get_unified_logger().set_current_agent(agent_name)


def clear_current_agent() -> None:
    """Очистить текущего агента."""
    get_unified_logger().clear_current_agent()


# Регистрация слушателей (глобальные удобные функции)
def add_log_listener(name: str, callback: Callable[[LogEvent, UnifiedLogger], None]) -> None:
    get_unified_logger().add_listener(name, callback)


def remove_log_listener(name: str) -> None:
    get_unified_logger().remove_listener(name) 