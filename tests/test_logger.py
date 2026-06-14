"""
Unit tests for utils/logger.py module.
"""

import pytest
import tempfile
import json
import logging
from pathlib import Path
from unittest.mock import patch, Mock, mock_open
from io import StringIO

from utils.logger import Logger, JSONFormatter, LegacyFormatter, SessionLogManager, format_verbose_block


class TestLogger:
    """Test Logger class functionality."""
    
    def test_logger_creation(self):
        """Test basic logger creation."""
        logger = Logger("test_module")
        
        assert logger.name == "test_module"
        assert isinstance(logger.logger, logging.Logger)
    
    def test_logger_singleton_behavior(self):
        """Test that loggers with same name are reused."""
        logger1 = Logger("same_name")
        logger2 = Logger("same_name")
        
        # Should be the same logger instance
        assert logger1.logger is logger2.logger
    
    def test_logger_different_names(self):
        """Test that loggers with different names are different."""
        logger1 = Logger("name1")
        logger2 = Logger("name2")
        
        assert logger1.logger is not logger2.logger
        assert logger1.name != logger2.name

    def test_get_logger_normalizes_names(self):
        """Test unified logger factory normalizes names into grid.* hierarchy."""
        assert Logger.get_logger("tool").name == "grid.tool"
        assert Logger.get_logger("grid.timeline").name == "grid.timeline"
        assert Logger.get_logger("").name == "grid"
    
    def test_logger_logging_methods(self):
        """Test all logging methods."""
        logger = Logger("test")
        
        with patch.object(logger.logger, 'log') as mock_log:
            logger.debug("debug message")
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert args[0] == 10  # DEBUG level
            assert args[1] == "debug message"
        
        with patch.object(logger.logger, 'log') as mock_log:
            logger.info("info message")
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert args[0] == 20  # INFO level
            assert args[1] == "info message"
        
        with patch.object(logger.logger, 'log') as mock_log:
            logger.warning("warning message")
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert args[0] == 30  # WARNING level
            assert args[1] == "warning message"
        
        with patch.object(logger.logger, 'log') as mock_log:
            logger.error("error message")
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert args[0] == 40  # ERROR level
            assert args[1] == "error message"
    
    def test_logger_with_extra_fields(self):
        """Test logging with extra fields."""
        logger = Logger("test")
        
        with patch.object(logger.logger, 'log') as mock_log:
            logger.info("test message", extra_field1="value1", extra_field2="value2")
            
            # Check that the call was made with extra fields
            mock_log.assert_called_once()
            args, kwargs = mock_log.call_args
            assert args[0] == 20  # INFO level
            assert args[1] == "test message"
            assert 'extra' in kwargs
    
    def test_logger_setup_file_logging(self, temp_dir):
        """Test file logging setup."""
        import logging
        log_file = temp_dir / "test.log"
        
        # Create logger with file logging
        logger = Logger("test")
        logger.setup_file_logging(str(log_file))
        
        # Log a message
        logger.info("test file message")
        
        # Check that file was created and contains the message
        assert log_file.exists()
        content = log_file.read_text()
        assert "test file message" in content
        
        # Cleanup file handlers to allow temp_dir cleanup
        for handler in logger.logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                logger.logger.removeHandler(handler)
    
    def test_logger_setup_file_logging_with_level(self, temp_dir):
        """Test file logging setup with specific level."""
        import logging
        log_file = temp_dir / "test.log"
        
        logger = Logger("test")
        logger.setup_file_logging(str(log_file), level=logging.WARNING)
        
        # Log messages at different levels
        logger.info("info message")  # Should not appear
        logger.warning("warning message")  # Should appear
        
        # Check log file content
        assert log_file.exists()
        content = log_file.read_text()
        assert "warning message" in content
        assert "info message" not in content
        
        # Cleanup file handlers to allow temp_dir cleanup
        for handler in logger.logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                logger.logger.removeHandler(handler)

    def test_configured_root_handles_factory_logger(self, temp_dir):
        """Test that Logger.get_logger() works with root JSON configuration."""
        Logger.configure(
            level="INFO",
            log_dir=str(temp_dir),
            enable_console=False,
            enable_json=False,
            enable_legacy_logs=False,
            force_reconfigure=True,
        )
        logger = Logger.get_logger("compat.module")
        logger.info("compat message", extra={"extra_fields": {"request_id": "r1"}})

        grid_log = temp_dir / "grid.log"
        assert grid_log.exists()
        lines = [line for line in grid_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        payload = json.loads(lines[-1])
        assert payload["logger"] == "grid.compat.module"
        assert payload["message"] == "compat message"
        assert payload["request_id"] == "r1"


class TestJSONFormatter:
    """Test JSONFormatter class functionality."""
    
    def test_json_formatter_basic(self):
        """Test basic JSON formatting."""
        formatter = JSONFormatter()
        
        # Create a log record
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None
        )
        record.module = "test_module"
        record.funcName = "test_function"
        record.created = 1609459200.0  # Fixed timestamp for testing
        
        formatted = formatter.format(record)
        
        # Parse JSON to verify structure
        data = json.loads(formatted)
        
        assert data["level"] == "INFO"
        assert data["logger"] == "test_logger"
        assert data["message"] == "Test message"
        assert data["module"] == "test_module"
        assert data["function"] == "test_function"
        assert data["line"] == 42
        assert "timestamp" in data
    
    def test_json_formatter_with_exception(self):
        """Test JSON formatting with exception info."""
        formatter = JSONFormatter()
        
        try:
            raise ValueError("Test exception")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        
        record = logging.LogRecord(
            name="test_logger",
            level=logging.ERROR,
            pathname="test.py",
            lineno=42,
            msg="Error occurred",
            args=(),
            exc_info=exc_info
        )
        record.module = "test_module"
        record.funcName = "test_function"
        record.created = 1609459200.0
        
        formatted = formatter.format(record)
        data = json.loads(formatted)
        
        assert data["level"] == "ERROR"
        assert data["message"] == "Error occurred"
        assert "exception" in data
        assert "ValueError: Test exception" in data["exception"]
    
    def test_json_formatter_with_extra_fields(self):
        """Test JSON formatting with extra fields."""
        formatter = JSONFormatter()
        
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None
        )
        record.module = "test_module"
        record.funcName = "test_function"
        record.created = 1609459200.0
        record.extra_fields = {"custom_field": "custom_value", "agent_name": "test_agent"}
        
        formatted = formatter.format(record)
        data = json.loads(formatted)
        
        assert data["custom_field"] == "custom_value"
        assert data["agent_name"] == "test_agent"


class TestLegacyFormatter:
    """Test LegacyFormatter class functionality."""

    def test_legacy_formatter_basic(self):
        formatter = LegacyFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        record.created = 1609459200.0
        formatted = formatter.format(record)
        assert "Test message" in formatted
        assert "INFO" in formatted


class TestSessionLogManager:
    def test_uses_verbose_mirror(self):
        SessionLogManager.configure(enabled=True, level="full")
        assert SessionLogManager.uses_verbose_mirror() is True
        SessionLogManager.configure(enabled=True, level="basic")
        assert SessionLogManager.uses_verbose_mirror() is False


class TestHelpers:
    def test_format_verbose_block(self):
        formatted = format_verbose_block("TITLE", {"a": 1})
        assert "TITLE" in formatted
        assert '"a": 1' in formatted
