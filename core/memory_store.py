"""
SQLite-based Memory Store for Agent Platform.

Provides:
- Long-term and short-term memory
- Task tracking and plan management
- Full-text search (FTS5)
- Thread-safe operations
- Pre-loading for agent instructions
"""

import sqlite3
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """Single memory entry."""
    id: int
    type: str  # long_term | short_term | task | task_plan
    content: str
    tags: str
    created_at: str
    updated_at: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    status: Optional[str] = None  # active | completed | abandoned
    importance: float = 0.5
    is_archived: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


class MemoryStore:
    """
    SQLite-based memory storage with FTS5 full-text search.

    Features:
    - 4 memory types: long_term, short_term, task, task_plan
    - Full-text search via FTS5
    - Pre-loading for agent instructions
    - Automatic cleanup of old short-term entries
    - Thread-safe via WAL mode

    Usage:
        store = MemoryStore(db_path="data/memory.db")

        # Save memory
        entry_id = store.save("User prefers Python", type="long_term", tags="preference")

        # Search
        results = store.search("Python", type="long_term", limit=10)

        # Pre-load for agent
        preload = store.get_preload_context()
        text = store.format_preload(preload)
    """

    SCHEMA_VERSION = 1

    VALID_TYPES = {"long_term", "short_term", "task", "task_plan"}
    VALID_STATUSES = {"active", "completed", "abandoned"}

    def __init__(self, db_path: str):
        """
        Initialize memory store.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()
        logger.info(f"✅ MemoryStore initialized: {self.db_path}")

    def _init_db(self):
        """Initialize database schema with FTS5."""
        with self._get_connection() as conn:
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            # Main memory table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    type        TEXT NOT NULL CHECK(type IN ('long_term','short_term','task','task_plan')),
                    content     TEXT NOT NULL,
                    tags        TEXT DEFAULT '',
                    created_at  TEXT DEFAULT (datetime('now')),
                    updated_at  TEXT DEFAULT (datetime('now')),
                    session_id  TEXT,
                    task_id     TEXT,
                    status      TEXT CHECK(status IS NULL OR status IN ('active','completed','abandoned')),
                    importance  REAL DEFAULT 0.5 CHECK(importance >= 0 AND importance <= 1),
                    is_archived INTEGER DEFAULT 0 CHECK(is_archived IN (0, 1))
                )
            """)

            # Indexes for common queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory(type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_session ON memory(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_task ON memory(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_importance ON memory(importance)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_created ON memory(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_status ON memory(status)")

            # FTS5 virtual table for full-text search
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    content,
                    tags,
                    content='memory',
                    content_rowid='id'
                )
            """)

            # Triggers to sync FTS5 with main table
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
                    INSERT INTO memory_fts(rowid, content, tags)
                    VALUES (new.id, new.content, new.tags);
                END
            """)

            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
                    DELETE FROM memory_fts WHERE rowid = old.id;
                END
            """)

            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
                    UPDATE memory_fts SET content = new.content, tags = new.tags
                    WHERE rowid = new.id;
                END
            """)

            # Metadata table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # Store schema version
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),)
            )

            conn.commit()

    @contextmanager
    def _get_connection(self):
        """Get database connection with context manager."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def save(
        self,
        content: str,
        type: str = "long_term",
        tags: str = "",
        importance: float = 0.5,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        status: Optional[str] = None
    ) -> int:
        """
        Save a memory entry.

        Args:
            content: Memory content
            type: long_term | short_term | task | task_plan
            tags: Comma-separated tags
            importance: 0.0 to 1.0 (higher = more important)
            session_id: Optional session identifier
            task_id: Optional task identifier
            status: active | completed | abandoned (for tasks)

        Returns:
            ID of created entry
        """
        if type not in self.VALID_TYPES:
            raise ValueError(f"Invalid type: {type}. Must be one of {self.VALID_TYPES}")

        if status and status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {self.VALID_STATUSES}")

        if not 0 <= importance <= 1:
            raise ValueError(f"Invalid importance: {importance}. Must be 0.0 to 1.0")

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memory (type, content, tags, importance, session_id, task_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (type, content, tags, importance, session_id, task_id, status)
            )
            conn.commit()
            entry_id = cursor.lastrowid
            logger.debug(f"💾 Saved memory #{entry_id} [{type}]: {content[:50]}...")
            return entry_id

    def search(
        self,
        query: str = "",
        type: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        status: Optional[str] = None,
        min_importance: float = 0.0,
        limit: int = 50,
        include_archived: bool = False
    ) -> List[MemoryEntry]:
        """
        Search memory entries.

        Args:
            query: FTS5 search query. Empty = recent entries.
            type: Filter by type
            session_id: Filter by session
            task_id: Filter by task
            status: Filter by status
            min_importance: Minimum importance
            limit: Max results
            include_archived: Include archived entries

        Returns:
            List of matching entries, sorted by relevance/recency
        """
        conditions = []
        params = []

        if type:
            if type not in self.VALID_TYPES:
                raise ValueError(f"Invalid type: {type}")
            conditions.append("type = ?")
            params.append(type)

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)

        if status:
            if status not in self.VALID_STATUSES:
                raise ValueError(f"Invalid status: {status}")
            conditions.append("status = ?")
            params.append(status)

        if min_importance > 0:
            conditions.append("importance >= ?")
            params.append(min_importance)

        if not include_archived:
            conditions.append("is_archived = 0")

        with self._get_connection() as conn:
            if query.strip():
                # FTS5 search with proper escaping
                # Wrap each token in quotes to prevent column interpretation
                tokens = query.split()
                fts_tokens = []
                for token in tokens:
                    # Escape internal quotes
                    escaped = token.replace('"', '""')
                    # Wrap in quotes to treat as literal string
                    fts_tokens.append(f'"{escaped}"')
                fts_query = ' '.join(fts_tokens)

                sql = """
                    SELECT m.* FROM memory m
                    JOIN memory_fts fts ON m.id = fts.rowid
                    WHERE memory_fts MATCH ?
                """
                params_fts = [fts_query]

                if conditions:
                    sql += " AND " + " AND ".join(conditions)
                    params_fts.extend(params)

                sql += " ORDER BY rank, importance DESC, created_at DESC LIMIT ?"
                params_fts.append(limit)

                cursor = conn.execute(sql, params_fts)
            else:
                # Recent entries
                sql = "SELECT * FROM memory"
                if conditions:
                    sql += " WHERE " + " AND ".join(conditions)
                sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
                params.append(limit)

                cursor = conn.execute(sql, params)

            rows = cursor.fetchall()
            return [MemoryEntry(**dict(row)) for row in rows]

    def update(
        self,
        entry_id: int,
        content: Optional[str] = None,
        status: Optional[str] = None,
        importance: Optional[float] = None,
        is_archived: Optional[bool] = None
    ) -> bool:
        """
        Update an existing entry.

        Args:
            entry_id: Entry ID
            content: New content
            status: New status
            importance: New importance
            is_archived: Archive flag

        Returns:
            True if updated, False if not found
        """
        updates = []
        params = []

        if content is not None:
            updates.append("content = ?")
            params.append(content)

        if status is not None:
            if status not in self.VALID_STATUSES:
                raise ValueError(f"Invalid status: {status}")
            updates.append("status = ?")
            params.append(status)

        if importance is not None:
            if not 0 <= importance <= 1:
                raise ValueError(f"Invalid importance: {importance}")
            updates.append("importance = ?")
            params.append(importance)

        if is_archived is not None:
            updates.append("is_archived = ?")
            params.append(1 if is_archived else 0)

        if not updates:
            return False

        updates.append("updated_at = datetime('now')")

        with self._get_connection() as conn:
            sql = f"UPDATE memory SET {', '.join(updates)} WHERE id = ?"
            params.append(entry_id)

            cursor = conn.execute(sql, params)
            conn.commit()

            success = cursor.rowcount > 0
            if success:
                logger.debug(f"✏️ Updated memory #{entry_id}")
            return success

    def delete(self, entry_id: int, hard: bool = False) -> bool:
        """
        Delete an entry.

        Args:
            entry_id: Entry ID
            hard: If True, permanently delete. If False, archive.

        Returns:
            True if deleted/archived, False if not found
        """
        with self._get_connection() as conn:
            if hard:
                cursor = conn.execute("DELETE FROM memory WHERE id = ?", (entry_id,))
                action = "Deleted"
            else:
                cursor = conn.execute(
                    "UPDATE memory SET is_archived = 1, updated_at = datetime('now') WHERE id = ?",
                    (entry_id,)
                )
                action = "Archived"

            conn.commit()
            success = cursor.rowcount > 0
            if success:
                logger.debug(f"🗑️ {action} memory #{entry_id}")
            return success

    def get_preload_context(
        self,
        max_long_term: int = 15,
        max_short_term: int = 5,
        max_tasks: int = 3,
        session_id: Optional[str] = None
    ) -> Dict[str, List[MemoryEntry]]:
        """
        Get memory entries for pre-loading into agent instructions.

        Args:
            max_long_term: Max long-term entries
            max_short_term: Max short-term entries
            max_tasks: Max active tasks
            session_id: Optional session filter

        Returns:
            Dict with 'long_term', 'short_term', 'tasks' lists
        """
        result = {
            "long_term": [],
            "short_term": [],
            "tasks": []
        }

        # Long-term: most important
        result["long_term"] = self.search(
            type="long_term",
            session_id=session_id,
            limit=max_long_term,
            min_importance=0.3  # Only meaningful entries
        )

        # Short-term: recent
        result["short_term"] = self.search(
            type="short_term",
            session_id=session_id,
            limit=max_short_term
        )

        # Active tasks and plans
        tasks = self.search(
            type="task",
            status="active",
            session_id=session_id,
            limit=max_tasks
        )

        for task in tasks:
            result["tasks"].append(task)
            # Get task plan if exists
            if task.task_id:
                plans = self.search(
                    type="task_plan",
                    task_id=task.task_id,
                    limit=1
                )
                if plans:
                    result["tasks"].append(plans[0])

        return result

    def format_preload(self, preload: Dict[str, List[MemoryEntry]], max_chars: int = 2000) -> str:
        """
        Format pre-loaded memory for agent instructions.

        Args:
            preload: Result from get_preload_context
            max_chars: Maximum characters to return

        Returns:
            Formatted markdown string
        """
        lines = []
        char_count = 0

        # Long-term memory
        if preload.get("long_term"):
            lines.append("=== ДОЛГОСРОЧНАЯ ПАМЯТЬ ===")
            for entry in preload["long_term"]:
                line = f"- {entry.content}"
                if char_count + len(line) > max_chars:
                    break
                lines.append(line)
                char_count += len(line)

        # Short-term memory
        if preload.get("short_term") and char_count < max_chars:
            lines.append("\n=== НЕДАВНИЕ ЗАМЕТКИ ===")
            for entry in preload["short_term"]:
                line = f"- [{entry.created_at[:10]}] {entry.content}"
                if char_count + len(line) > max_chars:
                    break
                lines.append(line)
                char_count += len(line)

        # Active tasks
        if preload.get("tasks") and char_count < max_chars:
            lines.append("\n=== ТЕКУЩИЕ ЗАДАЧИ ===")
            for entry in preload["tasks"]:
                if entry.type == "task":
                    line = f"📋 {entry.content}"
                else:  # task_plan
                    line = f"  └─ План: {entry.content}"
                if char_count + len(line) > max_chars:
                    break
                lines.append(line)
                char_count += len(line)

        if not lines:
            return ""

        return "\n".join(lines)

    def cleanup_short_term(self, max_age_hours: int = 48, session_id: Optional[str] = None):
        """
        Clean up old short-term memory entries.

        Args:
            max_age_hours: Archive entries older than this
            session_id: Optional session filter
        """
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()

        with self._get_connection() as conn:
            sql = """
                UPDATE memory SET is_archived = 1, updated_at = datetime('now')
                WHERE type = 'short_term' AND created_at < ? AND is_archived = 0
            """
            params = [cutoff]

            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)

            cursor = conn.execute(sql, params)
            conn.commit()

            if cursor.rowcount > 0:
                logger.info(f"🧹 Archived {cursor.rowcount} old short-term entries")

    def get_stats(self) -> Dict[str, Any]:
        """Get memory statistics."""
        with self._get_connection() as conn:
            # Count by type
            cursor = conn.execute("""
                SELECT type, COUNT(*) as count
                FROM memory
                WHERE is_archived = 0
                GROUP BY type
            """)
            by_type = {row["type"]: row["count"] for row in cursor.fetchall()}

            # Count by status (tasks only)
            cursor = conn.execute("""
                SELECT status, COUNT(*) as count
                FROM memory
                WHERE type IN ('task', 'task_plan') AND is_archived = 0 AND status IS NOT NULL
                GROUP BY status
            """)
            by_status = {row["status"]: row["count"] for row in cursor.fetchall()}

            # Total entries
            cursor = conn.execute("SELECT COUNT(*) as total FROM memory WHERE is_archived = 0")
            total = cursor.fetchone()["total"]

            # Archived count
            cursor = conn.execute("SELECT COUNT(*) as archived FROM memory WHERE is_archived = 1")
            archived = cursor.fetchone()["archived"]

            return {
                "total_entries": total,
                "archived_entries": archived,
                "by_type": by_type,
                "by_status": by_status,
                "db_path": str(self.db_path),
                "db_size_mb": self.db_path.stat().st_size / (1024 * 1024) if self.db_path.exists() else 0
            }
