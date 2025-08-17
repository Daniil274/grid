"""
Unit tests for utils/unified_logger.py module.
"""

import pytest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock
from datetime import datetime

from utils.unified_logger import (
    UnifiedLogger, LogLevel, LogTarget, LogEventType, LogEvent,
    log_agent_start, log_agent_end, log_agent_error,
    log_tool_call, log_tool_result, log_tool_error,
    log_prompt, set_current_agent, clear_current_agent,
    get_unified_logger
)


class TestLogEvent:
    """Test LogEvent dataclass functionality."""
    
    def test_log_event_creation(self):
        """Test creating LogEvent with required fields."""
        event = LogEvent(
            event_type=LogEventType.TOOL_CALL,
            message="Test tool call"
        )
        
        assert event.event_type == LogEventType.TOOL_CALL
        assert event.message == "Test tool call"
        assert event.agent_name is None
        assert event.tool_name is None
        assert event.duration is None
        assert event.data is None
        assert event.timestamp is not None
        assert event.level == LogLevel.INFO
    
    def test_log_event_with_all_fields(self):
        """Test creating LogEvent with all fields."""
        test_data = {"param1": "value1", "param2": "value2"}
        event = LogEvent(
            event_type=LogEventType.AGENT_START,
            message="Agent started",
            agent_name="test_agent",
            tool_name="test_tool",
            duration=1.5,
            data=test_data,
            level=LogLevel.SUCCESS
        )
        
        assert event.event_type == LogEventType.AGENT_START
        assert event.message == "Agent started"
        assert event.agent_name == "test_agent"
        assert event.tool_name == "test_tool"
        assert event.duration == 1.5
        assert event.data == test_data
        assert event.level == LogLevel.SUCCESS
    
    def test_log_event_auto_timestamp(self):
        """Test that timestamp is automatically set."""
        event = LogEvent(
            event_type=LogEventType.SYSTEM,
            message="System message"
        )
        
        # Parse timestamp to verify it's valid
        timestamp = datetime.fromisoformat(event.timestamp)
        assert isinstance(timestamp, datetime)
        
        # Should be recent (within last few seconds)
        now = datetime.now()
        diff = (now - timestamp).total_seconds()
        assert diff < 5.0


class TestUnifiedLogger:
    """Test UnifiedLogger class functionality."""
    
    def test_unified_logger_initialization(self, temp_dir):
        """Test UnifiedLogger initialization."""
        logger = UnifiedLogger(
            log_dir=str(temp_dir),
            console_level=LogLevel.INFO,
            file_level=LogLevel.DEBUG,
            enable_colors=True
        )
        
        assert logger.log_dir == temp_dir
        assert logger.console_level == LogLevel.INFO
        assert logger.file_level == LogLevel.DEBUG
        assert logger.enable_colors is True
        
        # Check that directories were created
        assert (temp_dir / "agents").exists()
        assert (temp_dir / "prompts").exists()
        assert (temp_dir / "conversations").exists()
    
    def test_unified_logger_default_initialization(self):
        """Test UnifiedLogger with default parameters."""
        logger = UnifiedLogger()
        
        assert logger.log_dir == Path("logs")
        assert logger.console_level == LogLevel.INFO
        assert logger.file_level == LogLevel.DEBUG
        assert logger.enable_colors is True
    
    @patch('utils.unified_logger.PrettyLogger')
    def test_unified_logger_pretty_logger_integration(self, mock_pretty_logger, temp_dir):
        """Test integration with PrettyLogger."""
        mock_pretty_instance = Mock()
        mock_pretty_logger.return_value = mock_pretty_instance
        
        logger = UnifiedLogger(log_dir=str(temp_dir), enable_colors=False)
        
        # Verify PrettyLogger was initialized correctly
        mock_pretty_logger.assert_called_once_with("grid")
        assert mock_pretty_instance.colors_enabled is False
    
    def test_unified_logger_log_event(self, temp_dir):
        """Test logging events through UnifiedLogger."""
        logger = UnifiedLogger(log_dir=str(temp_dir))
        
        event = LogEvent(
            event_type=LogEventType.TOOL_CALL,
            message="Test tool call",
            tool_name="test_tool",
            data={"param": "value"}
        )
        
        with patch.object(logger, '_log_to_console') as mock_console:
            with patch.object(logger, '_log_to_file') as mock_file:
                logger.log_event(event)
                
                mock_console.assert_called_once_with(event)
                mock_file.assert_called_once_with(event)
    
    def test_unified_logger_level_filtering_console(self, temp_dir):
        """Test level filtering for console output."""
        logger = UnifiedLogger(
            log_dir=str(temp_dir),
            console_level=LogLevel.WARNING  # Only WARNING and above to console
        )
        
        debug_event = LogEvent(LogEventType.SYSTEM, "Debug", level=LogLevel.DEBUG)
        info_event = LogEvent(LogEventType.SYSTEM, "Info", level=LogLevel.INFO)
        warning_event = LogEvent(LogEventType.SYSTEM, "Warning", level=LogLevel.WARNING)
        
        with patch.object(logger, '_log_to_console') as mock_console:
            with patch.object(logger, '_log_to_file') as mock_file:
                logger.log_event(debug_event)
                logger.log_event(info_event)
                logger.log_event(warning_event)
                
                # Console should only receive WARNING
                assert mock_console.call_count == 1
                mock_console.assert_called_with(warning_event)
                
                # File should receive all events
                assert mock_file.call_count == 3
    
    def test_unified_logger_level_filtering_file(self, temp_dir):
        """Test level filtering for file output."""
        logger = UnifiedLogger(
            log_dir=str(temp_dir),
            file_level=LogLevel.ERROR  # Only ERROR and above to file
        )
        
        info_event = LogEvent(LogEventType.SYSTEM, "Info", level=LogLevel.INFO)
        error_event = LogEvent(LogEventType.SYSTEM, "Error", level=LogLevel.ERROR)
        
        with patch.object(logger, '_log_to_console') as mock_console:
            with patch.object(logger, '_log_to_file') as mock_file:
                logger.log_event(info_event)
                logger.log_event(error_event)
                
                # Console should receive both (default level is INFO)
                assert mock_console.call_count == 2
                
                # File should only receive ERROR
                assert mock_file.call_count == 1
                mock_file.assert_called_with(error_event)


class TestUnifiedLoggerGlobalFunctions:
    """Test global logging functions."""
    
    @patch('utils.unified_logger.get_unified_logger')
    def test_log_agent_start(self, mock_get_logger):
        """Test log_agent_start function."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        log_agent_start("test_agent", "Test task")
        
        mock_logger.log_event.assert_called_once()
        event = mock_logger.log_event.call_args[0][0]
        assert event.event_type == LogEventType.AGENT_START
        assert event.agent_name == "test_agent"
        assert "Test task" in event.message
    
    @patch('utils.unified_logger.get_unified_logger')
    def test_log_agent_end(self, mock_get_logger):
        """Test log_agent_end function."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        log_agent_end("test_agent", 2.5, "Success")
        
        mock_logger.log_event.assert_called_once()
        event = mock_logger.log_event.call_args[0][0]
        assert event.event_type == LogEventType.AGENT_END
        assert event.agent_name == "test_agent"
        assert event.duration == 2.5
        assert "Success" in event.message
    
    @patch('utils.unified_logger.get_unified_logger')
    def test_log_agent_error(self, mock_get_logger):
        """Test log_agent_error function."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        log_agent_error("test_agent", "Test error")
        
        mock_logger.log_event.assert_called_once()
        event = mock_logger.log_event.call_args[0][0]
        assert event.event_type == LogEventType.AGENT_ERROR
        assert event.agent_name == "test_agent"
        assert event.level == LogLevel.ERROR
        assert "Test error" in event.message
    
    @patch('utils.unified_logger.get_unified_logger')
    def test_log_tool_call(self, mock_get_logger):
        """Test log_tool_call function."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        tool_data = {"param1": "value1", "param2": "value2"}
        log_tool_call("test_tool", tool_data)
        
        mock_logger.log_event.assert_called_once()
        event = mock_logger.log_event.call_args[0][0]
        assert event.event_type == LogEventType.TOOL_CALL
        assert event.tool_name == "test_tool"
        assert event.data == tool_data
    
    @patch('utils.unified_logger.get_unified_logger')
    def test_log_tool_result(self, mock_get_logger):
        """Test log_tool_result function."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        log_tool_result("test_tool", "Operation successful")
        
        mock_logger.log_event.assert_called_once()
        event = mock_logger.log_event.call_args[0][0]
        assert event.event_type == LogEventType.TOOL_RESULT
        assert event.tool_name == "test_tool"
        assert event.level == LogLevel.SUCCESS
        assert "Operation successful" in event.message
    
    @patch('utils.unified_logger.get_unified_logger')
    def test_log_tool_error(self, mock_get_logger):
        """Test log_tool_error function."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        log_tool_error("test_tool", "Tool failed")
        
        mock_logger.log_event.assert_called_once()
        event = mock_logger.log_event.call_args[0][0]
        assert event.event_type == LogEventType.TOOL_RESULT
        assert event.tool_name == "test_tool"
        assert event.level == LogLevel.ERROR
        assert "Tool failed" in event.message
    
    @patch('utils.unified_logger.get_unified_logger')
    def test_log_prompt(self, mock_get_logger):
        """Test log_prompt function."""
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        log_prompt("test_agent", "Test prompt content")
        
        mock_logger.log_event.assert_called_once()
        event = mock_logger.log_event.call_args[0][0]
        assert event.event_type == LogEventType.PROMPT
        assert event.agent_name == "test_agent"
        assert "Test prompt content" in event.message
    
    def test_set_and_clear_current_agent(self):
        """Test set_current_agent and clear_current_agent functions."""
        import utils.unified_logger as ul
        
        # Initially no current agent
        assert not hasattr(ul, '_current_agent') or ul._current_agent is None
        
        # Set current agent
        set_current_agent("test_agent")
        assert ul._current_agent == "test_agent"
        
        # Clear current agent
        clear_current_agent()
        assert ul._current_agent is None
    
    def test_get_unified_logger_singleton(self):
        """Test that get_unified_logger returns singleton."""
        logger1 = get_unified_logger()
        logger2 = get_unified_logger()
        
        assert logger1 is logger2


class TestUnifiedLoggerFileOperations:
    """Test file operations of UnifiedLogger."""
    
    def test_log_to_file_agent_event(self, temp_dir):
        """Test logging agent events to file."""
        logger = UnifiedLogger(log_dir=str(temp_dir))
        
        event = LogEvent(
            event_type=LogEventType.AGENT_START,
            message="Agent started",
            agent_name="test_agent",
            data={"task": "test_task"}
        )
        
        logger._log_to_file(event)
        
        # Check that agent-specific log file was created
        agent_files = list((temp_dir / "agents").glob("*test_agent*.json"))
        assert len(agent_files) > 0
        
        # Verify file content
        with open(agent_files[0], 'r') as f:
            logged_data = json.load(f)
        
        assert logged_data["event_type"] == "agent_start"
        assert logged_data["agent_name"] == "test_agent"
        assert logged_data["message"] == "Agent started"
    
    def test_log_to_file_prompt_event(self, temp_dir):
        """Test logging prompt events to file."""
        logger = UnifiedLogger(log_dir=str(temp_dir))
        
        event = LogEvent(
            event_type=LogEventType.PROMPT,
            message="Prompt sent",
            agent_name="test_agent",
            data={"prompt": "Test prompt content"}
        )
        
        logger._log_to_file(event)
        
        # Check that prompt file was created
        prompt_files = list((temp_dir / "prompts").glob("*test_agent*.txt"))
        assert len(prompt_files) > 0
        
        # Verify file content
        content = prompt_files[0].read_text()
        assert "Test prompt content" in content
    
    def test_log_to_file_general_event(self, temp_dir):
        """Test logging general events to main log file."""
        logger = UnifiedLogger(log_dir=str(temp_dir))
        
        event = LogEvent(
            event_type=LogEventType.SYSTEM,
            message="System event",
            data={"component": "test"}
        )
        
        logger._log_to_file(event)
        
        # Check main log file
        main_log = temp_dir / "grid.log"
        assert main_log.exists()
        
        content = main_log.read_text()
        assert "System event" in content


class TestUnifiedLoggerConsoleOperations:
    """Test console operations of UnifiedLogger."""
    
    @patch('utils.unified_logger.PrettyLogger')
    def test_log_to_console_tool_events(self, mock_pretty_logger, temp_dir):
        """Test logging tool events to console."""
        mock_pretty_instance = Mock()
        mock_pretty_logger.return_value = mock_pretty_instance
        
        logger = UnifiedLogger(log_dir=str(temp_dir))
        
        # Tool call event
        tool_call_event = LogEvent(
            event_type=LogEventType.TOOL_CALL,
            message="Tool called",
            tool_name="test_tool",
            data={"param": "value"}
        )
        
        logger._log_to_console(tool_call_event)
        
        # Should call pretty logger's tool methods
        mock_pretty_instance.tool_start.assert_called_once()
    
    @patch('utils.unified_logger.PrettyLogger')
    def test_log_to_console_agent_events(self, mock_pretty_logger, temp_dir):
        """Test logging agent events to console."""
        mock_pretty_instance = Mock()
        mock_pretty_logger.return_value = mock_pretty_instance
        
        logger = UnifiedLogger(log_dir=str(temp_dir))
        
        # Agent start event
        agent_event = LogEvent(
            event_type=LogEventType.AGENT_START,
            message="Agent started",
            agent_name="test_agent"
        )
        
        logger._log_to_console(agent_event)
        
        # Should log agent start
        mock_pretty_instance.info.assert_called()


class TestUnifiedLoggerErrorHandling:
    """Test error handling in UnifiedLogger."""
    
    def test_file_logging_permission_error(self, temp_dir):
        """Test handling of file permission errors."""
        # Create read-only directory
        readonly_dir = temp_dir / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)
        
        logger = UnifiedLogger(log_dir=str(readonly_dir))
        
        event = LogEvent(
            event_type=LogEventType.SYSTEM,
            message="Test message"
        )
        
        # Should not raise exception
        try:
            logger._log_to_file(event)
        except Exception:
            pytest.fail("Should handle permission errors gracefully")
        finally:
            # Restore permissions for cleanup
            readonly_dir.chmod(0o755)
    
    def test_invalid_json_data_handling(self, temp_dir):
        """Test handling of non-serializable data."""
        logger = UnifiedLogger(log_dir=str(temp_dir))
        
        # Create event with non-serializable data
        non_serializable_data = {"func": lambda x: x}  # Functions are not JSON serializable
        event = LogEvent(
            event_type=LogEventType.TOOL_CALL,
            message="Test with non-serializable data",
            data=non_serializable_data
        )
        
        # Should handle gracefully
        try:
            logger._log_to_file(event)
        except Exception:
            pytest.fail("Should handle non-serializable data gracefully")
    
    def test_console_logging_error_handling(self, temp_dir):
        """Test console logging error handling."""
        with patch('utils.unified_logger.PrettyLogger') as mock_pretty_logger:
            mock_pretty_instance = Mock()
            mock_pretty_instance.info.side_effect = Exception("Console error")
            mock_pretty_logger.return_value = mock_pretty_instance
            
            logger = UnifiedLogger(log_dir=str(temp_dir))
            
            event = LogEvent(
                event_type=LogEventType.SYSTEM,
                message="Test message"
            )
            
            # Should not raise exception
            try:
                logger._log_to_console(event)
            except Exception:
                pytest.fail("Should handle console errors gracefully")


class TestUnifiedLoggerIntegration:
    """Integration tests for UnifiedLogger."""
    
    def test_end_to_end_logging_workflow(self, temp_dir):
        """Test complete logging workflow."""
        logger = UnifiedLogger(log_dir=str(temp_dir))
        
        # Simulate agent workflow
        set_current_agent("test_agent")
        
        log_agent_start("test_agent", "Starting test task")
        log_tool_call("file_read", {"path": "/test/file.txt"})
        log_tool_result("file_read", "File read successfully")
        log_tool_call("file_write", {"path": "/test/output.txt", "content": "data"})
        log_tool_error("file_write", "Permission denied")
        log_agent_error("test_agent", "Task failed due to permission error")
        log_agent_end("test_agent", 5.2, "Failed")
        
        clear_current_agent()
        
        # Verify files were created
        assert (temp_dir / "grid.log").exists()
        agent_files = list((temp_dir / "agents").glob("*test_agent*.json"))
        assert len(agent_files) > 0
        
        # Verify content in main log
        main_log_content = (temp_dir / "grid.log").read_text()
        assert "test_agent" in main_log_content
        assert "file_read" in main_log_content
        assert "Permission denied" in main_log_content
    
    def test_concurrent_logging(self, temp_dir):
        """Test concurrent logging from multiple threads."""
        import threading
        import time
        
        logger = UnifiedLogger(log_dir=str(temp_dir))
        results = []
        
        def log_from_thread(thread_id):
            try:
                agent_name = f"agent_{thread_id}"
                set_current_agent(agent_name)
                
                for i in range(10):
                    log_tool_call(f"tool_{i}", {"thread": thread_id, "iteration": i})
                    log_tool_result(f"tool_{i}", f"Result from thread {thread_id}")
                    time.sleep(0.001)
                
                clear_current_agent()
                results.append("success")
            except Exception as e:
                results.append(f"error: {e}")
        
        # Start multiple threads
        threads = []
        for i in range(5):
            thread = threading.Thread(target=log_from_thread, args=(i,))
            threads.append(thread)
            thread.start()
        
        # Wait for completion
        for thread in threads:
            thread.join()
        
        # All threads should succeed
        assert len(results) == 5
        assert all(result == "success" for result in results)
        
        # Main log should contain entries from all threads
        main_log_content = (temp_dir / "grid.log").read_text()
        for thread_id in range(5):
            assert f"agent_{thread_id}" in main_log_content