"""
Универсальный логгер для агентной системы с красивым отображением в терминале и детальным логированием в файлы.
"""

import threading
import json
import os
import re
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from enum import Enum

from utils.pretty_logger import PrettyLogger


class LogLevel(Enum):
    """Уровни логирования"""
    DEBUG = 10
    INFO = 20
    SUCCESS = 25
    WARNING = 30
    ERROR = 40


class LogEventType(Enum):
    """Типы событий логирования"""
    SYSTEM = "system"
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    AGENT_ERROR = "agent_error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PROMPT = "prompt"
    ERROR = "error"


class LogTarget(Enum):
    """Цели логирования"""
    CONSOLE = "console"
    FILE = "file"
    BOTH = "both"


@dataclass
class LogEvent:
    """Структура события логирования"""
    event_type: LogEventType
    message: str
    agent_name: Optional[str] = None
    tool_name: Optional[str] = None
    duration: Optional[float] = None
    data: Optional[Dict[str, Any]] = None
    level: LogLevel = LogLevel.INFO
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass 
class AgentContext:
    """Контекст выполнения агента для подробного логирования"""
    agent_name: str
    session_id: str = field(default_factory=lambda: datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    input_message: Optional[str] = None
    output_message: Optional[str] = None


class UnifiedLogger:
    """
    Универсальный логгер с красивым отображением в терминале и двойным детальным логированием в файлы.
    
    Поддерживает два типа файловых логов:
    - Основной лог: сжатый формат без истории контекста, с обрезанием больших выводов
    - Подробный лог: полный формат с историей контекста агентов
    """
    
    def __init__(self, 
                 log_dir: str = "logs",
                 console_level: LogLevel = LogLevel.INFO,
                 file_level: LogLevel = LogLevel.DEBUG,
                 enable_colors: bool = True,
                 max_output_length: int = 5000):
        """
        Инициализация универсального логгера.
        
        Args:
            log_dir: Директория для логов
            console_level: Уровень логирования для консоли
            file_level: Уровень логирования для файлов
            enable_colors: Включить цвета в консоли
            max_output_length: Максимальная длина вывода в основном логе (для обрезания)
        """
        # Принудительно использовать директорию логов проекта, а не рабочую директорию агента
        self.log_dir = Path("logs") if not Path(log_dir).is_absolute() else Path(log_dir)
        self.console_level = console_level
        self.file_level = file_level
        self.enable_colors = enable_colors
        self.max_output_length = max_output_length
        
        # Создаем директории в каталоге проекта
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "agents").mkdir(exist_ok=True)
        (self.log_dir / "prompts").mkdir(exist_ok=True)
        (self.log_dir / "conversations").mkdir(exist_ok=True)
        
        # Инициализируем компоненты
        self.pretty_logger = PrettyLogger("grid")
        self.pretty_logger.colors_enabled = enable_colors
        
        # Настройка файлового логгера (без постоянных файловых хендлеров)
        self._setup_file_logger()
        
        # Создаем пути к файлам логов
        # Используем стабильные имена файлов, чтобы упрощать анализ и соответствовать ожиданиям тестов
        self.basic_log_path = self.log_dir / "grid.log"
        self.detailed_log_path = self.log_dir / "grid_detailed.log"
        
        # Текущее выполнение и контекст агентов
        self.current_execution: Optional[Dict[str, Any]] = None
        self.conversation_history: List[Dict[str, Any]] = []
        self.agent_contexts: Dict[str, AgentContext] = {}
        
        # Thread-local storage
        self._thread_local = threading.local()

    def _sanitize_text_for_preview(self, text: str, max_len: int = 200) -> str:
        """Убрать сырые JSON-фрагменты и ограничить длину превью."""
        if not text:
            return ""
        safe = str(text)
        try:
            # Удаляем простые JSON-блоки формата {...} или [...]
            safe = re.sub(r"\{[^{}]*\}", "[json]", safe)
            safe = re.sub(r"\[[^\[\]]*\]", "[json]", safe)
        except Exception:
            pass
        if len(safe) > max_len:
            safe = safe[:max_len] + "..."
        return safe

    def _looks_like_json(self, value: str) -> bool:
        if not isinstance(value, str):
            return False
        v = value.strip()
        return (v.startswith("{") and v.endswith("}")) or (v.startswith("[") and v.endswith("]"))

    def _sanitize_data_for_file(self, data: Any, depth: int = 0) -> Any:
        """Очистить данные события для записи в файл: без сырых больших строк и JSON."""
        if data is None:
            return None
        # Ограничиваем глубину для предсказуемости
        if depth > 3:
            return "<truncated>"
        try:
            if isinstance(data, dict):
                sanitized: Dict[str, Any] = {}
                for k, v in data.items():
                    # Скрываем потенциально большие поля
                    if isinstance(v, str):
                        if self._looks_like_json(v):
                            sanitized[k] = f"<json {len(v)} chars>"
                        elif len(v) > 120:
                            sanitized[k] = f"<text {len(v)} chars>"
                        else:
                            sanitized[k] = v
                    elif isinstance(v, (list, tuple)):
                        # Не распечатываем вложенные большие структуры
                        sanitized[k] = f"{type(v).__name__}({len(v)})"
                    elif isinstance(v, dict):
                        sanitized[k] = self._sanitize_data_for_file(v, depth + 1)
                    else:
                        sanitized[k] = v
                return sanitized
            elif isinstance(data, (list, tuple)):
                return f"{type(data).__name__}({len(data)})"
            elif isinstance(data, str):
                if self._looks_like_json(data):
                    return f"<json {len(data)} chars>"
                if len(data) > 120:
                    return f"<text {len(data)} chars>"
                return data
            return data
        except Exception:
            # На всякий случай возвращаем краткое представление
            try:
                return f"<{type(data).__name__}>"
            except Exception:
                return "<data>"
    
    def _setup_file_logger(self):
        """Настройка файлового логгера без постоянных файловых хендлеров.
        Держим только именованный логгер без FileHandler, чтобы избежать блокировок файлов на Windows."""
        self.file_logger = logging.getLogger(f"grid.file.{id(self)}")
        self.file_logger.setLevel(logging.DEBUG)
        for handler in self.file_logger.handlers[:]:
            self.file_logger.removeHandler(handler)
        self.file_logger.propagate = False
        # Не добавляем FileHandler: запись в файлы выполняется эпизодически в _log_to_file
        
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
    
    def log_event(self, event: LogEvent) -> None:
        """
        Логирование готового события.
        
        Args:
            event: Событие для логирования
        """
        # Логируем в консоль
        if event.level.value >= self.console_level.value:
            self._log_to_console(event)
            
        # Логируем в файл
        if event.level.value >= self.file_level.value:
            self._log_to_file(event)
            
        # Обновляем текущее выполнение
        self._update_execution(event)
        
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
        else:
            # Общий случай
            self.pretty_logger.info(event.message)
            
    def _console_agent_start(self, event: LogEvent) -> None:
        """Красивое отображение начала выполнения агента."""
        agent_name = event.agent_name or "Unknown"
        message = event.message or ""
        
        # Красивый заголовок для начала работы агента
        print()  # Пустая строка для разделения
        print(f"🚀 {agent_name} начинает обработку:")
        
        # Также вызываем info для совместимости с тестами
        self.pretty_logger.info(f"Обработка сообщения агентом {agent_name}...")
        
        if event.data:
            message_length = len(event.data.get('message', ''))
            if message_length > 0:
                preview = str(event.data.get('message', ''))[:50]
                if len(str(event.data.get('message', ''))) > 50:
                    preview += "..."
                print(f"   📝 Сообщение: {preview}")
                print(f"   📊 Размер: {message_length} символов")
        
        # НЕ создаем AgentExecution operation чтобы избежать дублирования
        # self._thread_local.current_operation = None
        
    def _console_agent_end(self, event: LogEvent) -> None:
        """Красивое отображение завершения выполнения агента."""
        agent_name = event.agent_name or "Unknown"
        duration = float(event.duration) if event.duration else 0.0
        # Используем data['output'] если есть, иначе fallback на message
        output_value = ''
        if event.data and 'output' in (event.data or {}):
            output_value = event.data.get('output') or ''
        else:
            output_value = event.message or ''
        output_length = len(str(output_value)) if output_value is not None else 0
        
        # Красивое завершение работы агента
        print(f"⏱  Время выполнения: {duration:.2f}с")
        print(f"📤 Результат: {output_length} символов")
        
        # Добавляем разделитель
        print("─" * 60)
            
    def _console_tool_call(self, event: LogEvent) -> None:
        """Красивое отображение вызова инструмента."""
        tool_name = event.tool_name or "Unknown"
        agent_name = event.agent_name or "Unknown"
        
        # Получаем аргументы
        args = event.data.get('args', {}) if event.data else {}
        
        # Обрабатываем все типы инструментов универсально
        display_name, icon = self._get_tool_display_info(tool_name)
        
        # Основной заголовок инструмента
        print(f"◦ [{agent_name}] {icon} {display_name}")
        
        # Табулированное отображение аргументов (универсальное для любого JSON)
        if args:
            self._format_tool_arguments(args, indent="   ")
        
    def _get_tool_display_info(self, tool_name: str) -> tuple[str, str]:
        """Получить отображаемое имя и иконку для инструмента."""
        icon = "⚙️"
        display_name = tool_name
        
        if "Agent-Tool:" in tool_name:
            display_name = tool_name.replace("Agent-Tool:", "").strip()
            icon = "🤖"
        elif "MCP:" in tool_name:
            # Парсим MCP инструменты для красивого отображения
            parts = tool_name.replace("MCP:", "").split(".", 1)
            server = parts[0] if parts else "unknown"
            method = parts[1] if len(parts) > 1 else "unknown"
            
            server_icons = {
                "filesystem": "📁",
                "git": "🔀", 
                "sequential_thinking": "🧠",
                "coordinator": "🎯"
            }
            icon = server_icons.get(server, "🔧")
            display_name = f"[{server}] {method}"
            
        elif tool_name in ["sequentialthinking", "read_text_file", "write_text_file", "list_directory", "create_directory", "delete_file", "move_file"]:
            icon = "📁"
            display_name = f"[filesystem] {tool_name}"
        elif tool_name in ["git_status", "git_log", "git_diff", "git_add", "git_commit", "git_push", "git_pull", "git_set_working_dir", "git_show"]:
            icon = "🔀"
            display_name = f"[git] {tool_name}"
        elif tool_name.startswith("git_"):
            icon = "🔀"
            display_name = f"[Function] {tool_name}"
        elif tool_name.startswith("read_") or tool_name.startswith("write_") or tool_name.startswith("edit_"):
            icon = "📝"
            display_name = f"[файл] {tool_name}"
        elif "search" in tool_name.lower() or "grep" in tool_name.lower():
            icon = "🔍"
            display_name = f"[поиск] {tool_name}"
        elif "test" in tool_name.lower():
            icon = "🧪"
            display_name = f"[тест] {tool_name}"
        
        return display_name, icon
        
    def _format_tool_arguments(self, args: dict, indent: str = "   ") -> None:
        """Форматирование аргументов инструмента в табулированном виде."""
        if not args:
            return
            
        print(f"{indent}├─ 📥 Параметры:")
        
        arg_items = list(args.items())
        for i, (key, value) in enumerate(arg_items):
            is_last_arg = i == len(arg_items) - 1
            arg_prefix = "└─" if is_last_arg else "├─"
            
            # Форматируем значение в зависимости от типа
            if isinstance(value, str):
                if key.lower() == 'thought':
                    # Специальная обработка для размышлений - план в одну строку, шаги с переносами
                    lines = value.split('\n')
                    
                    # Ищем план в первой строке
                    plan_line = lines[0] if lines else ""
                    if plan_line.startswith('План:') or 'план' in plan_line.lower():
                        # План отображаем в одну строку
                        print(f"{indent}│  {arg_prefix} {key}: {plan_line}")
                        
                        # Остальные строки (шаги) с переносами
                        if len(lines) > 1:
                            for j, line in enumerate(lines[1:], 1):
                                if line.strip():
                                    step_prefix = "└─" if j == len(lines) - 1 else "├─"
                                    print(f"{indent}│     {step_prefix} {line.strip()}")
                    else:
                        # Обычное размышление - с переносами для читаемости
                        if len(value) > 80:
                            print(f"{indent}│  {arg_prefix} {key}:")
                            for j, line in enumerate(lines[:5]):
                                if line.strip():
                                    line_prefix = "└─" if j == len(lines) - 1 and len(lines) <= 5 else "├─"
                                    print(f"{indent}│     {line_prefix} {line.strip()}")
                            if len(lines) > 5:
                                print(f"{indent}│     └─ ... и ещё {len(lines) - 5} строк")
                        else:
                            print(f"{indent}│  {arg_prefix} {key}: \"{value}\"")
                elif len(value) > 80:
                    # Для длинных строк показываем превью
                    lines_count = len(value.split('\n'))
                    if lines_count > 1:
                        preview = value.split('\n')[0][:40] + "..."
                        print(f"{indent}│  {arg_prefix} {key}: \"{preview}\" ({lines_count} строк)")
                    else:
                        preview = value[:50] + "..."
                        print(f"{indent}│  {arg_prefix} {key}: \"{preview}\" ({len(value)} символов)")
                elif '\n' in value:
                    # Многострочные значения отображаем с отступами
                    print(f"{indent}│  {arg_prefix} {key}:")
                    value_lines = value.split('\n')
                    for j, line in enumerate(value_lines[:5]):  # Показываем первые 5 строк
                        if line.strip():
                            line_prefix = "└─" if j == len(value_lines) - 1 and len(value_lines) <= 5 else "├─"
                            print(f"{indent}│     {line_prefix} {line.strip()}")
                    if len(value_lines) > 5:
                        print(f"{indent}│     └─ ... и ещё {len(value_lines) - 5} строк")
                else:
                    print(f"{indent}│  {arg_prefix} {key}: \"{value}\"")
            elif isinstance(value, (dict, list)):
                # Для сложных объектов показываем структуру
                if isinstance(value, dict):
                    print(f"{indent}│  {arg_prefix} {key}: dict({len(value)} ключей)")
                    if len(value) <= 3:  # Показываем содержимое для небольших объектов
                        for j, (sub_key, sub_value) in enumerate(value.items()):
                            sub_is_last = j == len(value) - 1
                            sub_prefix = "  └─" if sub_is_last else "  ├─"
                            if isinstance(sub_value, str) and len(sub_value) > 30:
                                print(f"{indent}│     {sub_prefix} {sub_key}: \"{sub_value[:25]}...\"")
                            else:
                                print(f"{indent}│     {sub_prefix} {sub_key}: {sub_value}")
                else:
                    print(f"{indent}│  {arg_prefix} {key}: list({len(value)} элементов)")
            else:
                print(f"{indent}│  {arg_prefix} {key}: {value}")
        
    def _console_tool_result(self, event: LogEvent) -> None:
        """Красивое отображение результата инструмента с универсальным JSON парсером."""
        tool_name = event.tool_name or "Unknown"
        agent_name = event.agent_name or "Unknown"
        
        # Получаем детали результата
        result_data = event.data.get('result', '') if event.data else ''
        error_data = event.data.get('error', '') if event.data else ''
        
        # Определяем иконку
        display_name, icon = self._get_tool_display_info(tool_name)
        
        if error_data:
            print(f"● [{agent_name}] ❌ {display_name} → Ошибка:")
            print(f"   └─ {error_data}")
        elif result_data:
            # Показываем название инструмента в результате
            print(f"● [{agent_name}] {display_name} → {tool_name}:")
            self._format_result_tabulated(result_data)
            # Добавляем перенос строки для разделения шагов
            print()
        else:
            print(f"● [{agent_name}] ✅ {display_name} → Выполнено")
            print()
    
    def _format_result_tabulated(self, result: str) -> None:
        """Универсальное табулированное отображение любого результата."""
        result_str = str(result).strip()
        
        # Пытаемся распарсить JSON
        json_data = self._try_parse_json(result_str)
        
        if json_data is not None:
            # Если это JSON - фильтруем служебные поля и отображаем только содержимое
            filtered_data = self._filter_json_data(json_data)
            
            if filtered_data:
                self._print_json_tabulated(filtered_data, indent="   ")
            else:
                print(f"   └─ ✅ Выполнено")
        else:
            # Если это обычный текст - отображаем с умным форматированием
            self._format_text_content(result_str)
            
    def _filter_json_data(self, data: any) -> any:
        """Фильтрация JSON данных - убираем служебные поля, оставляем только содержимое."""
        if isinstance(data, dict):
            # Список служебных полей которые нужно скрыть
            skip_fields = {
                'type', 'meta', 'metadata', 'annotations', 'annotation', 
                'timestamp', '_type', '__type', 'version', 'schema'
            }
            
            # Если есть поле "text" - используем только его содержимое
            if 'text' in data:
                text_content = data['text']
                if isinstance(text_content, str):
                    return text_content  # Возвращаем строку для специального форматирования
                else:
                    return {'text': text_content}
            
            # Фильтруем остальные поля
            filtered = {}
            for key, value in data.items():
                if key.lower() not in skip_fields:
                    if isinstance(value, (dict, list)):
                        filtered_value = self._filter_json_data(value)
                        if filtered_value:
                            filtered[key] = filtered_value
                    else:
                        filtered[key] = value
            
            return filtered if filtered else None
            
        elif isinstance(data, list):
            filtered_list = []
            for item in data:
                filtered_item = self._filter_json_data(item)
                if filtered_item:
                    filtered_list.append(filtered_item)
            return filtered_list if filtered_list else None
            
        return data
        
    def _format_text_content(self, text: str) -> None:
        """Умное форматирование текстового содержимого."""
        lines = text.split('\n')
        lines_count = len(lines)
        
        if lines_count > 6:
            # Для длинного текста показываем превью
            print(f"   ├─ 📄 Текст ({lines_count} строк):")
            for i, line in enumerate(lines[:4]):
                if line.strip():
                    prefix = "├─" if i < 3 else "└─"
                    print(f"   {prefix} {line.strip()[:70]}")
            if lines_count > 4:
                print(f"   └─ ... и ещё {lines_count - 4} строк")
        else:
            # Короткий текст - показываем полностью
            for i, line in enumerate(lines):
                if line.strip():
                    is_last = i == lines_count - 1
                    prefix = "└─" if is_last else "├─"
                    print(f"   {prefix} {line.strip()}")
                    
            if lines_count == 1 and not lines[0].strip():
                print(f"   └─ ✅ Выполнено успешно")

    def _try_parse_json(self, text: str) -> any:
        """Попытка распарсить JSON из текста."""
        if not text:
            return None
            
        # Убираем лишние пробелы и проверяем базовые признаки JSON
        text = text.strip()
        if not (text.startswith('{') or text.startswith('[')):
            return None
            
        try:
            import json
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
            
    def _print_json_tabulated(self, data: any, indent: str = "   ", max_depth: int = 3, current_depth: int = 0) -> None:
        """Рекурсивное табулированное отображение JSON данных."""
        if current_depth >= max_depth:
            print(f"{indent}└─ ... (глубина {max_depth}+)")
            return
            
        # Если это строка (отфильтрованное поле "text") - форматируем как текст
        if isinstance(data, str):
            self._format_text_content(data)
            return
            
        if isinstance(data, dict):
            items = list(data.items())
            for i, (key, value) in enumerate(items):
                is_last = i == len(items) - 1
                prefix = "└─" if is_last else "├─"
                
                if isinstance(value, (dict, list)):
                    print(f"{indent}{prefix} {key}: {type(value).__name__}({len(value)})")
                    if len(value) > 0 and current_depth < max_depth - 1:
                        next_indent = indent + ("   " if is_last else "│  ")
                        self._print_json_tabulated(value, next_indent, max_depth, current_depth + 1)
                elif isinstance(value, str):
                    if len(value) > 80:
                        # Для длинных строк показываем превью с количеством строк
                        lines_count = len(value.split('\n'))
                        if lines_count > 1:
                            preview = value.split('\n')[0][:40] + "..."
                            print(f"{indent}{prefix} {key}: \"{preview}\" ({lines_count} строк)")
                        else:
                            preview = value[:50] + "..."
                            print(f"{indent}{prefix} {key}: \"{preview}\" ({len(value)} символов)")
                    elif '\n' in value:
                        # Многострочные значения отображаем с отступами
                        print(f"{indent}{prefix} {key}:")
                        value_lines = value.split('\n')
                        for j, line in enumerate(value_lines[:5]):  # Показываем первые 5 строк
                            if line.strip():
                                line_prefix = "└─" if j == len(value_lines) - 1 and len(value_lines) <= 5 else "├─"
                                next_indent = indent + ("   " if is_last else "│  ")
                                print(f"{next_indent}{line_prefix} {line.strip()}")
                        if len(value_lines) > 5:
                            next_indent = indent + ("   " if is_last else "│  ")
                            print(f"{next_indent}└─ ... и ещё {len(value_lines) - 5} строк")
                    else:
                        print(f"{indent}{prefix} {key}: \"{value}\"")
                elif isinstance(value, bool):
                    emoji = "✅" if value else "❌"
                    print(f"{indent}{prefix} {key}: {emoji} {value}")
                elif isinstance(value, (int, float)):
                    print(f"{indent}{prefix} {key}: {value}")
                else:
                    print(f"{indent}{prefix} {key}: {value}")
                    
        elif isinstance(data, list):
            for i, item in enumerate(data):
                is_last = i == len(data) - 1
                prefix = "└─" if is_last else "├─"
                
                if isinstance(item, (dict, list)):
                    print(f"{indent}{prefix} [{i}]: {type(item).__name__}({len(item)})")
                    if len(item) > 0 and current_depth < max_depth - 1:
                        next_indent = indent + ("   " if is_last else "│  ")
                        self._print_json_tabulated(item, next_indent, max_depth, current_depth + 1)
                elif isinstance(item, str) and len(item) > 60:
                    preview = item[:50] + "..."
                    print(f"{indent}{prefix} [{i}]: \"{preview}\"")
                else:
                    print(f"{indent}{prefix} [{i}]: {item}")
        else:
            print(f"{indent}└─ {data}")
                
    def _format_thinking_call(self, event: LogEvent) -> None:
        """Универсальное форматирование размышлений через табулированный вывод."""
        agent_name = event.agent_name or "Unknown"
        args = event.data.get('args', {}) if event.data else {}
        
        # Основной заголовок
        print(f"🧠 [{agent_name}] Размышление:")
        
        # Табулированное отображение аргументов (универсальное для любого JSON)
        if args:
            self._format_tool_arguments(args, indent="   ")
        else:
            print(f"   └─ (без параметров)")
            
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
            
            safe_preview = self._sanitize_text_for_preview(content, max_len=200)
            self.pretty_logger.debug(f"[{agent_name}] Промпт ({prompt_type}): {safe_preview}")
            
    def _format_data_as_text(self, data: Any, indent_level: int = 0) -> str:
        """Форматирует данные как обычный текст с табуляцией без JSON синтаксиса."""
        indent = "  " * indent_level
        result = ""
        
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    result += f"\n{indent}{key}:"
                    result += self._format_data_as_text(value, indent_level + 1)
                else:
                    result += f"\n{indent}{key}: {value}"
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, (dict, list)):
                    result += f"\n{indent}[{i}]:"
                    result += self._format_data_as_text(item, indent_level + 1)
                else:
                    result += f"\n{indent}- {item}"
        else:
            result += f"\n{indent}{data}"
        
        return result
    
    def _truncate_output(self, text: str, max_length: int = None) -> str:
        """Обрезать большой вывод для основного лога."""
        if max_length is None:
            max_length = self.max_output_length
        
        if not text or len(text) <= max_length:
            return text
        
        # Обрезаем с сообщением о том, что обрезано
        truncated = text[:max_length]
        return f"{truncated}... [обрезано: {len(text) - max_length} символов]"
    
    def _format_basic_log_entry(self, event: LogEvent) -> str:
        """Форматирование записи для основного (сжатого) лога."""
        timestamp = datetime.fromisoformat(event.timestamp).strftime('%Y-%m-%d %H:%M:%S')
        return f"{timestamp} | {event.event_type.name} | {event.agent_name or ''} | {event.message}"
    
    def _format_detailed_log_entry(self, event: LogEvent) -> str:
        """Форматирование записи для подробного лога с полным контекстом."""
        timestamp = datetime.fromisoformat(event.timestamp).strftime('%Y-%m-%d %H:%M:%S')
        level = event.level.name.ljust(8)
        logger_name = "grid.core.agent_factory".ljust(20)
        
        # Формируем подробное сообщение
        message = f"{event.event_type.value.upper()} | {event.message}"
        
        if event.agent_name:
            message += f" | Agent: {event.agent_name}"
        if event.tool_name:
            message += f" | Tool: {event.tool_name}"
        if event.duration:
            message += f" | Duration: {event.duration:.2f}s"
        
        # Добавляем полную информацию из data без JSON синтаксиса
        if event.data:
            try:
                message += f"\nДанные:"
                message += self._format_data_as_text(event.data, indent_level=1)
            except Exception:
                message += f"\nДанные: {str(event.data)}"
        
        # Добавляем контекст агента если доступен
        if event.agent_name and event.agent_name in self.agent_contexts:
            context = self.agent_contexts[event.agent_name]
            message += f"\nКонтекст агента:"
            message += f"\n  - Сессия: {context.session_id}"
            message += f"\n  - Сообщений в истории: {len(context.messages)}"
            if context.tools_used:
                tools_str = ", ".join(context.tools_used)
                message += f"\n  - Использованные инструменты: {tools_str}"
            else:
                message += f"\n  - Использованные инструменты: нет"
            if context.messages:
                message += f"\n  - Последние сообщения:"
                # Показываем последние 3 сообщения
                recent_messages = context.messages[-3:]
                for msg in recent_messages:
                    role_name = {"user": "Пользователь", "assistant": "Агент", "system": "Система"}.get(msg['role'], msg['role'])
                    content_preview = msg['content'][:50] + "..." if len(msg['content']) > 50 else msg['content']
                    message += f"\n    {role_name}: {content_preview}"
        
        return f"{timestamp} | {level} | {logger_name} | {message}"
    
    def _log_to_file(self, event: LogEvent) -> None:
        """Запись в оба типа логов - основной и подробный."""
        # Записываем в основной лог (сжатый формат)
        try:
            basic_entry = self._format_basic_log_entry(event)
            with open(self.basic_log_path, 'a', encoding='utf-8') as f:
                f.write(f"{basic_entry}\n")
        except Exception:
            # Не падаем на ошибках записи логов
            pass
        
        # Записываем в подробный лог (полный формат)
        try:
            detailed_entry = self._format_detailed_log_entry(event)
            with open(self.detailed_log_path, 'a', encoding='utf-8') as f:
                f.write(f"{detailed_entry}\n")
        except Exception:
            # Не падаем на ошибках записи логов
            pass
        
        # Дополнительно сохраняем промпты в отдельные файлы
        if event.event_type == LogEventType.PROMPT:
            agent_name = (event.agent_name or 'unknown').replace(' ', '_')
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            prompt_file = self.log_dir / "prompts" / f"prompt_{agent_name}_{timestamp}.txt"
            prompt_content = ""
            if event.data:
                # Поддержка ключей 'prompt' и 'content'
                if 'prompt' in event.data:
                    prompt_content = str(event.data.get('prompt') or '')
                elif 'content' in event.data:
                    prompt_content = str(event.data.get('content') or '')
            if not prompt_content:
                prompt_content = event.message
            try:
                with open(prompt_file, 'w', encoding='utf-8') as pf:
                    pf.write(prompt_content)
            except Exception:
                # Игнорируем ошибки записи промптов
                pass
        
    def _get_or_create_agent_context(self, agent_name: str, create_new_session: bool = False) -> AgentContext:
        """Получить или создать контекст агента."""
        if agent_name not in self.agent_contexts or create_new_session:
            self.agent_contexts[agent_name] = AgentContext(agent_name=agent_name)
        return self.agent_contexts[agent_name]
    
    def add_agent_message(self, agent_name: str, role: str, content: str) -> None:
        """Добавить сообщение в контекст агента."""
        if not agent_name:
            return
            
        context = self._get_or_create_agent_context(agent_name)
        context.messages.append({
            'role': role,
            'content': content,
            'timestamp': datetime.now().isoformat()
        })
    
    def add_tool_to_context(self, agent_name: str, tool_name: str) -> None:
        """Добавить инструмент в контекст агента."""
        if not agent_name or not tool_name:
            return
            
        context = self._get_or_create_agent_context(agent_name)
        if tool_name not in context.tools_used:
            context.tools_used.append(tool_name)
    
    def _update_execution(self, event: LogEvent) -> None:
        """Обновление информации о текущем выполнении и контексте агента."""
        # Обновляем legacy execution для совместимости
        if event.event_type == LogEventType.AGENT_START:
            self.current_execution = {
                'agent_name': event.agent_name,
                'start_time': event.timestamp,
                'input_message': event.message,
                'tools_used': [],
                'conversation_history': []
            }
            
            # Обновляем контекст агента (создаем новую сессию для каждого AGENT_START)
            if event.agent_name:
                context = self._get_or_create_agent_context(event.agent_name, create_new_session=True)
                context.start_time = event.timestamp
                context.input_message = event.message
                # Добавляем входящее сообщение пользователя в контекст
                self.add_agent_message(event.agent_name, "user", event.message)
                
        elif event.event_type == LogEventType.AGENT_END and self.current_execution:
            self.current_execution['end_time'] = event.timestamp
            self.current_execution['duration'] = event.duration
            self.current_execution['output_message'] = event.data.get('output', '') if event.data else ''
            
            # Обновляем контекст агента
            if event.agent_name:
                context = self._get_or_create_agent_context(event.agent_name)
                context.end_time = event.timestamp
                context.output_message = event.data.get('output', '') if event.data else ''
                # Добавляем выходящее сообщение агента в контекст
                output = event.data.get('output', '') if event.data else event.message
                self.add_agent_message(event.agent_name, "assistant", output)
            
            # Сохраняем выполнение
            self._save_execution()
            
        elif event.event_type == LogEventType.TOOL_CALL:
            # Добавляем в legacy execution
            if self.current_execution:
                tool_name = event.tool_name
                if tool_name and tool_name not in self.current_execution['tools_used']:
                    self.current_execution['tools_used'].append(tool_name)
            
            # Добавляем в контекст агента
            if event.agent_name and event.tool_name:
                self.add_tool_to_context(event.agent_name, event.tool_name)
                
        elif event.event_type == LogEventType.PROMPT:
            # Добавляем промпт в контекст как системное сообщение
            if event.agent_name and event.data:
                prompt_content = event.data.get('content', '') or event.data.get('prompt', '')
                if prompt_content:
                    self.add_agent_message(event.agent_name, "system", str(prompt_content))
                
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
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self.current_execution, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        
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
            f"Вызов инструмента {tool_name}" + (f" | Agent: {agent_name}" if agent_name else ""),
            tool_name=tool_name,
            agent_name=agent_name,
            data={'args': args},
            **kwargs
        )
        
    def tool_result(self, tool_name: str, result: Any, agent_name: Optional[str] = None, **kwargs) -> None:
        """Логирование результата инструмента."""
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

# Глобальный текущий агент
_current_agent: Optional[str] = None


def get_unified_logger() -> UnifiedLogger:
    """Получить глобальный экземпляр универсального логгера."""
    global _unified_logger
    if _unified_logger is None:
        _unified_logger = UnifiedLogger()
    return _unified_logger


def configure_unified_logger(log_dir: str = "logs",
                             console_level: LogLevel = LogLevel.INFO,
                             file_level: LogLevel = LogLevel.DEBUG,
                             enable_colors: bool = True,
                             max_output_length: int = 1000) -> UnifiedLogger:
    """
    Настроить глобальный экземпляр unified logger.
    
    Args:
        log_dir: Директория для логов
        console_level: Уровень логирования для консоли
        file_level: Уровень логирования для файлов
        enable_colors: Включить цвета в консоли
        max_output_length: Максимальная длина вывода в основном логе (для обрезания)
    
    Returns:
        Настроенный экземпляр UnifiedLogger
    """
    global _unified_logger
    _unified_logger = UnifiedLogger(
        log_dir=log_dir,
        console_level=console_level,
        file_level=file_level,
        enable_colors=enable_colors,
        max_output_length=max_output_length
    )
    return _unified_logger


# Удобные функции для быстрого доступа
def log_agent_start(agent_name: str, message: str, **kwargs) -> None:
    """Логирование начала агента."""
    event = LogEvent(
        event_type=LogEventType.AGENT_START,
        message=message,
        agent_name=agent_name,
        **kwargs
    )
    get_unified_logger().log_event(event)


def log_agent_end(agent_name: str, output: str, duration: float, **kwargs) -> None:
    """Логирование завершения агента."""
    event = LogEvent(
        event_type=LogEventType.AGENT_END,
        message=output,
        agent_name=agent_name,
        duration=duration,
        data={'output': output},
        **kwargs
    )
    get_unified_logger().log_event(event)


def log_agent_error(agent_name: str, error: Exception, **kwargs) -> None:
    """Логирование ошибки агента."""
    event = LogEvent(
        event_type=LogEventType.AGENT_ERROR,
        message=str(error),
        agent_name=agent_name,
        level=LogLevel.ERROR,
        **kwargs
    )
    get_unified_logger().log_event(event)


def log_tool_call(tool_name: str, args: Dict[str, Any], agent_name: Optional[str] = None, **kwargs) -> None:
    """Логирование вызова инструмента."""
    event = LogEvent(
        event_type=LogEventType.TOOL_CALL,
        message=f"Calling tool: {tool_name}",
        agent_name=agent_name,
        tool_name=tool_name,
        data=args,
        **kwargs
    )
    get_unified_logger().log_event(event)


def log_tool_result(tool_name: str, result: Any, agent_name: Optional[str] = None, **kwargs) -> None:
    """Логирование результата инструмента."""
    event = LogEvent(
        event_type=LogEventType.TOOL_RESULT,
        message=f"Tool result: {tool_name} - {result}",
        agent_name=agent_name,
        tool_name=tool_name,
        level=LogLevel.SUCCESS,
        data={"result": str(result) if result is not None else ""},
        **kwargs
    )
    get_unified_logger().log_event(event)


def log_tool_error(tool_name: str, error: str, agent_name: Optional[str] = None, **kwargs) -> None:
    """Логирование ошибки инструмента."""
    event = LogEvent(
        event_type=LogEventType.TOOL_RESULT,  # Используем TOOL_RESULT с ERROR level
        message=f"Tool error: {tool_name} - {error}",
        agent_name=agent_name,
        tool_name=tool_name,
        level=LogLevel.ERROR,
        data={"error": error},
        **kwargs
    )
    get_unified_logger().log_event(event)


def log_prompt(agent_name: str, content: str, prompt_type: str = "default", **kwargs) -> None:
    """Логирование промпта."""
    event = LogEvent(
        event_type=LogEventType.PROMPT,
        message=f"Prompt ({prompt_type}): {content[:100]}...",
        agent_name=agent_name,
        data={"prompt_type": prompt_type, "content": content},
        **kwargs
    )
    get_unified_logger().log_event(event)


def set_current_agent(agent_name: str) -> None:
    """Установить текущего агента."""
    global _current_agent
    _current_agent = agent_name
    get_unified_logger().set_current_agent(agent_name)


def clear_current_agent() -> None:
    """Очистить текущего агента."""
    global _current_agent
    _current_agent = None
    get_unified_logger().clear_current_agent() 