"""
Session Manager for agent sessions.

Manages SQLite sessions for agents, providing session isolation
per agent/context pair.
"""

import asyncio
import logging
from typing import Dict, Tuple
from agents import SQLiteSession

logger = logging.getLogger("grid.session_manager")


class SessionManager:
    """
    Manages agent sessions with proper lifecycle management.

    Each session is scoped to an (agent_key, context_id) pair,
    ensuring that agent memory is properly isolated between
    different conversations and contexts.
    """

    def __init__(self) -> None:
        """Initialize SessionManager with empty session cache."""
        # Session management for agent memory (per agent/context pair)
        self._agent_sessions: Dict[Tuple[str, str], SQLiteSession] = {}

    def get_agent_session(self, agent_key: str, context_id: str) -> SQLiteSession:
        """
        Get or create a session scoped to an agent/context pair.

        Args:
            agent_key: Agent identifier
            context_id: Context identifier

        Returns:
            SQLiteSession instance for the given agent/context pair

        Example:
            >>> manager = SessionManager()
            >>> session = manager.get_agent_session("my_agent", "ctx-12345")
            >>> # Session is cached for subsequent calls
            >>> same_session = manager.get_agent_session("my_agent", "ctx-12345")
            >>> assert session is same_session
        """
        session_key = (agent_key, context_id)
        if session_key not in self._agent_sessions:
            session_id = f"agent_{agent_key}_{context_id}"
            self._agent_sessions[session_key] = SQLiteSession(session_id)
            logger.debug(
                "Created new session",
                extra={
                    "agent_key": agent_key,
                    "context_id": context_id,
                    "session_id": session_id,
                },
            )

        return self._agent_sessions[session_key]

    async def cleanup_sessions(self) -> None:
        """
        Cleanup all active sessions and release resources.

        This method properly closes all database connections
        and should be called during application shutdown.

        Note:
            This method is async to support potential async cleanup
            operations in the future.
        """
        logger.info(
            "Cleaning up sessions",
            extra={"session_count": len(self._agent_sessions)},
        )

        for session_key, session in list(self._agent_sessions.items()):
            try:
                await session.clear_session()
                logger.debug(
                    "Session cleaned up",
                    extra={
                        "agent_key": session_key[0],
                        "context_id": session_key[1],
                    },
                )
            except asyncio.CancelledError:
                logger.debug(
                    "Session cleanup cancelled",
                    extra={"session_key": session_key},
                    exc_info=True,
                )
            except Exception as e:
                logger.warning(
                    "Failed to cleanup agent session",
                    extra={
                        "agent_key": session_key[0],
                        "context_id": session_key[1],
                        "error": str(e),
                    },
                    exc_info=e,
                )

        self._agent_sessions.clear()
        logger.info("All sessions cleaned up")

    def clear_sessions(self) -> None:
        """
        Clear all sessions from cache without cleanup.

        This is a lightweight operation that just removes sessions
        from the cache. For proper resource cleanup, use cleanup_sessions().

        Warning:
            This does not close database connections or release resources.
            Use this only when you're sure sessions are no longer needed
            and cleanup_sessions() cannot be called.
        """
        session_count = len(self._agent_sessions)
        self._agent_sessions.clear()
        logger.debug(
            "Sessions cleared from cache",
            extra={"session_count": session_count},
        )

    def get_session_count(self) -> int:
        """
        Get the number of active sessions.

        Returns:
            Number of sessions in cache
        """
        return len(self._agent_sessions)

    def has_session(self, agent_key: str, context_id: str) -> bool:
        """
        Check if a session exists for the given agent/context pair.

        Args:
            agent_key: Agent identifier
            context_id: Context identifier

        Returns:
            True if session exists in cache
        """
        return (agent_key, context_id) in self._agent_sessions
