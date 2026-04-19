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
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from contextlib import contextmanager
from difflib import SequenceMatcher
import json
from collections import deque, defaultdict

# Optional semantic search support via OpenRouter embeddings
try:
    from core.memory.embeddings import EmbeddingsManager, create_embeddings_manager
    _EMBEDDINGS_MODULE_AVAILABLE = True
except ImportError:
    _EMBEDDINGS_MODULE_AVAILABLE = False

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
    ttl_days: Optional[int] = None
    last_accessed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class SkillEntry:
    """Single skill entry stored in the dedicated skills table."""
    id: int
    name: str
    content: str
    tags: str
    user_id: str
    agent_id: str
    summary: str
    created_at: str
    updated_at: str


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

    SCHEMA_VERSION = 5

    VALID_TYPES = {"long_term", "short_term", "task", "task_plan", "skill", "insight"}
    VALID_STATUSES = {"active", "completed", "abandoned"}

    # Дедупликация
    DEFAULT_SIMILARITY_THRESHOLD = 0.8  # 80% = дубликат
    MAX_FTS_CANDIDATES = 50  # Макс. кандидатов от FTS5
    IMPORTANCE_BOOST_ON_UPDATE = 0.1  # Увеличение importance при обновлении

    def __init__(
        self,
        db_path: str,
        config: Optional[Any] = None,
        enable_embeddings: bool = True,
        embeddings_persist_directory: Optional[str] = None,
    ):
        """
        Initialize memory store.

        Args:
            db_path: Path to SQLite database file
            config: Optional Config object for TTL defaults
            enable_embeddings: Enable semantic search via OpenRouter embeddings
            embeddings_persist_directory: Persist ChromaDB vectors here (None = in-memory)
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config

        # Initialize database
        self._init_db()

        # Optional semantic search (requires OPENROUTER_API_KEY + chromadb)
        self._embeddings: Optional[EmbeddingsManager] = None
        if enable_embeddings and _EMBEDDINGS_MODULE_AVAILABLE:
            persist_dir = embeddings_persist_directory
            if persist_dir is None:
                # Default: store vectors next to the SQLite DB
                persist_dir = str(self.db_path.parent / "embeddings")
            try:
                self._embeddings = create_embeddings_manager(
                    config=self.config,
                    persist_directory=persist_dir,
                    collection_name="memory_semantic",
                )
                if self._embeddings:
                    logger.info(
                        "MemoryStore: semantic search enabled (persist=%s)", persist_dir
                    )
                else:
                    logger.info("MemoryStore: semantic search unavailable (check OPENROUTER_API_KEY)")
            except Exception as exc:
                logger.warning("MemoryStore: embeddings init failed: %s", exc)

        logger.info("MemoryStore initialized: %s", self.db_path)

    def _cleanup_wal_files(self):
        """Remove stale WAL/SHM files that could corrupt a new database."""
        for suffix in ["-wal", "-shm"]:
            wal = Path(str(self.db_path) + suffix)
            if wal.exists():
                try:
                    wal.unlink()
                    logger.info("Removed stale %s file", wal.name)
                except Exception as e:
                    logger.warning("Could not remove %s: %s", wal.name, e)

    def close(self) -> None:
        """Flush SQLite WAL state and remove sidecar files when possible."""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                conn.execute("PRAGMA wal_checkpoint(FULL)")
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.commit()
        except Exception as exc:
            logger.debug("MemoryStore close checkpoint skipped: %s", exc)
        self._cleanup_wal_files()

    def _init_db(self):
        """Initialize database schema with FTS5."""
        # If DB does not exist, clean up any stale WAL/SHM files from a previous crash
        # to prevent them from corrupting the new database.
        if not self.db_path.exists():
            self._cleanup_wal_files()

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
                    self._cleanup_wal_files()
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
                    source_ids  TEXT DEFAULT '[]',
                    ttl_days     INTEGER,
                    last_accessed_at TEXT
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

            # Triggers to sync FTS5 with main table.
            # FTS5 external content tables require special insert/delete syntax — plain
            # UPDATE/DELETE on the FTS table corrupts the inverted index (SQLITE_CORRUPT).
            # Always drop and recreate so existing DBs with wrong triggers get fixed.
            conn.execute("DROP TRIGGER IF EXISTS memory_ai")
            conn.execute("DROP TRIGGER IF EXISTS memory_ad")
            conn.execute("DROP TRIGGER IF EXISTS memory_au")

            conn.execute("""
                CREATE TRIGGER memory_ai AFTER INSERT ON memory BEGIN
                    INSERT INTO memory_fts(rowid, content, tags, summary, entities)
                    VALUES (new.id, new.content, new.tags, new.summary, new.entities);
                END
            """)

            conn.execute("""
                CREATE TRIGGER memory_ad AFTER DELETE ON memory BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, content, tags, summary, entities)
                    VALUES ('delete', old.id, old.content, old.tags, old.summary, old.entities);
                END
            """)

            conn.execute("""
                CREATE TRIGGER memory_au AFTER UPDATE ON memory BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, content, tags, summary, entities)
                    VALUES ('delete', old.id, old.content, old.tags, old.summary, old.entities);
                    INSERT INTO memory_fts(rowid, content, tags, summary, entities)
                    VALUES (new.id, new.content, new.tags, new.summary, new.entities);
                END
            """)

            # Metadata table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # ── Skills table (SQL-only, no filesystem) ────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    tags        TEXT DEFAULT '',
                    user_id     TEXT DEFAULT 'default_user',
                    agent_id    TEXT DEFAULT 'default_agent',
                    summary     TEXT DEFAULT '',
                    created_at  DATETIME DEFAULT (datetime('now')),
                    updated_at  DATETIME DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS skills_name_user_agent
                    ON skills(name, user_id, agent_id)
            """)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
                    name, content, tags, summary,
                    content='skills', content_rowid='id'
                )
            """)
            conn.execute("DROP TRIGGER IF EXISTS skills_ai")
            conn.execute("DROP TRIGGER IF EXISTS skills_ad")
            conn.execute("DROP TRIGGER IF EXISTS skills_au")
            conn.execute("""
                CREATE TRIGGER skills_ai AFTER INSERT ON skills BEGIN
                    INSERT INTO skills_fts(rowid, name, content, tags, summary)
                    VALUES (new.id, new.name, new.content, new.tags, new.summary);
                END
            """)
            conn.execute("""
                CREATE TRIGGER skills_ad AFTER DELETE ON skills BEGIN
                    INSERT INTO skills_fts(skills_fts, rowid, name, content, tags, summary)
                    VALUES ('delete', old.id, old.name, old.content, old.tags, old.summary);
                END
            """)
            conn.execute("""
                CREATE TRIGGER skills_au AFTER UPDATE ON skills BEGIN
                    INSERT INTO skills_fts(skills_fts, rowid, name, content, tags, summary)
                    VALUES ('delete', old.id, old.name, old.content, old.tags, old.summary);
                    INSERT INTO skills_fts(rowid, name, content, tags, summary)
                    VALUES (new.id, new.name, new.content, new.tags, new.summary);
                END
            """)

            # Rebuild FTS5 index when upgrading from schema < 5 (fixes corrupted index
            # caused by the old wrong UPDATE trigger on the external content FTS5 table).
            try:
                cur = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'")
                row = cur.fetchone()
                stored_version = int(row[0]) if row else 0
            except Exception:
                stored_version = 0

            if stored_version < 5:
                logger.info("🔄 Schema v%s→5: rebuilding FTS5 index to fix trigger corruption...", stored_version)
                conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")

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

    def _touch_entries(self, entry_ids: List[int]) -> None:
        """
        Update last_accessed_at for entries if extend_ttl_on_access is enabled.
        
        Args:
            entry_ids: List of entry IDs to update
        """
        if not entry_ids:
            return
            
        # Check if extend_ttl_on_access is enabled
        extend = self.config.get('memory_optimizer.extend_ttl_on_access', True) if self.config else True
        if not extend:
            return
            
        with self._get_connection() as conn:
            placeholders = ','.join('?' * len(entry_ids))
            conn.execute(
                f"UPDATE memory SET last_accessed_at = datetime('now') WHERE id IN ({placeholders})",
                entry_ids
            )
            conn.commit()

    # ==================== ДЕДУПЛИКАЦИЯ ====================

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """
        Вычислить коэффициент схожести двух текстов.

        Использует SequenceMatcher.ratio() — нормализованное расстояние Левенштейна.

        Args:
            text1: Первый текст
            text2: Второй текст

        Returns:
            float от 0.0 (полностью разные) до 1.0 (идентичные)
        """
        # Нормализация: lowercase, удаление лишних пробелов
        t1 = ' '.join(text1.lower().split())
        t2 = ' '.join(text2.lower().split())

        return SequenceMatcher(None, t1, t2).ratio()

    def _fts_search_candidates(
        self,
        query: str,
        max_candidates: int = 50,
        type: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        exclude_ids: Optional[List[int]] = None
    ) -> List[MemoryEntry]:
        """
        Быстрый FTS5 поиск кандидатов по ключевым словам.

        Args:
            query: Текст для поиска
            max_candidates: Максимальное количество кандидатов
            type: Фильтр по типу памяти
            user_id: Фильтр по пользователю
            agent_id: Фильтр по агенту
            exclude_ids: ID записей для исключения

        Returns:
            Список кандидатов MemoryEntry
        """
        # Извлекаем значимые слова (длина > 3)
        tokens = [t for t in query.lower().split() if len(t) > 3]
        if not tokens:
            return []

        # Просто передаём слова через пробел (search превратит их в AND/implicit OR)
        fts_query = ' '.join(tokens[:10])

        return self.search(
            query=fts_query,
            type=type,
            user_id=user_id,
            agent_id=agent_id,
            limit=max_candidates
        )

    def find_similar(
        self,
        query: str,
        threshold: float = 0.8,
        limit: int = 5,
        type: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        exclude_ids: Optional[List[int]] = None
    ) -> List[Tuple[MemoryEntry, float]]:
        """
        Найти похожие записи в памяти.

        Алгоритм:
        1. FTS5 поиск кандидатов по ключевым словам
        2. Вычисление similarity через SequenceMatcher для каждого кандидата
        3. Фильтрация по threshold и сортировка по убыванию similarity

        Args:
            query: Текст для поиска похожих записей
            threshold: Минимальный порог схожести (0.0-1.0), default 0.8
            limit: Максимальное количество результатов
            type: Фильтр по типу памяти
            user_id: Фильтр по пользователю
            agent_id: Фильтр по агенту
            exclude_ids: ID записей для исключения из поиска

        Returns:
            List[Tuple[MemoryEntry, float]] — список (запись, similarity_score)
            отсортированный по убыванию similarity
        """
        # Шаг 1: FTS5 для отбора кандидатов (быстро)
        candidates = self._fts_search_candidates(
            query=query,
            max_candidates=self.MAX_FTS_CANDIDATES,
            type=type,
            user_id=user_id,
            agent_id=agent_id,
            exclude_ids=exclude_ids
        )

        # Фильтрация по exclude_ids
        if exclude_ids:
            candidates = [c for c in candidates if c.id not in exclude_ids]

        # Шаг 2: Re-ranking — embeddings (если доступны) или SequenceMatcher
        results: List[Tuple[MemoryEntry, float]] = []

        if self._embeddings and candidates:
            # Семантический re-ranking: одним батч-запросом вычисляем сходство
            try:
                query_vec = self._embeddings.embed_text(query)
                if query_vec:
                    candidate_texts = [e.content for e in candidates]
                    candidate_vecs = self._embeddings.embed_texts(candidate_texts)

                    def _cosine(v1: List[float], v2: List[float]) -> float:
                        if not v1 or not v2:
                            return 0.0
                        dot = sum(a * b for a, b in zip(v1, v2))
                        n1 = sum(a * a for a in v1) ** 0.5
                        n2 = sum(b * b for b in v2) ** 0.5
                        return dot / (n1 * n2) if n1 and n2 else 0.0

                    for entry, vec in zip(candidates, candidate_vecs):
                        sim = _cosine(query_vec, vec)
                        if sim >= threshold:
                            results.append((entry, sim))

                    logger.debug(
                        "find_similar: embedding re-rank %d candidates → %d above threshold",
                        len(candidates), len(results),
                    )
                else:
                    raise ValueError("empty query embedding")
            except Exception as exc:
                logger.warning(
                    "find_similar: embedding re-rank failed (%s), falling back to SequenceMatcher",
                    exc,
                )
                results = []
                for entry in candidates:
                    sim = self._calculate_similarity(query, entry.content)
                    if sim >= threshold:
                        results.append((entry, sim))
        else:
            # Fallback: SequenceMatcher (без embeddings)
            for entry in candidates:
                similarity = self._calculate_similarity(query, entry.content)
                if similarity >= threshold:
                    results.append((entry, similarity))

        # Шаг 3: Сортировка и лимит
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

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
        source_ids: str = '[]',
        # TTL параметры:
        ttl_days: Optional[int] = None,
        # Параметры дедупликации:
        update_if_exists: bool = False,
        similarity_threshold: Optional[float] = None
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
            ttl_days: TTL в днях (None = бессрочно для long_term используется default_long_term_ttl_days из config)
            update_if_exists: Если True, проверяет дубликаты и обновляет существующую запись
            similarity_threshold: Порог схожести для дедупликации (default: DEFAULT_SIMILARITY_THRESHOLD)

        Returns:
            ID of created or updated entry
        """
        if type not in self.VALID_TYPES:
            raise ValueError(f"Invalid type: {type}. Must be one of {self.VALID_TYPES}")

        if status and status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {self.VALID_STATUSES}")

        if not 0 <= importance <= 1:
            raise ValueError(f"Invalid importance: {importance}. Must be 0.0 to 1.0")

        # Дедупликация: поиск похожих записей
        if update_if_exists:
            threshold = similarity_threshold if similarity_threshold is not None else self.DEFAULT_SIMILARITY_THRESHOLD
            similar = self.find_similar(
                query=content,
                threshold=threshold,
                limit=1,
                type=type,
                user_id=user_id,
                agent_id=agent_id
            )

            if similar:
                existing_entry, score = similar[0]
                logger.info(f"🔄 Found similar entry #{existing_entry.id} (similarity={score:.2f}), updating...")

                # Стратегия: обновить content, увеличить importance
                new_importance = min(1.0, existing_entry.importance + self.IMPORTANCE_BOOST_ON_UPDATE)
                self.update(
                    entry_id=existing_entry.id,
                    content=content,
                    importance=new_importance
                )
                return existing_entry.id

        # TTL логика: default TTL для long_term если не указан
        effective_ttl = ttl_days
        if effective_ttl is None and type == "long_term" and self.config:
            effective_ttl = self.config.get('memory_optimizer.default_long_term_ttl_days', 90)

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memory (type, content, tags, importance, session_id, task_id, user_id, agent_id, status, summary, entities, connections, source_ids, ttl_days, last_accessed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (type, content, tags, importance, session_id, task_id, user_id, agent_id, status, summary, entities, connections, source_ids, effective_ttl)
            )
            conn.commit()
            entry_id = cursor.lastrowid
            logger.debug(f"💾 Saved memory #{entry_id} [{type}]: {content[:50]}...")

        # Index in ChromaDB for semantic search (non-blocking: errors are logged only)
        if self._embeddings:
            try:
                self._embeddings.upsert_text(
                    text=content,
                    doc_id=f"mem_{entry_id}",
                    metadata={
                        "entry_id": entry_id,
                        "type": type or "",
                        "tags": tags or "",
                        "user_id": user_id or "",
                        "agent_id": agent_id or "",
                        "importance": importance,
                    },
                )
            except Exception as exc:
                logger.warning("save: embedding index failed for #%s: %s", entry_id, exc)

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
            entries = [MemoryEntry(**dict(row)) for row in rows]
            
            # Update last_accessed_at if extend_ttl_on_access enabled
            if entries:
                self._touch_entries([e.id for e in entries])
            
            return entries

    def search_semantic(
        self,
        query: str,
        n_results: int = 10,
        type: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        min_similarity: float = 0.0,
    ) -> List[Tuple["MemoryEntry", float]]:
        """
        Pure semantic search via OpenRouter embeddings + ChromaDB.

        Falls back to an empty list (with a warning) when embeddings are unavailable.

        Args:
            query: Natural-language search query
            n_results: Maximum number of results
            type: Filter by memory type
            user_id: Filter by user
            agent_id: Filter by agent
            min_similarity: Minimum cosine similarity (0.0–1.0)

        Returns:
            List of (MemoryEntry, similarity_score) sorted by descending similarity
        """
        if not self._embeddings:
            logger.warning(
                "search_semantic: embeddings unavailable; "
                "set OPENROUTER_API_KEY and install chromadb to enable"
            )
            return []

        # Build ChromaDB metadata filter
        # ChromaDB requires $and when combining multiple conditions
        conditions: List[Dict[str, Any]] = []
        if type:
            conditions.append({"type": {"$eq": type}})
        if user_id:
            conditions.append({"user_id": {"$eq": user_id}})
        if agent_id:
            conditions.append({"agent_id": {"$eq": agent_id}})

        if len(conditions) == 0:
            where: Optional[Dict[str, Any]] = None
        elif len(conditions) == 1:
            where = conditions[0]
        else:
            where = {"$and": conditions}

        try:
            hits = self._embeddings.search(
                query=query,
                n_results=n_results,
                where=where,
            )
        except Exception as exc:
            logger.error("search_semantic: ChromaDB query failed: %s", exc)
            return []

        results: List[Tuple[MemoryEntry, float]] = []
        for hit in hits:
            similarity = hit.get("similarity", 0.0)
            if similarity < min_similarity:
                continue
            meta = hit.get("metadata", {})
            entry_id = meta.get("entry_id")
            if entry_id is None:
                continue
            entry = self.get_by_id(int(entry_id))
            if entry:
                results.append((entry, similarity))

        logger.info(
            "search_semantic: query='%s...' → %d results", query[:50], len(results)
        )
        return results

    def get_by_id(self, entry_id: int) -> Optional[MemoryEntry]:
        """
        Get a memory entry by ID.

        Args:
            entry_id: Entry ID

        Returns:
            MemoryEntry or None if not found
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM memory WHERE id = ?",
                (entry_id,)
            )
            row = cursor.fetchone()
            if row:
                entry = MemoryEntry(**dict(row))
                # Update last_accessed_at if extend_ttl_on_access enabled
                self._touch_entries([entry.id])
                return entry
            return None

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

    def cleanup_expired(self) -> int:
        """
        Archive memory entries with expired TTL.
        
        Entries are expired when:
        - ttl_days IS NOT NULL (has TTL)
        - datetime(last_accessed_at, '+' || ttl_days || ' days') < datetime('now')
        
        Returns:
            Number of archived entries
        """
        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE memory 
                SET is_archived = 1, updated_at = datetime('now')
                WHERE is_archived = 0
                  AND ttl_days IS NOT NULL
                  AND last_accessed_at IS NOT NULL
                  AND datetime(last_accessed_at, '+' || ttl_days || ' days') < datetime('now')
            """)
            conn.commit()
            
            count = cursor.rowcount
            if count > 0:
                logger.info(f"⏰ Archived {count} expired TTL entries")
            return count

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
                "db_size_mb": self.get_db_size_mb()
            }

    def get_db_size_mb(self) -> float:
        """
        Get database file size in megabytes.
        
        Returns:
            Database file size in MB, or 0.0 if file doesn't exist
        """
        if self.db_path.exists():
            return self.db_path.stat().st_size / (1024 * 1024)
        return 0.0

    def check_cleanup_needed(self, threshold_mb: float = 100.0) -> Dict[str, Any]:
        """
        Check if database cleanup is needed based on size threshold.
        
        Args:
            threshold_mb: Maximum allowed database size in MB
            
        Returns:
            Dict with keys:
            - needed: bool - whether cleanup is required
            - current_size_mb: float - current DB size
            - threshold_mb: float - configured threshold
            - archived_count: int - count of archived entries
            - low_importance_count: int - entries with importance < 0.3
            - expired_ttl_count: int - entries with expired TTL
        """
        current_size_mb = self.get_db_size_mb()
        
        with self._get_connection() as conn:
            # Count archived entries (candidates for hard deletion)
            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM memory WHERE is_archived = 1"
            )
            archived_count = cursor.fetchone()["count"]
            
            # Count low importance entries (candidates for cleanup)
            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM memory WHERE importance < 0.3 AND is_archived = 0"
            )
            low_importance_count = cursor.fetchone()["count"]
            
            # Count expired TTL entries
            cursor = conn.execute("""
                SELECT COUNT(*) as count FROM memory 
                WHERE is_archived = 0
                  AND ttl_days IS NOT NULL
                  AND last_accessed_at IS NOT NULL
                  AND datetime(last_accessed_at, '+' || ttl_days || ' days') < datetime('now')
            """)
            expired_ttl_count = cursor.fetchone()["count"]
        
        return {
            "needed": current_size_mb >= threshold_mb,
            "current_size_mb": current_size_mb,
            "threshold_mb": threshold_mb,
            "archived_count": archived_count,
            "low_importance_count": low_importance_count,
            "expired_ttl_count": expired_ttl_count
        }

    def run_cleanup(
        self,
        strategy: str = "balanced",
        max_age_days: Optional[int] = None,
        min_importance: float = 0.3,
        hard_delete_archived: bool = True
    ) -> Dict[str, int]:
        """
        Run database cleanup based on specified strategy.
        
        Args:
            strategy: Cleanup strategy - 'aggressive', 'balanced', or 'conservative'
                - aggressive: delete archived + low importance + expired TTL
                - balanced: delete archived + expired TTL (default)
                - conservative: only hard delete archived entries
            max_age_days: Optional max age for entries (not yet implemented)
            min_importance: Minimum importance threshold (for aggressive strategy)
            hard_delete_archived: Whether to hard-delete archived entries
            
        Returns:
            Dict with counts of deleted entries by category:
            - archived_deleted: int - hard-deleted archived entries
            - expired_deleted: int - archived expired TTL entries
            - low_importance_deleted: int - archived low importance entries
        """
        result = {
            "archived_deleted": 0,
            "expired_deleted": 0,
            "low_importance_deleted": 0
        }
        
        with self._get_connection() as conn:
            # 1. Hard delete archived entries (if enabled)
            if hard_delete_archived:
                cursor = conn.execute("SELECT COUNT(*) as count FROM memory WHERE is_archived = 1")
                archived_count = cursor.fetchone()["count"]
                
                if archived_count > 0:
                    # Let the DELETE trigger update the external-content FTS table.
                    # Direct DELETEs against the FTS table can corrupt the index.
                    conn.execute("DELETE FROM memory WHERE is_archived = 1")
                    conn.commit()
                    result["archived_deleted"] = archived_count
                    logger.info(f"🗑️ Hard deleted {archived_count} archived entries")
            
            # 2. Handle expired TTL entries (balanced and aggressive strategies)
            if strategy in ("balanced", "aggressive"):
                expired_count = self.cleanup_expired()
                result["expired_deleted"] = expired_count
            
            # 3. Handle low importance entries (aggressive strategy only)
            if strategy == "aggressive":
                cursor = conn.execute("""
                    UPDATE memory 
                    SET is_archived = 1, updated_at = datetime('now')
                    WHERE is_archived = 0
                      AND importance < ?
                """, (min_importance,))
                conn.commit()
                low_importance_count = cursor.rowcount
                if low_importance_count > 0:
                    logger.info(f"📦 Archived {low_importance_count} low importance entries")
                    result["low_importance_deleted"] = low_importance_count
        
        total = sum(result.values())
        if total > 0:
            logger.info(f"🧹 Cleanup complete: {result}")
        
        return result

    def _get_entry_by_id(self, entry_id: int) -> Optional[MemoryEntry]:
        """Get single entry by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM memory WHERE id = ? AND is_archived = 0", (entry_id,))
            row = cursor.fetchone()
            if row:
                return MemoryEntry(**dict(row))
            return None

    def _get_outgoing_connections(self, entry_id: int) -> List[Tuple[int, str]]:
        """Get outgoing connections from entry."""
        entry = self._get_entry_by_id(entry_id)
        if not entry:
            return []
        try:
            conns = json.loads(entry.connections or '[]')
            return [(int(c['target_id']), c['relation']) for c in conns if c.get('target_id') and isinstance(c.get('target_id'), (int, str))]
        except (json.JSONDecodeError, KeyError, ValueError):
            return []

    def _get_incoming_connections(self, target_id: int) -> List[Tuple[int, str]]:
        """Get incoming connections to target."""
        with self._get_connection() as conn:
            cursor = conn.execute(""
                "SELECT m.id as source_id, json_extract(c.value, '$.relation') as relation "
                "FROM memory m, json_each(m.connections) c "
                "WHERE json_extract(c.value, '$.target_id') = ? "
                "AND m.is_archived = 0"
                "", (target_id,))
            return [(int(row['source_id']), row['relation'] or '') for row in cursor.fetchall()]

    def get_entity_graph(self, entity: str, limit: int = 20) -> List[MemoryEntry]:
        """Find all records where entity is mentioned in entities. Uses JSON search."""
        with self._get_connection() as conn:
            cursor = conn.execute(""
                "SELECT * FROM memory "
                "WHERE EXISTS ("
                "    SELECT 1 FROM json_each(entities) "
                "    WHERE json_extract(value, '$.name') = ?"
                ") "
                "AND is_archived = 0 "
                "ORDER BY importance DESC, updated_at DESC "
                "LIMIT ?"
                "", (entity, limit))
            entries = [MemoryEntry(**dict(row)) for row in cursor.fetchall()]
            self._touch_entries([e.id for e in entries])
            return entries

    def get_connected_entries(self, entry_id: int, max_depth: int = 2) -> List[Tuple[MemoryEntry, str]]:
        """BFS graph traversal from entry_id up to max_depth.\nBidirectional (outgoing + incoming)."""
        start_entry = self._get_entry_by_id(entry_id)
        if not start_entry:
            return []

        from collections import deque
        visited = set()
        queue = deque([(entry_id, 0, None)])
        connected: List[Tuple[MemoryEntry, str]] = []

        while queue:
            cid, depth, rel = queue.popleft()
            if cid in visited:
                continue
            visited.add(cid)

            if rel is not None:
                entry = self._get_entry_by_id(cid)
                if entry:
                    connected.append((entry, rel))

            if depth >= max_depth:
                continue

            # Outgoing
            for tid, r in self._get_outgoing_connections(cid):
                if tid not in visited:
                    queue.append((tid, depth + 1, r))

            # Incoming
            for sid, r in self._get_incoming_connections(cid):
                if sid not in visited:
                    queue.append((sid, depth + 1, r))

        self._touch_entries([e.id for e, _ in connected])
        return connected

    def find_entity_connections(self, entity1: str, entity2: str, max_depth: int = 3) -> List[dict]:
        """Find shortest path between entries containing entity1 and entity2 using BFS."""
        starts = self.get_entity_graph(entity1, limit=50)
        if not starts:
            return []
        start_ids = {e.id for e in starts}

        targets_list = self.get_entity_graph(entity2, limit=50)
        if not targets_list:
            return []
        target_ids = {e.id for e in targets_list}

        if start_ids & target_ids:
            return []  # Direct overlap

        visited = set(start_ids)
        queue = deque((sid, 0) for sid in start_ids)
        parent: Dict[int, int] = {sid: None for sid in start_ids}
        relation_to: Dict[int, str] = {sid: '' for sid in start_ids}

        found_id = None
        while queue:
            cid, depth = queue.popleft()
            if cid in target_ids:
                found_id = cid
                break
            if depth >= max_depth:
                continue

            neighbors = self._get_outgoing_connections(cid) + self._get_incoming_connections(cid)
            for nid, r in neighbors:
                if nid not in visited:
                    visited.add(nid)
                    queue.append((nid, depth + 1))
                    parent[nid] = cid
                    relation_to[nid] = r

        if not found_id:
            return []

        # Reconstruct path
        path = []
        current = found_id
        while parent.get(current) is not None:
            p = parent[current]
            r = relation_to[current]
            to_entry = self._get_entry_by_id(current)
            entities = json.loads(to_entry.entities or '[]') if to_entry else []
            entity_name = entities[0].get('name', '') if entities else ''
            path.append({
                "from_id": p,
                "to_id": current,
                "relation": r,
                "entity": entity_name
            })
            current = p

        path.reverse()
        self._touch_entries(list(parent.keys()) + [found_id])
        return path

    def vacuum_db(self) -> Dict[str, Any]:
        """
        Run VACUUM on the database to reclaim space and optimize.
        
        VACUUM rebuilds the database file, which:
        - Reclaims unused space from deleted rows
        - Defragments the database
        - Improves query performance
        
        Note: VACUUM requires exclusive lock and may take time on large databases.
        
        Returns:
            Dict with:
            - success: bool - whether vacuum succeeded
            - size_before_mb: float - size before vacuum
            - size_after_mb: float - size after vacuum (0 if failed)
            - reclaimed_mb: float - space reclaimed (0 if failed)
            - error: str - error message if failed
        """
        size_before_mb = self.get_db_size_mb()
        
        try:
            # VACUUM requires a fresh connection without WAL mode active
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("VACUUM")
                conn.execute("PRAGMA journal_mode=WAL")
            
            size_after_mb = self.get_db_size_mb()
            reclaimed_mb = size_before_mb - size_after_mb
            
            logger.info(
                f"✅ VACUUM complete: {size_before_mb:.2f}MB -> {size_after_mb:.2f}MB "
                f"(reclaimed {reclaimed_mb:.2f}MB)"
            )
            
            return {
                "success": True,
                "size_before_mb": round(size_before_mb, 2),
                "size_after_mb": round(size_after_mb, 2),
                "reclaimed_mb": round(reclaimed_mb, 2)
            }
        except Exception as e:
            logger.error(f"❌ VACUUM failed: {e}")
            return {
                "success": False,
                "size_before_mb": round(size_before_mb, 2),
                "size_after_mb": 0.0,
                "reclaimed_mb": 0.0,
                "error": str(e)
            }

    # ══════════════════════════════════════════════════════════════════════════
    # SKILLS — SQL-only storage (no filesystem)
    # ══════════════════════════════════════════════════════════════════════════

    def _extract_summary(self, content: str) -> str:
        """Return first non-empty line stripped of leading # chars (max 200 chars)."""
        for line in content.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:200]
        return ""

    def save_skill(
        self,
        name: str,
        content: str,
        tags: str = "",
        user_id: str = "default_user",
        agent_id: str = "default_agent",
    ) -> int:
        """
        Upsert a skill.  If (name, user_id, agent_id) already exists the
        content / tags / summary are updated and the existing id is returned.
        """
        summary = self._extract_summary(content)
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO skills (name, content, tags, user_id, agent_id, summary)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name, user_id, agent_id) DO UPDATE SET
                    content  = excluded.content,
                    tags     = excluded.tags,
                    summary  = excluded.summary,
                    updated_at = datetime('now')
                """,
                (name, content, tags, user_id, agent_id, summary),
            )
            conn.commit()
            # lastrowid is 0 on UPDATE — fetch real id
            row = conn.execute(
                "SELECT id FROM skills WHERE name=? AND user_id=? AND agent_id=?",
                (name, user_id, agent_id),
            ).fetchone()
            return row["id"] if row else (cursor.lastrowid or 0)

    def get_skill(
        self,
        name: str,
        user_id: str = "default_user",
        agent_id: str = "default_agent",
    ) -> Optional["SkillEntry"]:
        """Return a SkillEntry by exact name, or None if not found."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM skills WHERE name=? AND user_id=? AND agent_id=?",
                (name, user_id, agent_id),
            ).fetchone()
            return SkillEntry(**dict(row)) if row else None

    def search_skills(
        self,
        query: str = "",
        tags: str = "",
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 20,
    ) -> List["SkillEntry"]:
        """
        Search skills.

        Args:
            query: FTS5 full-text query (name + content + tags + summary).
                   Empty = return recent entries.
            tags:  Comma-separated tag filter (LIKE match, all must match).
            user_id:  Optional user filter.
            agent_id: Optional agent filter (omit for cross-agent search).
            limit:    Max results.
        """
        conditions: List[str] = []
        params: List[Any] = []

        if user_id:
            conditions.append("s.user_id = ?")
            params.append(user_id)
        if agent_id:
            conditions.append("s.agent_id = ?")
            params.append(agent_id)
        if tags:
            for tag in (t.strip() for t in tags.split(",") if t.strip()):
                conditions.append("s.tags LIKE ?")
                params.append(f"%{tag}%")

        with self._get_connection() as conn:
            if query.strip():
                fts_tokens = " ".join(
                    f'"{t.replace(chr(34), "")}"' for t in query.split()
                )
                extra_where = (" AND " + " AND ".join(conditions)) if conditions else ""
                sql = f"""
                    SELECT s.* FROM skills s
                    JOIN skills_fts f ON s.id = f.rowid
                    WHERE skills_fts MATCH ?{extra_where}
                    ORDER BY rank
                    LIMIT ?
                """
                rows = conn.execute(sql, [fts_tokens] + params + [limit]).fetchall()
            else:
                where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
                sql = f"SELECT * FROM skills s {where} ORDER BY s.updated_at DESC LIMIT ?"
                rows = conn.execute(sql, params + [limit]).fetchall()

            return [SkillEntry(**dict(r)) for r in rows]

    def update_skill(
        self,
        skill_id: int,
        name: Optional[str] = None,
        content: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> bool:
        """
        Update skill fields by id.

        Returns True if the row was found and updated, False otherwise.
        At least one of name / content / tags must be provided.
        """
        updates: List[str] = []
        params: List[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if content is not None:
            updates.append("content = ?")
            params.append(content)
            updates.append("summary = ?")
            params.append(self._extract_summary(content))
        if tags is not None:
            updates.append("tags = ?")
            params.append(tags)

        if not updates:
            return False

        updates.append("updated_at = datetime('now')")
        params.append(skill_id)

        with self._get_connection() as conn:
            cur = conn.execute(
                f"UPDATE skills SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_skill(self, skill_id: int) -> bool:
        """Delete skill by id. Returns True if a row was deleted."""
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
            conn.commit()
            return cur.rowcount > 0
