"""
Session Manager for agent sessions.

Manages SQLite sessions for agents, providing session isolation
per system/agent/context pair when a system namespace is supplied.
"""

import asyncio
import hashlib
import logging
import sqlite3
import threading
from typing import Callable, Dict, Optional, Tuple
from agents import SQLiteSession

logger = logging.getLogger("grid.session_manager")


def agent_session_id(agent_key: str, context_id: str, namespace: Optional[str] = None) -> str:
    """The SDK session id of one agent, system and conversation.

    An empty namespace keeps existing single-system session IDs readable.
    """
    if namespace:
        system_id = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:16]
        return f"system_{system_id}_agent_{agent_key}_{context_id}"
    return f"agent_{agent_key}_{context_id}"


class FileSQLiteSession(SQLiteSession):
    """A file-backed SDK session whose close() releases the file.

    The SDK opens one connection per thread - every ``asyncio.to_thread``
    worker that touches the session gets its own - and its close() reaches
    only the caller's. The others keep the database open for as long as the
    worker threads live; on Windows the file then cannot be deleted. This
    session remembers each connection it opened and closes them all.
    """

    def __init__(self, session_id: str, db_path: str) -> None:
        self._opened: list[sqlite3.Connection] = []
        self._opened_lock = threading.Lock()
        super().__init__(session_id=session_id, db_path=db_path)

    def _get_connection(self) -> sqlite3.Connection:
        fresh = not hasattr(self._local, "connection")
        connection = super()._get_connection()
        if fresh:
            with self._opened_lock:
                self._opened.append(connection)
        return connection

    def close(self) -> None:
        with self._opened_lock:
            opened, self._opened = self._opened, []
            # A later use opens new connections instead of the closed ones.
            self._local = threading.local()
        for connection in opened:
            connection.close()


class SessionManager:
    """
    Manages agent sessions with proper lifecycle management.

    Each session is scoped to a namespace and (agent_key, context_id) pair,
    ensuring that agent memory is properly isolated between
    different conversations and contexts.
    """

    def __init__(self, session_factory: Optional[Callable[[str], SQLiteSession]] = None,
                 *, namespace: Optional[str] = None) -> None:
        """Initialize SessionManager with empty session cache."""
        # Session management for agent memory (per agent/context pair)
        self._agent_sessions: Dict[Tuple[str, str], SQLiteSession] = {}
        self._session_factory = session_factory or SQLiteSession
        self.namespace = namespace

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
            session_id = agent_session_id(agent_key, context_id, self.namespace)
            self._agent_sessions[session_key] = self._session_factory(session_id)
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
        Close active sessions and release resources without erasing history.

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
                session.close()
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
