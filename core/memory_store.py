"""
SQLite-based Memory Store for Agent Platform.

Provides:
- Long-term and short-term memory
- Task tracking and plan management
- Skill indexing
- Full-text search (FTS5)
- Thread-safe operations
- Pre-loading for agent instructions
- User and Agent isolation
"""

import shutil
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
    type: str  # long_term | short_term | task | task_plan | skill | insight
    content: str
    tags: str
    created_at: str
    updated_at: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    status: Optional[str] = None  # active | completed | abandoned
    importance: float = 0.5
    is_archived: int = 0
    summary: Optional[str] = None
    entities: str = '[]'
    connections: str = '[]'
    source_ids: str = '[]'

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


class MemoryStore:
    """
    SQLite-based memory storage with FTS5 full-text search.

    Features:
    - 5 memory types: long_term, short_term, task, task_plan, skill
    - Full-text search via FTS5
    - Pre-loading for agent instructions
    - Automatic cleanup of old short-term entries
    - Thread-safe via WAL mode
    - User and Agent isolation via user_id and agent_id

    Usage:
        store = MemoryStore(db_path="data/memory.db")

        # Save memory
        entry_id = store.save(
            "User prefers Python", 
            type="long_term", 
            tags="preference",
            user_id="user_123",
            agent_id="agent_456"
        )

        # Search
        results = store.search(
            "Python", 
            type="long_term", 
            user_id="user_123",
            limit=10
        )
    """

    SCHEMA_VERSION = 3

    VALID_TYPES = {"long_term", "short_term", "task", "task_plan", "skill", "insight"}
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
        # If DB file exists, check integrity first (handles "database disk image is malformed")
        if self.db_path.exists():
            try:
                with sqlite3.connect(str(self.db_path)) as conn:
                    cursor = conn.execute("PRAGMA integrity_check")
                    row = cursor.fetchone()
                    if row is None or (row[0] if isinstance(row, (tuple, list)) else row) != "ok":
                        raise sqlite3.DatabaseError(
                            "PRAGMA integrity_check failed: " + (str(row[0]) if row else "unknown")
                        )
            except sqlite3.DatabaseError as e:
                logger.error(
                    "❌ Memory database is corrupted (%s). Backing up and recreating. Backup: %s",
                    e,
                    self.db_path.with_suffix(self.db_path.suffix + ".corrupted"),
                )
                backup = self.db_path.with_suffix(self.db_path.suffix + ".corrupted")
                try:
                    shutil.copy2(str(self.db_path), str(backup))
                    self.db_path.unlink()
                except Exception as backup_err:
                    logger.exception("Failed to backup/remove corrupted DB: %s", backup_err)
                    raise
                logger.info("🔄 Corrupted DB removed. A new database will be created.")
            except Exception as e:
                logger.warning("⚠️ Error checking integrity: %s", e)

        # Check if we need to reset the database (schema migration)
        # We do this by checking if the 'summary' column exists in the 'memory' table
        # if the table already exists.
        reset_needed = False
        if self.db_path.exists():
            try:
                with sqlite3.connect(str(self.db_path)) as conn:
                    cursor = conn.execute("PRAGMA table_info(memory)")
                    columns = [row[1] for row in cursor.fetchall()]
                    if columns and "summary" not in columns:
                        reset_needed = True
                        logger.info("🔄 Old schema detected (missing summary). Resetting memory database as requested.")
            except Exception as e:
                logger.warning("⚠️ Error checking schema: %s", e)

        if reset_needed:
            try:
                # Close any existing connections and delete the file
                # Since we are in __init__, there shouldn't be other connections yet
                # but let's be safe and just drop the table instead of deleting the file
                # to avoid permission issues if the file is open.
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute("DROP TABLE IF EXISTS memory")
                    conn.execute("DROP TABLE IF EXISTS memory_fts")
                    conn.execute("DROP TABLE IF EXISTS metadata")
                    conn.commit()
                logger.info("🗑️ Old memory tables dropped.")
            except Exception as e:
                logger.error(f"❌ Failed to reset database: {e}")

        with self._get_connection() as conn:
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            # Main memory table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    type        TEXT NOT NULL CHECK(type IN ('long_term','short_term','task','task_plan','skill','insight')),
                    content     TEXT NOT NULL,
                    tags        TEXT DEFAULT '',
                    created_at  TEXT DEFAULT (datetime('now')),
                    updated_at  TEXT DEFAULT (datetime('now')),
                    session_id  TEXT,
                    task_id     TEXT,
                    user_id     TEXT,
                    agent_id    TEXT,
                    status      TEXT CHECK(status IS NULL OR status IN ('active','completed','abandoned')),
                    importance  REAL DEFAULT 0.5 CHECK(importance >= 0 AND importance <= 1),
                    is_archived INTEGER DEFAULT 0 CHECK(is_archived IN (0, 1)),
                    summary     TEXT,
                    entities    TEXT DEFAULT '[]',
                    connections TEXT DEFAULT '[]',
                    source_ids  TEXT DEFAULT '[]'
                )
            """)

            # Indexes for common queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory(type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_session ON memory(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_task ON memory(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_user ON memory(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_agent ON memory(agent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_importance ON memory(importance)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_created ON memory(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_status ON memory(status)")

            # FTS5 virtual table for full-text search
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    content,
                    tags,
                    summary,
                    entities,
                    content='memory',
                    content_rowid='id'
                )
            """)

            # Triggers to sync FTS5 with main table
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
                    INSERT INTO memory_fts(rowid, content, tags, summary, entities)
                    VALUES (new.id, new.content, new.tags, new.summary, new.entities);
                END
            """)

            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
                    DELETE FROM memory_fts WHERE rowid = old.id;
                END
            """)

            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
                    UPDATE memory_fts SET content = new.content, tags = new.tags, summary = new.summary, entities = new.entities
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
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        summary: Optional[str] = None,
        entities: str = '[]',
        connections: str = '[]',
        source_ids: str = '[]'
    ) -> int:
        """
        Save a memory entry.

        Args:
            content: Memory content
            type: long_term | short_term | task | task_plan | skill | insight
            tags: Comma-separated tags
            importance: 0.0 to 1.0 (higher = more important)
            session_id: Optional session identifier
            task_id: Optional task identifier
            user_id: Optional user identifier
            agent_id: Optional agent identifier
            status: active | completed | abandoned (for tasks)
            summary: Optional short summary
            entities: JSON array of entities
            connections: JSON array of connections
            source_ids: JSON array of source memory IDs

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
                INSERT INTO memory (type, content, tags, importance, session_id, task_id, user_id, agent_id, status, summary, entities, connections, source_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (type, content, tags, importance, session_id, task_id, user_id, agent_id, status, summary, entities, connections, source_ids)
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
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
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
            user_id: Filter by user
            agent_id: Filter by agent
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

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)

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
        is_archived: Optional[bool] = None,
        summary: Optional[str] = None,
        entities: Optional[str] = None,
        connections: Optional[str] = None,
        source_ids: Optional[str] = None
    ) -> bool:
        """
        Update an existing entry.

        Args:
            entry_id: Entry ID
            content: New content
            status: New status
            importance: New importance
            is_archived: Archive flag
            summary: New summary
            entities: New entities JSON
            connections: New connections JSON
            source_ids: New source_ids JSON

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

        if summary is not None:
            updates.append("summary = ?")
            params.append(summary)

        if entities is not None:
            updates.append("entities = ?")
            params.append(entities)

        if connections is not None:
            updates.append("connections = ?")
            params.append(connections)

        if source_ids is not None:
            updates.append("source_ids = ?")
            params.append(source_ids)

        if not updates:
            return False

        updates.append("updated_at = datetime('now')")

        try:
            with self._get_connection() as conn:
                sql = f"UPDATE memory SET {', '.join(updates)} WHERE id = ?"
                params.append(entry_id)

                cursor = conn.execute(sql, params)
                conn.commit()

                success = cursor.rowcount > 0
                if success:
                    logger.debug("✏️ Updated memory #%s", entry_id)
                return success
        except sqlite3.DatabaseError as e:
            logger.error(
                "❌ Memory database error on update (entry_id=%s): %s. "
                "If you see 'database disk image is malformed', remove or replace %s (e.g. backup as .corrupted and restart).",
                entry_id,
                e,
                self.db_path,
            )
            raise

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
        max_insights: int = 5,
        max_long_term: int = 10,
        max_short_term: int = 5,
        max_tasks: int = 3,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None
    ) -> Dict[str, List[MemoryEntry]]:
        """
        Get memory entries for pre-loading into agent instructions.

        Args:
            max_insights: Max insight entries
            max_long_term: Max long-term entries
            max_short_term: Max short-term entries
            max_tasks: Max active tasks
            session_id: Optional session filter
            user_id: Optional user filter
            agent_id: Optional agent filter

        Returns:
            Dict with 'insights', 'long_term', 'short_term', 'tasks' lists
        """
        result = {
            "insights": [],
            "long_term": [],
            "short_term": [],
            "tasks": []
        }

        # Insights: highest priority
        result["insights"] = self.search(
            type="insight",
            session_id=None,
            user_id=user_id,
            agent_id=agent_id,
            limit=max_insights
        )

        # Long-term: important facts
        result["long_term"] = self.search(
            type="long_term",
            session_id=None,
            user_id=user_id,
            agent_id=agent_id,
            limit=max_long_term,
            min_importance=0.3  # Only meaningful entries
        )

        # Short-term: recent context
        result["short_term"] = self.search(
            type="short_term",
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            limit=max_short_term
        )

        # Active tasks and plans
        tasks = self.search(
            type="task",
            status="active",
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            limit=max_tasks
        )

        for task in tasks:
            result["tasks"].append(task)
            # Get task plan if exists
            if task.task_id:
                plans = self.search(
                    type="task_plan",
                    task_id=task.task_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    limit=1
                )
                if plans:
                    result["tasks"].append(plans[0])

        return result

    def format_preload(self, preload: Dict[str, List[MemoryEntry]], max_chars: int = 2500) -> str:
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

        # Insights
        if preload.get("insights"):
            lines.append("=== КЛЮЧЕВЫЕ ИНСАЙТЫ ===")
            for entry in preload["insights"]:
                content = entry.summary if entry.summary else entry.content
                line = f"💡 {content}"
                if char_count + len(line) > max_chars:
                    break
                lines.append(line)
                char_count += len(line)

        # Long-term memory
        if preload.get("long_term") and char_count < max_chars:
            lines.append("\n=== ДОЛГОСРОЧНАЯ ПАМЯТЬ ===")
            for entry in preload["long_term"]:
                content = entry.summary if entry.summary else entry.content
                line = f"- {content}"
                if char_count + len(line) > max_chars:
                    break
                lines.append(line)
                char_count += len(line)

        # Short-term memory
        if preload.get("short_term") and char_count < max_chars:
            lines.append("\n=== НЕДАВНИЕ ЗАМЕТКИ ===")
            for entry in preload["short_term"]:
                content = entry.summary if entry.summary else entry.content
                line = f"- [{entry.created_at[:10]}] {content}"
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

    def cleanup_short_term(
        self, 
        max_age_hours: int = 48, 
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None
    ):
        """
        Clean up old short-term memory entries.

        Args:
            max_age_hours: Archive entries older than this
            session_id: Optional session filter
            user_id: Optional user filter
            agent_id: Optional agent filter
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
            
            if user_id:
                sql += " AND user_id = ?"
                params.append(user_id)
            
            if agent_id:
                sql += " AND agent_id = ?"
                params.append(agent_id)

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
