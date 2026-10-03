"""
Protocols (interfaces) for agent system components.

This module defines interfaces for different managers and components
to enable proper separation of concerns and dependency injection.
"""

from typing import Protocol, Any, Optional, List, Dict, Tuple, runtime_checkable, Callable
from openai import AsyncOpenAI
from agents import SQLiteSession


@runtime_checkable
class IModelManager(Protocol):
    """Protocol for managing models and API clients."""

    def resolve_model_key(self, key: Optional[str]) -> str:
        """
        Resolve input key into a model key using configuration.

        Args:
            key: Model key, agent key, or None (uses default)

        Returns:
            Resolved model key
        """
        ...

    def get_openai_client_for_model(self, model_key: str) -> Tuple[AsyncOpenAI, str]:
        """
        Create OpenAI client and return (client, model_name) using configuration.

        Args:
            model_key: Model configuration key

        Returns:
            Tuple of (AsyncOpenAI client, model name)

        Raises:
            AgentError: If API key not found or client creation fails
        """
        ...

    def is_reasoning_model_name(self, model_name: str) -> bool:
        """
        Heuristic check for reasoning-style models requiring Responses API.

        Args:
            model_name: Name of the model

        Returns:
            True if model appears to be a reasoning model
        """
        ...


@runtime_checkable
class ISessionManager(Protocol):
    """Protocol for managing agent sessions."""

    def get_agent_session(self, agent_key: str, context_id: str) -> SQLiteSession:
        """
        Get or create a session scoped to an agent/context pair.

        Args:
            agent_key: Agent identifier
            context_id: Context identifier

        Returns:
            SQLiteSession instance
        """
        ...

    async def cleanup_sessions(self) -> None:
        """
        Cleanup all active sessions.

        This method should be called during shutdown to properly
        close all database connections and release resources.
        """
        ...

    def clear_sessions(self) -> None:
        """
        Clear all sessions from memory.

        This does not cleanup resources, just clears the cache.
        Use cleanup_sessions() for proper resource cleanup.
        """
        ...


@runtime_checkable
class IInstructionsBuilder(Protocol):
    """Protocol for building agent instructions."""

    def build_agent_instructions(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        include_conversation_context: bool = True
    ) -> str:
        """
        Build complete agent instructions with context.

        Args:
            agent_key: Agent configuration key
            context_path: Optional context path for agent
            include_conversation_context: Whether to include conversation history

        Returns:
            Complete instructions string
        """
        ...

    def build_path_context(self, context_path: Optional[str] = None) -> str:
        """
        Build path context information.

        Args:
            context_path: Optional context path

        Returns:
            Path context string
        """
        ...


@runtime_checkable
class IContextManager(Protocol):
    """Protocol for managing conversation context."""

    def get_current_context_id(self) -> Optional[str]:
        """Get current active context identifier."""
        ...

    def start_new_context(self) -> str:
        """Start a new context session."""
        ...

    def activate_context(self, context_id: str) -> str:
        """Activate a specific context session."""
        ...

    def add_message(
        self,
        role: str,
        content: Any,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add message to conversation context."""
        ...

    def get_conversation_context(self, last_n: Optional[int] = None) -> str:
        """Get conversation context as formatted string."""
        ...

    def get_conversation_history_as_sdk_messages(
        self,
        last_n: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Get conversation history in SDK message format."""
        ...

    def add_execution(self, execution: Any) -> None:
        """Add agent execution to history."""
        ...

    def set_metadata(self, key: str, value: Any) -> None:
        """Set metadata for current context."""
        ...


@runtime_checkable
class IConfig(Protocol):
    """Protocol for configuration management."""

    def get_agent(self, agent_key: str) -> Any:
        """Get agent configuration."""
        ...

    def get_model(self, model_key: str) -> Any:
        """Get model configuration."""
        ...

    def get_provider(self, provider_key: str) -> Any:
        """Get provider configuration."""
        ...

    def get_tool(self, tool_key: str) -> Any:
        """Get tool configuration."""
        ...

    def get_api_key(self, provider_key: str) -> Optional[str]:
        """Get API key for provider."""
        ...

    def get_working_directory(self) -> str:
        """Get working directory path."""
        ...

    def get_config_directory(self) -> str:
        """Get config directory path."""
        ...

    def get_max_history(self) -> int:
        """Get maximum history size."""
        ...

    def get_max_turns(self, agent_key: Optional[str] = None) -> int:
        """Maximum turns per run; 0 means no limit.

        A per-agent ``max_turns`` wins over ``settings.max_turns``.
        """
        ...

    def get_agent_timeout(self, agent_key: Optional[str] = None) -> int:
        """Agent execution timeout in seconds; 0 means no timeout.

        A per-agent ``timeout`` wins over ``settings.agent_timeout``.
        """
        ...

    def is_mcp_enabled(self) -> bool:
        """Check if MCP is enabled globally."""
        ...

    def build_agent_prompt(self, agent_key: str) -> str:
        """Build base prompt for agent."""
        ...


# ---------------------------------------------------------------------------
# Cross-cutting ports for unit-testability (I/O, time, logging, tool registry)
# ---------------------------------------------------------------------------

@runtime_checkable
class LoggerPort(Protocol):
    """Minimal logger interface used by core without binding to logging impl."""

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def info(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def error(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None: ...


@runtime_checkable
class ClockPort(Protocol):
    """Time source abstraction for deterministic tests."""

    def time(self) -> float: ...


# Tool registry: resolve tool callables/objects by name
ToolResolver = Callable[[List[str]], List[Any]]


@runtime_checkable
class ImageConfiguratorPort(Protocol):
    """Optional hook to configure image processing (for tests / different envs)."""

    def set_config(self, config: Any) -> None: ...


# Type aliases for common types
AgentKey = str
ContextId = str
ModelKey = str
ToolName = str
