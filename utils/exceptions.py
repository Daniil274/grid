"""
Exception classes for Grid system.
"""

from typing import Optional, Any


class GridError(Exception):
    """Base exception for Grid system."""
    
    def __init__(self, message: str, details: Optional[dict] = None):
        self.message = message
        self.details = details or {}
        super().__init__(message)


class ConfigError(GridError):
    """Configuration related errors."""
    pass


class AgentError(GridError):
    """Agent creation and execution errors."""
    pass


class ToolError(GridError):
    """Tool execution errors."""
    pass


class MCPError(GridError):
    """MCP connection and execution errors."""
    pass


class ContextError(GridError):
    """Context management errors."""
    pass