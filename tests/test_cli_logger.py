"""
Unit tests for utils/cli_logger.py module.
"""

import pytest
import sys
import datetime
from io import StringIO
from unittest.mock import patch, Mock

from utils.cli_logger import CLILogger, LOG_LEVELS


class TestCLILogger:
    """Test CLILogger class functionality."""
    
    def test_cli_logger_default_initialization(self):
        """Test CLILogger initialization with defaults."""
        logger = CLILogger()
        
        assert logger.level == 'INFO'
        assert logger.level_num == 20
        assert logger.stream == sys.stdout
    
    def test_cli_logger_custom_initialization(self):
        """Test CLILogger initialization with custom parameters."""
        custom_stream = StringIO()
        logger = CLILogger(level='DEBUG', stream=custom_stream)
        
        assert logger.level == 'DEBUG'
        assert logger.level_num == 10
        assert logger.stream == custom_stream
    
    def test_cli_logger_case_insensitive_level(self):
        """Test that log level is case insensitive."""
        logger = CLILogger(level='debug')
        
        assert logger.level == 'DEBUG'
        assert logger.level_num == 10
    
    def test_cli_logger_invalid_level(self):
        """Test CLILogger with invalid log level."""
        logger = CLILogger(level='INVALID')
        
        assert logger.level == 'INVALID'
        assert logger.level_num == 20  # Should default to INFO level
    
    def test_log_method_basic(self):
        """Test basic log method functionality."""
        output_stream = StringIO()
        logger = CLILogger(level='DEBUG', stream=output_stream)
        
        with patch('datetime.datetime') as mock_datetime:
            mock_datetime.now.return_value.strftime.return_value = '2021-01-01 12:00:00'
            
            logger.log('INFO', 'Test message')
            
            output = output_stream.getvalue()
            assert '2021-01-01 12:00:00 [INFO] Test message\n' == output
    
    def test_log_method_level_filtering(self):
        """Test that log method respects level filtering."""
        output_stream = StringIO()
        logger = CLILogger(level='WARNING', stream=output_stream)
        
        # These should not be logged (below WARNING level)
        logger.log('DEBUG', 'Debug message')
        logger.log('INFO', 'Info message')
        
        # These should be logged (WARNING level and above)
        logger.log('WARNING', 'Warning message')
        logger.log('ERROR', 'Error message')
        logger.log('CRITICAL', 'Critical message')
        
        output = output_stream.getvalue()
        
        assert 'Debug message' not in output
        assert 'Info message' not in output
        assert 'Warning message' in output
        assert 'Error message' in output
        assert 'Critical message' in output
    
    def test_log_method_unknown_level(self):
        """Test log method with unknown log level."""
        output_stream = StringIO()
        logger = CLILogger(level='DEBUG', stream=output_stream)
        
        logger.log('UNKNOWN', 'Test message')
        
        # Unknown level should have value 0, so should not be logged (below DEBUG=10)
        output = output_stream.getvalue()
        assert 'Test message' not in output
    
    def test_debug_method(self):
        """Test debug convenience method."""
        output_stream = StringIO()
        logger = CLILogger(level='DEBUG', stream=output_stream)
        
        logger.debug('Debug message')
        
        output = output_stream.getvalue()
        assert '[DEBUG]' in output
        assert 'Debug message' in output
    
    def test_info_method(self):
        """Test info convenience method."""
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        logger.info('Info message')
        
        output = output_stream.getvalue()
        assert '[INFO]' in output
        assert 'Info message' in output
    
    def test_warning_method(self):
        """Test warning convenience method."""
        output_stream = StringIO()
        logger = CLILogger(level='WARNING', stream=output_stream)
        
        logger.warning('Warning message')
        
        output = output_stream.getvalue()
        assert '[WARNING]' in output
        assert 'Warning message' in output
    
    def test_error_method(self):
        """Test error convenience method."""
        output_stream = StringIO()
        logger = CLILogger(level='ERROR', stream=output_stream)
        
        logger.error('Error message')
        
        output = output_stream.getvalue()
        assert '[ERROR]' in output
        assert 'Error message' in output
    
    def test_critical_method(self):
        """Test critical convenience method."""
        output_stream = StringIO()
        logger = CLILogger(level='CRITICAL', stream=output_stream)
        
        logger.critical('Critical message')
        
        output = output_stream.getvalue()
        assert '[CRITICAL]' in output
        assert 'Critical message' in output
    
    def test_multiple_messages(self):
        """Test logging multiple messages."""
        output_stream = StringIO()
        logger = CLILogger(level='DEBUG', stream=output_stream)
        
        logger.debug('First message')
        logger.info('Second message')
        logger.warning('Third message')
        
        output = output_stream.getvalue()
        lines = output.strip().split('\n')
        
        assert len(lines) == 3
        assert 'First message' in lines[0]
        assert 'Second message' in lines[1]
        assert 'Third message' in lines[2]
    
    def test_log_levels_constant(self):
        """Test LOG_LEVELS constant."""
        expected_levels = {
            'DEBUG': 10,
            'INFO': 20,
            'WARNING': 30,
            'ERROR': 40,
            'CRITICAL': 50,
        }
        
        assert LOG_LEVELS == expected_levels
    
    def test_timestamp_format(self):
        """Test timestamp format in log messages."""
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        logger.info('Test message')
        
        output = output_stream.getvalue()
        
        # Extract timestamp part (first 19 characters: YYYY-MM-DD HH:MM:SS)
        timestamp_part = output[:19]
        
        # Verify timestamp format
        try:
            datetime.datetime.strptime(timestamp_part, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pytest.fail(f"Invalid timestamp format: {timestamp_part}")
    
    def test_stream_flushing(self):
        """Test that stream is flushed after logging."""
        mock_stream = Mock()
        logger = CLILogger(level='INFO', stream=mock_stream)
        
        logger.info('Test message')
        
        # Verify that flush was called
        mock_stream.flush.assert_called_once()
    
    def test_unicode_message_handling(self):
        """Test handling of unicode characters in messages."""
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        unicode_message = 'Тест юникода 🚀 测试 テスト'
        logger.info(unicode_message)
        
        output = output_stream.getvalue()
        assert unicode_message in output
    
    def test_special_characters_in_message(self):
        """Test handling of special characters in messages."""
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        special_message = 'Special chars: !@#$%^&*()_+-=[]{}|;:,.<>?'
        logger.info(special_message)
        
        output = output_stream.getvalue()
        assert special_message in output
    
    def test_newline_in_message(self):
        """Test handling of newlines in messages."""
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        multiline_message = 'Line 1\nLine 2\nLine 3'
        logger.info(multiline_message)
        
        output = output_stream.getvalue()
        assert 'Line 1\nLine 2\nLine 3' in output
    
    def test_empty_message(self):
        """Test logging empty message."""
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        logger.info('')
        
        output = output_stream.getvalue()
        assert '[INFO] \n' in output
    
    def test_very_long_message(self):
        """Test logging very long message."""
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        long_message = 'x' * 10000  # 10KB message
        logger.info(long_message)
        
        output = output_stream.getvalue()
        assert long_message in output
    
    def test_different_log_levels_order(self):
        """Test that log levels have correct ordering."""
        assert LOG_LEVELS['DEBUG'] < LOG_LEVELS['INFO']
        assert LOG_LEVELS['INFO'] < LOG_LEVELS['WARNING']
        assert LOG_LEVELS['WARNING'] < LOG_LEVELS['ERROR']
        assert LOG_LEVELS['ERROR'] < LOG_LEVELS['CRITICAL']
    
    def test_level_boundary_conditions(self):
        """Test logging at exact level boundaries."""
        output_stream = StringIO()
        logger = CLILogger(level='WARNING', stream=output_stream)  # Level = 30
        
        # Exactly at boundary - should be logged
        logger.log('WARNING', 'At boundary')
        
        # Just below boundary - should not be logged
        logger.log('INFO', 'Below boundary')
        
        # Above boundary - should be logged
        logger.log('ERROR', 'Above boundary')
        
        output = output_stream.getvalue()
        
        assert 'At boundary' in output
        assert 'Below boundary' not in output
        assert 'Above boundary' in output
    
    def test_concurrent_logging(self):
        """Test concurrent logging from multiple threads."""
        import threading
        import time
        
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        results = []
        
        def log_from_thread(thread_id):
            try:
                for i in range(10):
                    logger.info(f'Thread {thread_id} message {i}')
                    time.sleep(0.001)  # Small delay
                results.append('success')
            except Exception as e:
                results.append(f'error: {e}')
        
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
        assert all(result == 'success' for result in results)
        
        # Check that all messages were logged
        output = output_stream.getvalue()
        for thread_id in range(5):
            for msg_id in range(10):
                assert f'Thread {thread_id} message {msg_id}' in output
    
    def test_stream_error_handling(self):
        """Test error handling when stream operations fail."""
        # Create a mock stream that raises an exception
        mock_stream = Mock()
        mock_stream.write.side_effect = IOError("Stream error")
        
        logger = CLILogger(level='INFO', stream=mock_stream)
        
        # Should not raise exception - error should be handled gracefully
        try:
            logger.info('Test message')
        except IOError:
            pytest.fail("Should handle stream errors gracefully")
    
    def test_logger_reusability(self):
        """Test that logger can be reused multiple times."""
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        # Use logger multiple times
        for i in range(100):
            logger.info(f'Message {i}')
        
        output = output_stream.getvalue()
        lines = output.strip().split('\n')
        
        assert len(lines) == 100
        assert 'Message 0' in lines[0]
        assert 'Message 99' in lines[99]
    
    def test_level_change_after_initialization(self):
        """Test changing log level after initialization."""
        output_stream = StringIO()
        logger = CLILogger(level='INFO', stream=output_stream)
        
        # Initially INFO level - debug should not be logged
        logger.debug('Debug message 1')
        logger.info('Info message 1')
        
        # Change to DEBUG level
        logger.level = 'DEBUG'
        logger.level_num = LOG_LEVELS['DEBUG']
        
        # Now debug should be logged
        logger.debug('Debug message 2')
        logger.info('Info message 2')
        
        output = output_stream.getvalue()
        
        assert 'Debug message 1' not in output
        assert 'Info message 1' in output
        assert 'Debug message 2' in output
        assert 'Info message 2' in output