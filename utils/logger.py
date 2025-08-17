"""
Enterprise-grade logging for Grid system with structured logging and multiple outputs.
"""

import logging
import sys
import json
from datetime import datetime
from typing import Any, Dict, Optional
from pathlib import Path
from functools import lru_cache


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields
        if hasattr(record, 'extra_fields'):
            log_data.update(record.extra_fields)
        
        return json.dumps(log_data, ensure_ascii=False)


class LegacyFormatter(logging.Formatter):
    """Legacy formatter for agent logs in old format."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record in legacy format."""
        timestamp = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')
        level = record.levelname.ljust(8)
        logger_name = record.name.ljust(20)
        message = record.getMessage()
        
        return f"{timestamp} | {level} | {logger_name} | {message}"


class TimestampedFileHandler(logging.FileHandler):
    """File handler that creates files with timestamps."""
    
    def __init__(self, log_dir: str, filename_prefix: str, mode: str = 'a', encoding: str = 'utf-8'):
        """
        Initialize timestamped file handler.
        
        Args:
            log_dir: Directory for log files
            filename_prefix: Prefix for log filename
            mode: File mode
            encoding: File encoding
        """
        self.log_dir = Path(log_dir)
        self.filename_prefix = filename_prefix
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Create filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{filename_prefix}_{timestamp}.log"
        filepath = self.log_dir / filename
        
        # Store filepath for reference
        self.filepath = filepath
        
        super().__init__(filepath, mode, encoding)


class Logger:
    """Centralized logger with support for structured logging."""
    
    _loggers: Dict[str, logging.Logger] = {}
    _configured = False
    
    def __init__(self, name: str):
        """Initialize logger for given name."""
        self.name = name
        self.logger = self._get_logger(name)
    
    @classmethod
    def configure(
        cls,
        level: str = "INFO",
        log_dir: Optional[str] = None,
        enable_console: bool = True,
        enable_json: bool = False,
        enable_legacy_logs: bool = True,
        force_reconfigure: bool = False
    ) -> None:
        """Configure global logging settings."""
        if cls._configured and not force_reconfigure:
            return
        
        # Clear existing handlers if reconfiguring
        if force_reconfigure:
            root_logger = logging.getLogger()
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
            cls._configured = False
        
        # Set global level
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.getLogger().setLevel(log_level)
        
        # Console handler
        if enable_console:
            console_handler = logging.StreamHandler(sys.stdout)
            if enable_json:
                console_handler.setFormatter(JSONFormatter())
            else:
                console_handler.setFormatter(
                    logging.Formatter(
                        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
                    )
                )
            logging.getLogger().addHandler(console_handler)
        
        # File handlers
        if log_dir:
            # Ensure we use the project's logs directory, not the agent's working directory
            if Path(log_dir).is_absolute():
                log_path = Path(log_dir)
            else:
                # Force use of project root logs directory
                import os
                project_root = os.environ.get('PROJECT_ROOT', '/workspaces/grid')
                log_path = Path(project_root) / "logs"
            
            log_path.mkdir(parents=True, exist_ok=True)
            
            # General log file
            file_handler = logging.FileHandler(log_path / "grid.log", encoding='utf-8')
            file_handler.setFormatter(JSONFormatter())
            logging.getLogger().addHandler(file_handler)
            
            # Error log file
            error_handler = logging.FileHandler(log_path / "grid_errors.log", encoding='utf-8')
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(JSONFormatter())
            logging.getLogger().addHandler(error_handler)
            
            # Legacy agent logs (timestamped)
            if enable_legacy_logs:
                legacy_handler = TimestampedFileHandler(str(log_path), "agents")
                legacy_handler.setFormatter(LegacyFormatter())
                logging.getLogger().addHandler(legacy_handler)
        
        cls._configured = True
    
    @classmethod
    @lru_cache(maxsize=128)
    def _get_logger(cls, name: str) -> logging.Logger:
        """Get or create logger instance."""
        if name not in cls._loggers:
            logger = logging.getLogger(f"grid.{name}")
            cls._loggers[name] = logger
        return cls._loggers[name]
    
    def debug(self, message: str, **kwargs) -> None:
        """Log debug message."""
        self._log(logging.DEBUG, message, **kwargs)
    
    def info(self, message: str, **kwargs) -> None:
        """Log info message."""
        self._log(logging.INFO, message, **kwargs)
    
    def warning(self, message: str, **kwargs) -> None:
        """Log warning message."""
        self._log(logging.WARNING, message, **kwargs)
    
    def error(self, message: str, **kwargs) -> None:
        """Log error message."""
        self._log(logging.ERROR, message, **kwargs)
    
    def critical(self, message: str, **kwargs) -> None:
        """Log critical message."""
        self._log(logging.CRITICAL, message, **kwargs)
    
    def _log(self, level: int, message: str, **kwargs) -> None:
        """Internal logging method with extra fields."""
        exc_info = None
        if "exc_info" in kwargs:
            try:
                exc_info = kwargs.pop("exc_info")
            except Exception:
                exc_info = True
        extra = {"extra_fields": kwargs} if kwargs else {}
        self.logger.log(level, message, extra=extra, exc_info=exc_info)
    
    # File logging setup for tests and integrations
    def setup_file_logging(self, file_path: str, level: int = logging.DEBUG) -> None:
        """Attach a file handler to this logger.
        Args:
            file_path: Path to the log file to write
            level: Minimum level for this handler
        """
        try:
            # Ensure parent directory exists
            path_obj = Path(file_path)
            if path_obj.parent and not path_obj.parent.exists():
                path_obj.parent.mkdir(parents=True, exist_ok=True)
            
            handler = logging.FileHandler(path_obj, encoding='utf-8')
            handler.setLevel(level)
            handler.setFormatter(LegacyFormatter())
            self.logger.addHandler(handler)
            # Keep logger level low to allow handler level filtering to work
            if self.logger.level == logging.NOTSET:
                self.logger.setLevel(logging.DEBUG)
        except Exception:
            # Swallow file handler errors to avoid crashing tests
            pass
    
    # Specialized logging methods for Grid components
    def log_agent_start(self, agent_name: str, input_message: str) -> None:
        """Log agent execution start."""
        # Legacy format logging (without emojis for compatibility)
        self.info(f"START | {input_message}")
        
        # JSON format logging
        self.info(
            f"Agent '{agent_name}' starting execution",
            agent_name=agent_name,
            input_length=len(input_message),
            event_type="agent_start"
        )
    
    def log_agent_end(self, agent_name: str, output: str, duration: float) -> None:
        """Log agent execution completion."""
        # Legacy format logging (without emojis for compatibility)
        self.info(f"END | {output} | {duration:.2f}s")
        
        # JSON format logging
        self.info(
            f"Agent '{agent_name}' completed execution",
            agent_name=agent_name,
            output_length=len(output),
            duration_seconds=duration,
            event_type="agent_end"
        )
    
    def log_agent_error(self, agent_name: str, error: Exception) -> None:
        """Log agent execution error."""
        # Legacy format logging (without emojis for compatibility)
        self.error(f"ERROR | {str(error)}")
        
        # JSON format logging
        self.error(
            f"Agent '{agent_name}' execution failed",
            agent_name=agent_name,
            error_type=type(error).__name__,
            error_message=str(error),
            event_type="agent_error"
        )
    
    def log_tool_call(self, tool_name: str, args: Dict[str, Any]) -> None:
        """Log tool call."""
        # Legacy format logging (without emojis for compatibility)
        args_str = str(args)
        self.info(f"TOOL | {tool_name} | {args_str}")
        
        # Beautiful CLI logging for MCP tools
        if tool_name.startswith("MCP:"):
            try:
                from utils.cli_logger import cli_logger
                # Extract server and method from tool name like "MCP:filesystem.read_file"
                parts = tool_name.split(".", 1)
                server_name = parts[0].replace("MCP:", "")
                method = parts[1] if len(parts) > 1 else "unknown"
                cli_logger.mcp_call(server_name, method, args)
            except ImportError:
                pass  # Fallback to regular logging if CLI logger not available
        
        # JSON format logging
        self.debug(
            f"Tool '{tool_name}' called",
            tool_name=tool_name,
            args_count=len(args),
            event_type="tool_call"
        )
    
    def log_agent_creation(self, agent_name: str, agent_display_name: str = None) -> None:
        """Log agent creation."""
        display_name = agent_display_name or agent_name
        # Legacy format logging (without emojis for compatibility)
        self.info(f"AGENT_CREATION: Creating agent '{agent_name}' ({display_name})")
        
        # JSON format logging
        self.info(
            f"Agent '{agent_name}' created",
            agent_name=agent_name,
            display_name=display_name,
            event_type="agent_creation"
        )
    
    def log_agent_tool_start(self, agent_name: str, tool_name: str, input_data: str) -> None:
        """Log agent tool execution start."""
        # Legacy format logging (without emojis for compatibility)
        self.info(f"AGENT_TOOL START | {tool_name} | {input_data}")
        
        # JSON format logging
        self.info(
            f"Agent '{agent_name}' tool '{tool_name}' starting",
            agent_name=agent_name,
            tool_name=tool_name,
            input_length=len(input_data),
            event_type="agent_tool_start"
        )
    
    def log_mcp_connection(self, server_name: str, status: str) -> None:
        """Log MCP server connection status."""
        level = logging.INFO if status == "connected" else logging.ERROR
        
        # Beautiful CLI logging
        try:
            from utils.cli_logger import cli_logger
            if status == "connected":
                cli_logger.success(f"MCP {server_name} connected", server=server_name)
            else:
                cli_logger.error(f"MCP {server_name} failed to connect", server=server_name)
        except ImportError:
            pass  # Fallback to regular logging if CLI logger not available
        
        # Regular structured logging
        self._log(
            level,
            f"MCP server '{server_name}' {status}",
            server_name=server_name,
            status=status,
            event_type="mcp_connection"
        )
    
    def log_mcp_call(self, server_name: str, method: str, params: Dict[str, Any] = None, duration: float = None, success: bool = True, error: str = None) -> None:
        """Log MCP method call with beautiful CLI formatting."""
        try:
            from utils.cli_logger import cli_logger
            operation_id = cli_logger.mcp_call(server_name, method, params or {})
            
            if success and duration is not None:
                cli_logger.operation_end(operation_id, result="completed")
            elif error:
                cli_logger.operation_end(operation_id, error=error)
        except ImportError:
            pass  # Fallback to regular logging if CLI logger not available
        
        # Regular structured logging
        level = logging.INFO if success else logging.ERROR
        message = f"MCP {server_name}.{method}"
        if error:
            message += f" failed: {error}"
        
        self._log(
            level,
            message,
            server_name=server_name,
            method=method,
            params=params,
            duration=duration,
            success=success,
            error=error,
            event_type="mcp_call"
        )
    
    def log_config_reload(self, config_path: str) -> None:
        """Log configuration reload."""
        self.info(
            f"Configuration reloaded from {config_path}",
            config_path=config_path,
            event_type="config_reload"
        )


# Legacy compatibility functions
def log_custom(level: str, category: str, message: str, **kwargs) -> None:
    """Legacy compatibility function."""
    logger = Logger(category)
    getattr(logger, level.lower(), logger.info)(message, **kwargs)


def log_agent_start(agent_name: str, input_message: str) -> None:
    """Legacy compatibility function."""
    logger = Logger("agent_execution")
    logger.log_agent_start(agent_name, input_message)


def log_agent_end(agent_name: str, output: str, duration: float) -> None:
    """Legacy compatibility function."""
    logger = Logger("agent_execution")
    logger.log_agent_end(agent_name, output, duration)


def log_agent_error(agent_name: str, error: Exception) -> None:
    """Legacy compatibility function."""
    logger = Logger("agent_execution")
    logger.log_agent_error(agent_name, error)


def log_agent_prompt(agent_name: str, prompt: str) -> None:
    """Legacy compatibility function."""
    logger = Logger("agent_prompt")
    logger.debug(f"Agent '{agent_name}' prompt built", 
                agent_name=agent_name, 
                prompt_length=len(prompt))


# Configure default logging
Logger.configure(level="INFO", enable_console=True)