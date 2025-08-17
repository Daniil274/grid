"""
Beautiful console logger for Grid system with tool tracking and rich formatting.
"""

import sys
import time
import json
import threading
from typing import Any, Dict, List, Optional, Union
from enum import Enum
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path


class LogLevel(Enum):
    """Log levels with colors and symbols."""
    DEBUG = ("🔍", "\033[90m")     # Gray
    INFO = ("●", "\033[96m")       # Cyan  
    SUCCESS = ("✓", "\033[92m")    # Green
    WARNING = ("⚠", "\033[93m")    # Yellow
    ERROR = ("✗", "\033[91m")      # Red
    TOOL = ("◦", "\033[95m")       # Magenta


# @dataclass
# class TodoItem:
#     """Todo item structure."""
#     id: str
#     content: str
#     status: str  # pending, in_progress, completed
#     priority: str  # high, medium, low
#     
#     @property
#     def symbol(self) -> str:
#         """Get symbol for todo status."""
#         symbols = {
#             "pending": "☐",
#             "in_progress": "◐", 
#             "completed": "☒"
#         }
#         return symbols.get(self.status, "☐")
#     
#     @property
#     def color(self) -> str:
#         """Get color for todo priority."""
#         colors = {
#             "high": "\033[91m",     # Red
#             "medium": "\033[93m",   # Yellow  
#             "low": "\033[90m"       # Gray
#         }
#         return colors.get(self.priority, "\033[90m")


@dataclass 
class ToolOperation:
    """Tool operation tracking."""
    name: str
    args: Dict[str, Any]
    result: Optional[str] = None
    error: Optional[str] = None
    duration: Optional[float] = None
    lines_changed: Optional[int] = None
    expand_hint: str = ""
    agent_name: Optional[str] = None


class PrettyLogger:
    """Beautiful console logger with tool tracking and rich formatting."""
    
    def __init__(self, name: str = "grid"):
        self.name = name
        self.tools_used: List[ToolOperation] = []
        self.colors_enabled = True
        self.reset_color = "\033[0m"
        # Thread-local storage for current agent
        self._thread_local = threading.local()
        
    def set_current_agent(self, agent_name: str) -> None:
        """Set current agent name for this thread."""
        self._thread_local.current_agent = agent_name
        
    def get_current_agent(self) -> Optional[str]:
        """Get current agent name for this thread."""
        return getattr(self._thread_local, 'current_agent', None)
        
    def clear_current_agent(self) -> None:
        """Clear current agent name for this thread."""
        if hasattr(self._thread_local, 'current_agent'):
            delattr(self._thread_local, 'current_agent')
        
    def _colorize(self, text: str, color: str) -> str:
        """Apply color to text if colors are enabled."""
        if not self.colors_enabled:
            return text
        return f"{color}{text}{self.reset_color}"
    
    def _format_symbol(self, level: LogLevel) -> str:
        """Format symbol with color."""
        symbol, color = level.value
        return self._colorize(symbol, color)
    
    def _print_line(self, text: str, indent: int = 0) -> None:
        """Print line with proper indentation."""
        spaces = "  " * indent
        print(f"{spaces}{text}")
    
    def _format_diff_lines(self, additions: int, removals: int) -> str:
        """Format diff summary."""
        parts = []
        if additions > 0:
            parts.append(self._colorize(f"{additions} additions", "\033[92m"))
        if removals > 0:
            parts.append(self._colorize(f"{removals} removals", "\033[91m"))
        return " and ".join(parts)
    
    def info(self, message: str) -> None:
        """Log info message."""
        symbol = self._format_symbol(LogLevel.INFO)
        print(f"{symbol} {message}")
    
    def success(self, message: str) -> None:
        """Log success message."""
        symbol = self._format_symbol(LogLevel.SUCCESS)
        print(f"{symbol} {message}")
    
    def warning(self, message: str) -> None:
        """Log warning message."""
        symbol = self._format_symbol(LogLevel.WARNING)
        print(f"{symbol} {message}")
    
    def error(self, message: str) -> None:
        """Log error message."""
        symbol = self._format_symbol(LogLevel.ERROR)
        print(f"{symbol} {message}")
    
    def debug(self, message: str) -> None:
        """Log debug message."""
        symbol = self._format_symbol(LogLevel.DEBUG)
        print(f"{symbol} {message}")
    
    def tool_start(self, tool_name: str, **kwargs) -> ToolOperation:
        """Start tracking a tool operation."""
        current_agent = self.get_current_agent()
        
        operation = ToolOperation(
            name=tool_name,
            args=kwargs,
            agent_name=current_agent
        )
        self.tools_used.append(operation)
        
        # Log to file using Logger only (avoid recursion with unified_logger)
        try:
            from utils.logger import Logger
            logger = Logger("grid.tools")
            logger.log_tool_call(tool_name, kwargs)
        except ImportError:
            pass
        
        # Format tool call with agent name if available and beautiful icons
        symbol = self._format_symbol(LogLevel.TOOL)
        
        # Определяем иконки для разных типов инструментов
        icon = "⚙️"
        display_name = tool_name
        
        if "Agent" in tool_name:
            icon = "🤖"
            display_name = tool_name
        elif tool_name.lower() in ["read", "notebookread"]:
            icon = "📖"
            display_name = f"Чтение файла"
        elif tool_name.lower() in ["edit", "multiedit", "write"]:
            icon = "✏️"
            display_name = f"Редактирование"
        elif tool_name.lower() in ["list", "ls"]:
            icon = "📁"
            display_name = f"Просмотр папки"
        elif tool_name.lower() == "bash":
            icon = "💻"
            display_name = f"Выполнение команды"
        elif tool_name.lower() in ["grep", "search"]:
            icon = "🔍"
            display_name = f"Поиск"
        elif tool_name.lower() == "glob":
            icon = "🔍"
            display_name = f"Поиск по шаблону"
        
        args_str = ""
        if kwargs:
            # Format arguments nicely with length limits
            args_parts = []
            for key, value in kwargs.items():
                if isinstance(value, str) and len(value) > 30:
                    args_parts.append(f"{key}=...({len(value)} chars)")
                elif isinstance(value, (list, dict)) and len(str(value)) > 30:
                    args_parts.append(f"{key}=...({len(str(value))} chars)")
                else:
                    args_parts.append(f"{key}={value}")
            if args_parts:
                args_str = f" ({', '.join(args_parts)})"
        
        # Show agent name if available with improved formatting
        if current_agent:
            print(f"{symbol} [{current_agent}] {icon} {display_name}{args_str}")
        else:
            print(f"{symbol} {icon} {display_name}{args_str}")
            
        return operation
    
    def tool_result(self, operation: ToolOperation, result: str = None, 
                   error: str = None, lines_read: int = None, 
                   additions: int = None, removals: int = None,
                   paths_count: int = None) -> None:
        """Log tool operation result."""
        operation.result = result
        operation.error = error
        
        # Log to file using Logger only (avoid recursion with unified_logger)
        try:
            from utils.logger import Logger
            logger = Logger("grid.tools")
            if error:
                logger.error(f"Tool '{operation.name}' failed: {error}")
            elif result:
                logger.info(f"Tool '{operation.name}' completed successfully")
        except ImportError:
            pass
        
        # Format result summary with better icons, structure and detailed information
        summary_parts = []
        
        if error:
            summary_parts.append(self._colorize(f"❌ Ошибка: {error}", "\033[91m"))
        elif operation.name.lower() in ["read", "notebookread"]:
            if lines_read:
                summary_parts.append(self._colorize(f"📖 Прочитано {lines_read} строк", "\033[92m"))
            elif result:
                # Автоматически определяем количество строк
                result_lines = len(str(result).split('\n'))
                summary_parts.append(self._colorize(f"📖 Прочитано {result_lines} строк", "\033[92m"))
        elif operation.name.lower() in ["edit", "multiedit", "write"]:
            if additions is not None or removals is not None:
                diff_str = self._format_diff_lines(additions or 0, removals or 0)
                summary_parts.append(self._colorize(f"✏️  Обновлено: {diff_str}", "\033[92m"))
            elif result:
                summary_parts.append(self._colorize(f"✏️  Файл обновлён", "\033[92m"))
        elif operation.name.lower() in ["ls", "glob", "list"]:
            if paths_count:
                summary_parts.append(self._colorize(f"📁 Найдено {paths_count} элементов", "\033[92m"))
            elif result:
                # Подсчитываем элементы автоматически
                lines = str(result).split('\n')
                items_count = len([l for l in lines if l.strip() and ('[FILE]' in l or '[DIR]' in l or '[file]' in l or '[dir]' in l)])
                if items_count > 0:
                    summary_parts.append(self._colorize(f"📁 Найдено {items_count} элементов", "\033[92m"))
                else:
                    summary_parts.append(self._colorize(f"📁 Список получен", "\033[92m"))
        elif operation.name.lower() == "bash":
            result_str = str(result) if result is not None else ""
            if result_str and len(result_str.strip()) > 0:
                lines_count = len(result_str.split('\n'))
                summary_parts.append(self._colorize(f"💻 Команда выполнена ({lines_count} строк вывода)", "\033[92m"))
            else:
                summary_parts.append(self._colorize("💻 Команда выполнена", "\033[92m"))
        elif operation.name == "AgentExecution":
            # Специальная обработка для завершения агента
            summary_parts.append(self._colorize(f"🎯 Агент завершил работу", "\033[92m"))
        elif "grep" in operation.name.lower() or "search" in operation.name.lower():
            if result:
                # Подсчитываем найденные совпадения
                result_str = str(result)
                matches = len([l for l in result_str.split('\n') if 'File:' in l])
                if matches > 0:
                    summary_parts.append(self._colorize(f"🔍 Найдено {matches} совпадений", "\033[92m"))
                else:
                    summary_parts.append(self._colorize(f"🔍 Поиск завершён", "\033[92m"))
        
        if not summary_parts and result:
            # Fallback - show abbreviated result with automatic analysis
            result_str = str(result)
            if len(result_str) > 100:
                lines_count = len(result_str.split('\n'))
                summary_parts.append(self._colorize(f"✅ Результат получен ({lines_count} строк)", "\033[92m"))
            else:
                result_preview = result_str.replace('\n', ' ')[:80]
                if len(result_str) > 80:
                    result_preview += "..."
                summary_parts.append(self._colorize(f"✅ {result_preview}", "\033[92m"))
        
        summary = " ".join(summary_parts) if summary_parts else self._colorize("✅ Выполнено", "\033[92m")
        
        # Print with beautiful indentation and formatting
        self._print_line(f"  └─ {summary} {operation.expand_hint}", 0)
        
        # Show code diff for edits with improved formatting
        if operation.name.lower() in ["edit", "multiedit"] and hasattr(operation, '_diff_lines'):
            print(f"      💾 Изменения в файле:")
            for line in operation._diff_lines:
                self._print_line(line, 1)
    
    def show_diff(self, operation: ToolOperation, old_lines: List[str], 
                  new_lines: List[str], start_line: int = 1) -> None:
        """Show formatted diff for file changes."""
        diff_lines = []
        
        # Show context around changes (simplified)
        context_lines = 3
        for i, (old, new) in enumerate(zip(old_lines, new_lines)):
            line_num = start_line + i
            
            if old != new:
                # Show removal
                if old.strip():
                    diff_lines.append(f"    {line_num:>4} -  {old.rstrip()}")
                # Show addition  
                if new.strip():
                    diff_lines.append(f"    {line_num:>4} +  {new.rstrip()}")
            elif len(diff_lines) > 0 and len(diff_lines) < 10:
                # Show some context
                diff_lines.append(f"    {line_num:>4}    {old.rstrip()}")
        
        # Show full diff output
        # No limit on diff lines
        
        operation._diff_lines = diff_lines
    
    def section_start(self, title: str) -> None:
        """Start a new section."""
        print(f"\n{self._colorize('●', '\033[96m')} {title}")
    
    def tool_bash(self, command: str, description: str = None, 
                  result: str = None, error: str = None) -> None:
        """Log bash command execution."""
        desc = description or f"Execute: {command}"
        operation = self.tool_start("Bash", command=command, description=desc)
        
        if error:
            self.tool_result(operation, error=error)
        else:
            # Determine if output should be shown
            result_str = str(result) if result is not None else ""
            show_output = result_str and len(result_str.strip()) > 0
            summary = "Command executed successfully"
            
            result_str = str(result) if result is not None else ""
            if show_output and len(result_str) > 200:
                summary += f" ({len(result_str.splitlines())} lines output)"
            
            self.tool_result(operation, result=summary)
    
    def tool_read(self, file_path: str, lines_read: int, content_preview: str = None) -> None:
        """Log file read operation."""
        operation = self.tool_start("Read", file_path=file_path)
        self.tool_result(operation, lines_read=lines_read)
    
    def tool_edit(self, file_path: str, old_string: str, new_string: str) -> None:
        """Log file edit operation."""
        operation = self.tool_start("Update", file_path=file_path)
        
        # Calculate changes (simplified)
        old_lines = old_string.split('\n')
        new_lines = new_string.split('\n') 
        additions = len([l for l in new_lines if l.strip()])
        removals = len([l for l in old_lines if l.strip()])
        
        self.tool_result(operation, additions=additions, removals=removals)
        
        # Show diff preview
        diff_lines = []
        max_lines = min(8, max(len(old_lines), len(new_lines)))
        
        for i in range(max_lines):
            line_num = i + 1  # Start from line 1
            
            if i < len(old_lines) and old_lines[i].strip():
                diff_lines.append(f"    {line_num:>4} -  {old_lines[i].rstrip()}")
            if i < len(new_lines) and new_lines[i].strip():  
                diff_lines.append(f"    {line_num:>4} +  {new_lines[i].rstrip()}")
        
        if diff_lines:
            operation._diff_lines = diff_lines
    
    def tool_ls(self, path: str, count: int) -> None:
        """Log directory listing."""
        operation = self.tool_start("List", path=path)
        self.tool_result(operation, paths_count=count)
    
    def tool_glob(self, pattern: str, count: int) -> None:
        """Log glob pattern search.""" 
        operation = self.tool_start("Glob", pattern=pattern)
        self.tool_result(operation, paths_count=count)
    
    def tool_grep(self, pattern: str, files_found: int = None, matches: int = None) -> None:
        """Log grep search."""
        operation = self.tool_start("Grep", pattern=pattern)
        
        summary = ""
        if files_found is not None:
            summary = f"Found {matches or 0} matches in {files_found} files"
        elif matches is not None:
            summary = f"Found {matches} matches"
        else:
            summary = "Search completed"
            
        self.tool_result(operation, result=summary)


# Global logger instance
pretty_logger = PrettyLogger()


# Convenience functions
def log_info(message: str) -> None:
    """Log info message."""
    pretty_logger.info(message)

def log_success(message: str) -> None:
    """Log success message."""
    pretty_logger.success(message)

def log_error(message: str) -> None:
    """Log error message."""
    pretty_logger.error(message)

def log_tool_start(tool_name: str, **kwargs) -> ToolOperation:
    """Start tool operation tracking."""
    return pretty_logger.tool_start(tool_name, **kwargs)

def log_tool_result(operation: ToolOperation, **kwargs) -> None:
    """Log tool operation result."""
    pretty_logger.tool_result(operation, **kwargs)


def section_start(title: str) -> None:
    """Start new section."""
    pretty_logger.section_start(title)

def set_current_agent(agent_name: str) -> None:
    """Set current agent name for this thread."""
    pretty_logger.set_current_agent(agent_name)

def get_current_agent() -> Optional[str]:
    """Get current agent name for this thread."""
    return pretty_logger.get_current_agent()

def clear_current_agent() -> None:
    """Clear current agent name for this thread."""
    pretty_logger.clear_current_agent()