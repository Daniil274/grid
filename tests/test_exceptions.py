"""
Unit tests for utils/exceptions.py module - simplified version.
"""

import pytest
from utils.exceptions import (
    GridError, ConfigError, AgentError, ContextError, ToolError
)


class TestGridError:
    """Test base GridError exception."""
    
    def test_grid_error_basic(self):
        """Test basic GridError creation."""
        error = GridError("Test error message")
        
        assert str(error) == "Test error message"
        assert error.message == "Test error message"
        assert error.details == {}
    
    def test_grid_error_with_details(self):
        """Test GridError with details."""
        details = {"field": "value", "error_type": "validation"}
        error = GridError("Test error", details=details)
        
        assert error.message == "Test error"
        assert error.details == details
    
    def test_grid_error_inheritance(self):
        """Test that GridError inherits from Exception."""
        error = GridError("Test")
        
        assert isinstance(error, Exception)
        assert isinstance(error, GridError)


class TestConfigError:
    """Test ConfigError exception."""
    
    def test_config_error_inheritance(self):
        """Test ConfigError inherits from GridError."""
        error = ConfigError("Config error")
        
        assert isinstance(error, GridError)
        assert isinstance(error, ConfigError)
    
    def test_config_error_basic(self):
        """Test basic ConfigError functionality."""
        error = ConfigError("Invalid configuration")
        
        assert str(error) == "Invalid configuration"
        assert error.message == "Invalid configuration"
    
    def test_config_error_with_details(self):
        """Test ConfigError with configuration details."""
        details = {
            "config_file": "config.yaml",
            "invalid_field": "providers.openai.timeout",
            "expected_type": "int",
            "actual_value": "invalid"
        }
        error = ConfigError("Configuration validation failed", details=details)
        
        assert error.message == "Configuration validation failed"
        assert error.details == details
        assert error.details["config_file"] == "config.yaml"


class TestAgentError:
    """Test AgentError exception."""
    
    def test_agent_error_inheritance(self):
        """Test AgentError inherits from GridError."""
        error = AgentError("Agent error")
        
        assert isinstance(error, GridError)
        assert isinstance(error, AgentError)
    
    def test_agent_error_basic(self):
        """Test basic AgentError functionality."""
        error = AgentError("Agent execution failed")
        
        assert str(error) == "Agent execution failed"
        assert error.message == "Agent execution failed"
    
    def test_agent_error_with_agent_details(self):
        """Test AgentError with agent-specific details."""
        details = {
            "agent_name": "file_agent",
            "agent_model": "gpt-4",
            "execution_time": 30.5,
            "tool_calls": ["file_read", "file_write"],
            "error_stage": "tool_execution"
        }
        error = AgentError("Tool execution failed", details=details)
        
        assert error.message == "Tool execution failed"
        assert error.details == details
        assert error.details["agent_name"] == "file_agent"
        assert error.details["tool_calls"] == ["file_read", "file_write"]


class TestContextError:
    """Test ContextError exception."""
    
    def test_context_error_inheritance(self):
        """Test ContextError inherits from GridError."""
        error = ContextError("Context error")
        
        assert isinstance(error, GridError)
        assert isinstance(error, ContextError)
    
    def test_context_error_basic(self):
        """Test basic ContextError functionality."""
        error = ContextError("Context management failed")
        
        assert str(error) == "Context management failed"
        assert error.message == "Context management failed"
    
    def test_context_error_with_context_details(self):
        """Test ContextError with context-specific details."""
        details = {
            "context_size": 15000,
            "max_context_size": 10000,
            "messages_count": 50,
            "operation": "add_message"
        }
        error = ContextError("Context size exceeded", details=details)
        
        assert error.message == "Context size exceeded"
        assert error.details == details
        assert error.details["context_size"] == 15000
        assert error.details["operation"] == "add_message"


class TestToolError:
    """Test ToolError exception."""
    
    def test_tool_error_inheritance(self):
        """Test ToolError inherits from GridError."""
        error = ToolError("Tool error")
        
        assert isinstance(error, GridError)
        assert isinstance(error, ToolError)
    
    def test_tool_error_basic(self):
        """Test basic ToolError functionality."""
        error = ToolError("Tool execution failed")
        
        assert str(error) == "Tool execution failed"
        assert error.message == "Tool execution failed"
    
    def test_tool_error_with_tool_details(self):
        """Test ToolError with tool-specific details."""
        details = {
            "tool_name": "git_status",
            "tool_type": "function",
            "args": {"directory": "/invalid/path"},
            "exit_code": 1,
            "stderr": "fatal: not a git repository"
        }
        error = ToolError("Git command failed", details=details)
        
        assert error.message == "Git command failed"
        assert error.details == details
        assert error.details["tool_name"] == "git_status"
        assert error.details["exit_code"] == 1


class TestExceptionUtilities:
    """Test exception utility functions and patterns."""
    
    def test_exception_chaining(self):
        """Test exception chaining with raise ... from."""
        original_error = ValueError("Original error")
        
        try:
            try:
                raise original_error
            except ValueError as e:
                raise ConfigError("Config processing failed") from e
        except ConfigError as config_error:
            assert config_error.__cause__ is original_error
            assert isinstance(config_error.__cause__, ValueError)
    
    def test_error_details_serializable(self):
        """Test that error details are JSON serializable."""
        import json
        
        details = {
            "string_field": "value",
            "int_field": 42,
            "float_field": 3.14,
            "bool_field": True,
            "list_field": [1, 2, 3],
            "dict_field": {"nested": "value"}
        }
        error = GridError("Test", details=details)
        
        # Should be able to serialize details to JSON
        json_str = json.dumps(error.details)
        deserialized = json.loads(json_str)
        
        assert deserialized == details
    
    def test_error_with_none_details(self):
        """Test error handling with None details."""
        error = GridError("Test", details=None)
        
        assert error.details == {}  # Should be empty dict, not None
        assert str(error) == "Test"
    
    def test_error_with_empty_details(self):
        """Test error handling with empty details."""
        error = GridError("Test", details={})
        
        assert error.details == {}
        assert str(error) == "Test"