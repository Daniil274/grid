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

from utils.logger import Logger, JSONFormatter, LegacyFormatter


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
    
    def test_json_formatter_unicode_handling(self):
        """Test JSON formatter with unicode characters."""
        formatter = JSONFormatter()
        
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Тест юникода 🚀",
            args=(),
            exc_info=None
        )
        record.module = "test_module"
        record.funcName = "test_function"
        record.created = 1609459200.0
        
        formatted = formatter.format(record)
        data = json.loads(formatted)
        
        assert data["message"] == "Тест юникода 🚀"


class TestLegacyFormatter:
    """Test LegacyFormatter class functionality."""
    
    def test_legacy_formatter_basic(self):
        """Test basic legacy formatting."""
        formatter = LegacyFormatter()
        
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None
        )
        record.created = 1609459200.0  # 2021-01-01 00:00:00
        
        formatted = formatter.format(record)
        
        # Check that time is formatted correctly (accounting for local timezone)
        assert "2021-01-01" in formatted
        assert "INFO" in formatted
        assert "test_logger" in formatted
        assert "Test message" in formatted
    
    def test_legacy_formatter_padding(self):
        """Test legacy formatter padding for consistent formatting."""
        formatter = LegacyFormatter()
        
        # Test with short level name
        record = logging.LogRecord(
            name="short_name",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None
        )
        record.created = 1609459200.0
        
        formatted = formatter.format(record)
        
        # Check that level is padded to 8 characters
        parts = formatted.split(" | ")
        level_part = parts[1]
        assert len(level_part) == 8
        assert level_part.strip() == "INFO"
        
        # Check that logger name is padded to 20 characters
        logger_part = parts[2]
        assert len(logger_part) == 20
        assert logger_part.strip() == "short_name"


class TestLoggerIntegration:
    """Integration tests for logger functionality."""
    
    def test_logger_file_and_console_integration(self, temp_dir):
        """Test that logger works with both file and console output."""
        log_file = temp_dir / "integration_test.log"
        
        # Capture console output
        console_output = StringIO()
        
        # Create logger and setup handlers
        logger = Logger("integration_test")
        logger.setup_file_logging(str(log_file), level=logging.DEBUG)
        
        # Add console handler for testing
        console_handler = logging.StreamHandler(console_output)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(LegacyFormatter())
        logger.logger.addHandler(console_handler)
        
        # Log messages at different levels
        logger.debug("Debug message")
        logger.info("Info message")
        logger.warning("Warning message")
        logger.error("Error message")
        
        # Check file output (should include all levels)
        file_content = log_file.read_text()
        assert "Debug message" in file_content
        assert "Info message" in file_content
        assert "Warning message" in file_content
        assert "Error message" in file_content
        
        # Check console output (should include INFO and above)
        console_content = console_output.getvalue()
        assert "Debug message" not in console_content  # DEBUG not in console
        assert "Info message" in console_content
        assert "Warning message" in console_content
        assert "Error message" in console_content
        
        # Cleanup file handlers to allow temp_dir cleanup
        for handler in logger.logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                logger.logger.removeHandler(handler)
    
    def test_logger_with_agent_context(self, temp_dir):
        """Test logger with agent-specific context."""
        log_file = temp_dir / "agent_test.log"
        
        logger = Logger("agent_test")
        logger.setup_file_logging(str(log_file))
        
        # Log with agent context
        logger.info("Agent started", agent_name="test_agent", operation="file_read")
        logger.info("Tool called", tool_name="read_file", filepath="/test/path")
        logger.error("Agent error", agent_name="test_agent", error_type="FileNotFound")
        
        file_content = log_file.read_text()
        
        # Verify agent context is logged
        assert "test_agent" in file_content
        assert "read_file" in file_content
        assert "FileNotFound" in file_content
        
        # Cleanup file handlers to allow temp_dir cleanup
        for handler in logger.logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                logger.logger.removeHandler(handler)
    
    def test_logger_concurrent_access(self, temp_dir):
        """Test logger with concurrent access."""
        import threading
        import time
        
        log_file = temp_dir / "concurrent_test.log"
        logger = Logger("concurrent_test")
        logger.setup_file_logging(str(log_file))
        
        results = []
        
        def log_messages(thread_id):
            try:
                for i in range(10):
                    logger.info(f"Thread {thread_id} message {i}")
                    time.sleep(0.001)  # Small delay to increase chance of race conditions
                results.append("success")
            except Exception as e:
                results.append(f"error: {e}")
        
        # Start multiple threads
        threads = []
        for i in range(5):
            thread = threading.Thread(target=log_messages, args=(i,))
            threads.append(thread)
            thread.start()
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join(timeout=10)  # Таймаут 10 сек для каждого потока
            if thread.is_alive():
                pytest.fail(f"Thread {thread.name} did not finish within timeout")
        
        # All threads should succeed
        assert len(results) == 5
        assert all(result == "success" for result in results)
        
        # Check that all messages were logged
        file_content = log_file.read_text()
        for thread_id in range(5):
            for msg_id in range(10):
                assert f"Thread {thread_id} message {msg_id}" in file_content
        
        # Cleanup: remove file handlers to release file locks
        for handler in logger.logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                logger.logger.removeHandler(handler)
    
    def test_logger_large_messages(self, temp_dir):
        """Test logger with large messages."""
        log_file = temp_dir / "large_test.log"
        logger = Logger("large_test")
        logger.setup_file_logging(str(log_file))
        
        # Log a very large message
        large_message = "x" * 10000  # 10KB message
        logger.info("Large message", large_data=large_message)
        
        file_content = log_file.read_text()
        assert large_message in file_content
    
    def test_logger_special_characters(self, temp_dir):
        """Test logger with special characters and unicode."""
        log_file = temp_dir / "unicode_test.log"
        logger = Logger("unicode_test")
        logger.setup_file_logging(str(log_file))
        
        # Log messages with various special characters
        logger.info("Unicode test: Привет мир! 🌍 测试 テスト")
        logger.info("Special chars: !@#$%^&*()_+-=[]{}|;:,.<>?")
        logger.info("Newlines and\ttabs\ntest")
        
        file_content = log_file.read_text()
        assert "Привет мир! 🌍 测试 テスト" in file_content
        assert "!@#$%^&*()_+-=[]{}|;:,.<>?" in file_content
    
    def test_logger_error_handling(self, temp_dir):
        """Test logger error handling."""
        # Try to log to a read-only file
        readonly_file = temp_dir / "readonly.log"
        readonly_file.write_text("initial content")
        readonly_file.chmod(0o444)  # Make read-only
        
        logger = Logger("error_test")
        
        # This should not raise an exception, but log setup might fail silently
        try:
            logger.setup_file_logging(str(readonly_file))
            logger.info("This might not be logged")
        except Exception:
            # If exception occurs, it should be handled gracefully
            pass
        finally:
            # Restore permissions for cleanup
            readonly_file.chmod(0o644)
    
    def test_logger_rotation_behavior(self, temp_dir):
        """Test logger behavior with file rotation considerations."""
        log_file = temp_dir / "rotation_test.log"
        logger = Logger("rotation_test")
        logger.setup_file_logging(str(log_file))
        
        # Log many messages to simulate log rotation scenario
        for i in range(1000):
            logger.info(f"Message {i}")
        
        # File should exist and contain messages
        assert log_file.exists()
        file_content = log_file.read_text()
        assert "Message 0" in file_content
        assert "Message 999" in file_content
    
    def test_logger_performance(self, temp_dir):
        """Test logger performance with many messages."""
        import time
        
        log_file = temp_dir / "performance_test.log"
        logger = Logger("performance_test")
        logger.setup_file_logging(str(log_file))
        
        start_time = time.time()
        
        # Log 1000 messages
        for i in range(1000):
            logger.info(f"Performance test message {i}")
        
        end_time = time.time()
        duration = end_time - start_time
        
        # Should complete in reasonable time (adjust threshold as needed)
        assert duration < 5.0  # Should take less than 5 seconds
        
        # Verify all messages were logged
        file_content = log_file.read_text()
        assert file_content.count("Performance test message") == 1000