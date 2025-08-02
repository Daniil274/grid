"""
Beautiful console logger for Grid system with tool tracking and rich formatting.
"""

import sys
import time
import json
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
    TODO = ("☐", "\033[94m")       # Blue


@dataclass
class TodoItem:
    """Todo item structure."""
    id: str
    content: str
    status: str  # pending, in_progress, completed
    priority: str  # high, medium, low
    
    @property
    def symbol(self) -> str:
        """Get symbol for todo status."""
        symbols = {
            "pending": "☐",
            "in_progress": "◐", 
            "completed": "☒"
        }
        return symbols.get(self.status, "☐")
    
    @property
    def color(self) -> str:
        """Get color for todo priority."""
        colors = {
            "high": "\033[91m",     # Red
            "medium": "\033[93m",   # Yellow  
            "low": "\033[90m"       # Gray
        }
        return colors.get(self.priority, "\033[90m")


@dataclass 
class ToolOperation:
    """Tool operation tracking."""
    name: str
    args: Dict[str, Any]
    result: Optional[str] = None
    error: Optional[str] = None
    duration: Optional[float] = None
    lines_changed: Optional[int] = None
    expand_hint: str = "ctrl+r to expand"


class PrettyLogger:
    """Beautiful console logger with tool tracking and rich formatting."""
    
    def __init__(self, name: str = "grid"):
        self.name = name
        self.todos: List[TodoItem] = []
        self.tools_used: List[ToolOperation] = []
        self.colors_enabled = True
        self.reset_color = "\033[0m"
        
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
        operation = ToolOperation(
            name=tool_name,
            args=kwargs
        )
        self.tools_used.append(operation)
        
        # Format tool call
        symbol = self._format_symbol(LogLevel.TOOL)
        args_str = ""
        if kwargs:
            # Format arguments nicely
            args_parts = []
            for key, value in kwargs.items():
                if isinstance(value, str) and len(value) > 50:
                    args_parts.append(f"{key}=...")
                else:
                    args_parts.append(f"{key}={value}")
            if args_parts:
                args_str = f"({', '.join(args_parts)})"
        
        print(f"{symbol} {tool_name}{args_str}")
        return operation
    
    def tool_result(self, operation: ToolOperation, result: str = None, 
                   error: str = None, lines_read: int = None, 
                   additions: int = None, removals: int = None,
                   paths_count: int = None, files_found: int = None, 
                   matches: int = None) -> None:
        """Log tool operation result."""
        operation.result = result
        operation.error = error
        
        # Format result summary
        summary_parts = []
        
        if error:
            summary_parts.append(self._colorize(f"Error: {error}", "\033[91m"))
        elif operation.name.lower() in ["read", "notebookread"]:
            if lines_read:
                summary_parts.append(f"Read {lines_read} lines")
        elif operation.name.lower() in ["edit", "multiedit", "write"]:
            if additions is not None or removals is not None:
                diff_str = self._format_diff_lines(additions or 0, removals or 0)
                summary_parts.append(f"Updated {operation.args.get('file_path', 'file')} with {diff_str}")
        elif operation.name.lower() in ["ls", "glob"]:
            if paths_count:
                summary_parts.append(f"Listed {paths_count} paths")
        elif operation.name.lower() == "grep":
            if files_found is not None and matches is not None:
                summary_parts.append(f"Found {matches} matches in {files_found} files")
            elif matches is not None:
                summary_parts.append(f"Found {matches} matches")
            else:
                summary_parts.append("Search completed")
        elif operation.name.lower() == "bash":
            if result and len(result.strip()) > 0:
                summary_parts.append("Command executed")
            else:
                summary_parts.append("Command completed")
        
        if not summary_parts and result:
            # Fallback - show truncated result
            result_preview = result[:100].replace('\n', ' ')
            if len(result) > 100:
                result_preview += "..."
            summary_parts.append(result_preview)
        
        summary = " ".join(summary_parts) if summary_parts else "Completed"
        
        # Print with indentation
        self._print_line(f"⎿  {summary} {operation.expand_hint}", 1)
        
        # Show code diff for edits
        if operation.name.lower() in ["edit", "multiedit"] and hasattr(operation, '_diff_lines'):
            for line in operation._diff_lines:
                self._print_line(line, 2)
    
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
        
        # Limit diff output
        if len(diff_lines) > 15:
            diff_lines = diff_lines[:15] + ["         ..."]
        
        operation._diff_lines = diff_lines
    
    def update_todos(self, todos: List[Dict[str, str]]) -> None:
        """Update todo list and display."""
        self.todos = [TodoItem(**todo) for todo in todos]
        self._display_todos()
    
    def _display_todos(self) -> None:
        """Display current todo list."""
        if not self.todos:
            return
            
        symbol = self._format_symbol(LogLevel.TODO)
        print(f"{symbol} Update Todos")
        
        # Group by status for better display
        for todo in self.todos:
            status_color = {
                "pending": "\033[90m",      # Gray
                "in_progress": "\033[93m",  # Yellow  
                "completed": "\033[92m"     # Green
            }.get(todo.status, "\033[90m")
            
            symbol = self._colorize(todo.symbol, status_color)
            content = todo.content
            
            # Truncate long content
            if len(content) > 60:
                content = content[:57] + "..."
                
            self._print_line(f"⎿  {symbol} {content}", 1)
    
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
            show_output = result and len(result.strip()) > 0
            summary = "Command executed successfully"
            
            if show_output and len(result) > 200:
                summary += f" ({len(result.splitlines())} lines output)"
            
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

    # Агент-специфичные методы для совместимости
    def log_agent_start(self, agent_name: str, input_data: str) -> None:
        """Log agent start."""
        symbol = self._format_symbol(LogLevel.INFO)
        input_summary = input_data[:100].replace('\n', ' ').strip()
        if len(input_data) > 100:
            input_summary += "..."
        print(f"{symbol} 🤖 {agent_name} START | 📝 {input_summary}")
    
    def log_agent_end(self, agent_name: str, output_data: str, duration: float) -> None:
        """Log agent end."""
        symbol = self._format_symbol(LogLevel.SUCCESS)
        output_summary = output_data[:100].replace('\n', ' ').strip()
        if len(output_data) > 100:
            output_summary += "..."
        print(f"{symbol} ✅ {agent_name} END (took {duration:.2f}s) | 📤 {output_summary}")
    
    def log_agent_error(self, agent_name: str, error: Exception) -> None:
        """Log agent error."""
        symbol = self._format_symbol(LogLevel.ERROR)
        print(f"{symbol} ❌ {agent_name} ERROR - {str(error)}")
    
    def log_tool_start(self, tool_name: str, args: Dict[str, Any]) -> ToolOperation:
        """Log tool start with compatibility."""
        return self.tool_start(tool_name, **args)
    
    def log_tool_end(self, tool_name: str, result: str, duration: float) -> None:
        """Log tool end with compatibility."""
        symbol = self._format_symbol(LogLevel.SUCCESS)
        result_summary = result[:100].replace('\n', ' ').strip()
        if len(result) > 100:
            result_summary += "..."
        print(f"{symbol} ✅ {tool_name} END (took {duration:.2f}s) | 📤 {result_summary}")
    
    def log_tool_error(self, tool_name: str, error: Exception) -> None:
        """Log tool error."""
        symbol = self._format_symbol(LogLevel.ERROR)
        print(f"{symbol} ❌ {tool_name} ERROR - {str(error)}")
    
    def log_communication(self, sender: str, receiver: str, message_type: str, content: str) -> None:
        """Log communication."""
        symbol = self._format_symbol(LogLevel.INFO)
        print(f"{symbol} 💬 {sender} → {receiver} ({message_type})")
    
    def log_context_change(self, context_name: str, old_value: Any, new_value: Any) -> None:
        """Log context change."""
        symbol = self._format_symbol(LogLevel.DEBUG)
        print(f"{symbol} 🔄 CONTEXT_CHANGE: {context_name}")
    
    def log_custom(self, level: str, category: str, message: str, **kwargs) -> None:
        """Log custom message."""
        level_enum = getattr(LogLevel, level.upper(), LogLevel.INFO)
        symbol = self._format_symbol(level_enum)
        print(f"{symbol} 🔹 {category.upper()}: {message}")
    
    def log_agent_prompt(self, agent_name: str, prompt: str) -> None:
        """Log agent prompt."""
        symbol = self._format_symbol(LogLevel.INFO)
        prompt_summary = prompt[:200].replace('\n', ' ').strip()
        if len(prompt) > 200:
            prompt_summary += "..."
        print(f"{symbol} 📝 {agent_name} PROMPT ({len(prompt)} chars): {prompt_summary}")


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

def log_warning(message: str) -> None:
    """Log warning message."""
    pretty_logger.warning(message)

def log_debug(message: str) -> None:
    """Log debug message."""
    pretty_logger.debug(message)

def log_tool_start(tool_name: str, **kwargs) -> ToolOperation:
    """Start tool operation tracking."""
    return pretty_logger.tool_start(tool_name, **kwargs)

def log_tool_result(operation: ToolOperation, **kwargs) -> None:
    """Log tool operation result."""
    pretty_logger.tool_result(operation, **kwargs)

def update_todos(todos: List[Dict[str, str]]) -> None:
    """Update and display todos."""
    pretty_logger.update_todos(todos)

def section_start(title: str) -> None:
    """Start new section."""
    pretty_logger.section_start(title)

# Совместимость с существующим API
def log_agent_start(agent_name: str, input_data: str) -> None:
    """Log agent start."""
    pretty_logger.log_agent_start(agent_name, input_data)

def log_agent_end(agent_name: str, output_data: str, duration: float) -> None:
    """Log agent end."""
    pretty_logger.log_agent_end(agent_name, output_data, duration)

def log_agent_error(agent_name: str, error: Exception) -> None:
    """Log agent error."""
    pretty_logger.log_agent_error(agent_name, error)

def log_tool_start_old(tool_name: str, args: Dict[str, Any]) -> ToolOperation:
    """Log tool start with old API."""
    return pretty_logger.log_tool_start(tool_name, args)

def log_tool_end_old(tool_name: str, result: str, duration: float) -> None:
    """Log tool end with old API."""
    pretty_logger.log_tool_end(tool_name, result, duration)

def log_tool_error_old(tool_name: str, error: Exception) -> None:
    """Log tool error with old API."""
    pretty_logger.log_tool_error(tool_name, error)

def log_communication(sender: str, receiver: str, message_type: str, content: str) -> None:
    """Log communication."""
    pretty_logger.log_communication(sender, receiver, message_type, content)

def log_context_change(context_name: str, old_value: Any, new_value: Any) -> None:
    """Log context change."""
    pretty_logger.log_context_change(context_name, old_value, new_value)

def log_custom(level: str, category: str, message: str, **kwargs) -> None:
    """Log custom message."""
    pretty_logger.log_custom(level, category, message, **kwargs)

def log_agent_prompt(agent_name: str, prompt: str) -> None:
    """Log agent prompt."""
    pretty_logger.log_agent_prompt(agent_name, prompt)

def log_tool_usage(tool_name: str, args: Dict[str, Any], success: bool, duration: float) -> None:
    """Log tool usage for statistics."""
    symbol = pretty_logger._format_symbol(LogLevel.SUCCESS if success else LogLevel.ERROR)
    status = "SUCCESS" if success else "FAILED"
    pretty_logger._print_line(f"{symbol} 📊 {tool_name} - {status} ({duration:.3f}s)")

def log_agent_tool_start(agent_name: str, tool_name: str, input_data: str) -> None:
    """Log agent tool start."""
    symbol = pretty_logger._format_symbol(LogLevel.TOOL)
    input_summary = input_data[:100].replace('\n', ' ').strip()
    if len(input_data) > 100:
        input_summary += "..."
    print(f"{symbol} 🔧 {agent_name} TOOL START | {tool_name} | 📝 {input_summary}")

def log_agent_tool_end(agent_name: str, tool_name: str, output_data: str, duration: float) -> None:
    """Log agent tool end."""
    symbol = pretty_logger._format_symbol(LogLevel.SUCCESS)
    output_summary = output_data[:100].replace('\n', ' ').strip()
    if len(output_data) > 100:
        output_summary += "..."
    print(f"{symbol} ✅ {agent_name} TOOL END (took {duration:.2f}s) | {tool_name} | 📤 {output_summary}")

def log_agent_tool_error(agent_name: str, tool_name: str, error: Exception) -> None:
    """Log agent tool error."""
    symbol = pretty_logger._format_symbol(LogLevel.ERROR)
    print(f"{symbol} ❌ {agent_name} TOOL ERROR | {tool_name} | {str(error)}")

# Функции-обертки для совместимости со старыми именами
def log_tool_end(tool_name: str, result: str, duration: float) -> None:
    """Log tool end - compatibility wrapper."""
    pretty_logger.log_tool_end(tool_name, result, duration)

def log_tool_error(tool_name: str, error: Exception) -> None:
    """Log tool error - compatibility wrapper."""
    pretty_logger.log_tool_error(tool_name, error)

def log_tool_start_old(tool_name: str, args: Dict[str, Any]) -> ToolOperation:
    """Log tool start with old API - compatibility wrapper."""
    return pretty_logger.log_tool_start(tool_name, args)

# Глобальный экземпляр для совместимости
logger = pretty_logger