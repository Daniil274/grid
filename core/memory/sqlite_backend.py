"""Workspace/tenant isolated SQLite memory with optional FTS5 and lexical RRF."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import ClassVar, Self

from .models import (
    MemoryContextPack,
    MemoryLayer,
    MemoryQuery,
    MemoryRecord,
    MemorySearchHit,
    StaleStatus,
    build_context_pack,
    list_records_via_search,
    reciprocal_rank_fusion,
    record_from_dict,
    record_to_dict,
    utc_now,
)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def _trigrams(text: str) -> set[str]:
    text = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return {text[i:i + 3] for i in range(max(0, len(text) - 2))}


def _content_hash(content: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", content).casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


class SQLiteMemoryBackend:
    name = "sqlite"
    _tables: ClassVar[Mapping[MemoryLayer, str]] = MappingProxyType({
        MemoryLayer.L0: "l0_refs", MemoryLayer.L1: "l1_facts",
        MemoryLayer.L2: "l2_scenarios", MemoryLayer.L3: "l3_profile",
    })

    def __init__(self, workspace_root: str | Path, db_path: str | Path | None = None,
                 *, tenant_id: str = "default", enable_fts: bool = True) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir() or not tenant_id:
            raise ValueError("A workspace directory and tenant_id are required")
        self.db_path = Path(db_path) if db_path is not None else self.workspace_root / ".grid/memory.sqlite"
        if not self.db_path.is_absolute():
            self.db_path = self.workspace_root / self.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = hashlib.sha256(json.dumps(
            [str(self.workspace_root), tenant_id], ensure_ascii=False).encode()).hexdigest()
        self._lock = RLock()
        self._closed = False
        self._fts = False
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            for table in self._tables.values():
                conn.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                    namespace TEXT NOT NULL, id TEXT NOT NULL, scope TEXT NOT NULL,
                    category TEXT NOT NULL, memory_key TEXT NOT NULL, content_hash TEXT NOT NULL,
                    content TEXT NOT NULL, payload TEXT NOT NULL, superseded_by TEXT,
                    PRIMARY KEY(namespace, id))""")
                conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_dedup ON {table} "
                             "(namespace,scope,category,memory_key,content_hash) WHERE superseded_by IS NULL")
            if enable_fts:
                try:
                    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(namespace UNINDEXED, id UNINDEXED, layer UNINDEXED, content)")
                    self._fts = True
                    # Reconcile indices also when a database was previously opened without FTS.
                    conn.execute("DELETE FROM memory_fts WHERE namespace=?", (self.namespace,))
                    for layer, table in self._tables.items():
                        conn.execute(f"INSERT INTO memory_fts(namespace,id,layer,content) SELECT namespace,id,?,content FROM {table} WHERE namespace=?", (layer.value, self.namespace))
                except sqlite3.OperationalError as exc:
                    if "no such module" not in str(exc).lower():
                        raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Memory backend is closed")
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=10000")
                with conn:
                    yield conn
            finally:
                conn.close()

    def forget(self, record_id: str) -> bool:
        """Delete only within this host-bound namespace, including the index."""
        removed = False
        with self._connection() as conn:
            for table in self._tables.values():
                cursor = conn.execute(f"DELETE FROM {table} WHERE namespace=? AND id=?",
                                      (self.namespace, record_id))
                removed = removed or cursor.rowcount > 0
            if self._fts:
                conn.execute("DELETE FROM memory_fts WHERE namespace=? AND id=?", (self.namespace, record_id))
        return removed

    def _safe_path(self, file_path: str) -> Path:
        path = Path(file_path)
        path = (path if path.is_absolute() else self.workspace_root / path).resolve()
        if not path.is_relative_to(self.workspace_root):
            raise ValueError("Memory file_path escapes workspace")
        return path

    def _fingerprint(self, path: Path) -> tuple[int, str]:
        """Stream a regular file through anchored, no-follow descriptors (POSIX).

        Resolving alone is not sufficient: a symlink could be swapped between
        validation and opening. Descriptor traversal closes that escape route.
        """
        parts = path.relative_to(self.workspace_root).parts
        if not parts or not hasattr(os, "O_NOFOLLOW"):
            raise OSError("Secure regular-file access unavailable")
        fd = os.open(self.workspace_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in parts[:-1]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise OSError("Memory source is not a regular file")
                digest = hashlib.sha256()
                while chunk := stream.read(65536):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    raise OSError("Memory source changed while being read")
                return after.st_mtime_ns, digest.hexdigest()
        finally:
            os.close(fd)

    def _snapshot(self, record: MemoryRecord) -> MemoryRecord:
        if not record.file_path and record.source_ref:
            if record.source_ref.startswith("file:"):
                record = replace(record, file_path=record.source_ref[5:])
            elif ":" not in record.source_ref:
                candidate = self._safe_path(record.source_ref)
                if candidate.is_file():
                    record = replace(record, file_path=record.source_ref)
        if not record.file_path:
            return replace(record, stale_status=StaleStatus.UNVERIFIED)
        path = self._safe_path(record.file_path)
        relative = str(path.relative_to(self.workspace_root))
        try:
            mtime, digest = self._fingerprint(path)
        except OSError:
            return replace(record, file_path=relative, file_mtime_ns=None,
                           file_sha256=None, stale_status=StaleStatus.UNVERIFIED)
        return replace(record, file_path=relative, file_mtime_ns=mtime,
                       file_sha256=digest, stale_status=StaleStatus.FRESH)

    def check_staleness(self, record: MemoryRecord) -> MemoryRecord:
        if not record.file_path:
            return replace(record, stale_status=StaleStatus.UNVERIFIED)
        try:
            path = self._safe_path(record.file_path)
            mtime = path.stat().st_mtime_ns
            if record.file_sha256 is None:
                status = StaleStatus.UNVERIFIED
            elif mtime == record.file_mtime_ns:
                status = StaleStatus.FRESH
            else:
                status = (StaleStatus.FRESH if self._fingerprint(path)[1]
                          == record.file_sha256 else StaleStatus.STALE)
        except (OSError, ValueError):
            status = StaleStatus.STALE
        return replace(record, stale_status=status)

    def remember(self, record: MemoryRecord, *, supersedes: str | None = None) -> MemoryRecord:
        record = self._snapshot(replace(record, updated_at=utc_now()))
        table = self._tables[record.layer]
        digest = _content_hash(record.content)
        with self._connection() as conn:
            # Serialize read-modify-write across processes as well as threads.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(f"SELECT payload FROM {table} WHERE namespace=? AND scope=? AND category=? AND memory_key=? AND content_hash=? AND superseded_by IS NULL",
                                    (self.namespace, record.scope, record.category, record.key, digest)).fetchone()
            if existing:
                old = record_from_dict(json.loads(existing[0]))
                record = replace(record, id=old.id, created_at=old.created_at)
            if supersedes and supersedes != record.id:
                old_row = conn.execute(f"SELECT payload FROM {table} WHERE namespace=? AND id=?", (self.namespace, supersedes)).fetchone()
                if old_row is None:
                    raise ValueError("Superseded record does not exist in this namespace/layer")
                old = replace(record_from_dict(json.loads(old_row[0])), superseded_by=record.id, updated_at=utc_now())
                conn.execute(f"UPDATE {table} SET superseded_by=?,payload=? WHERE namespace=? AND id=?",
                             (record.id, json.dumps(record_to_dict(old)), self.namespace, supersedes))
            conn.execute(f"""INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(namespace,id) DO UPDATE SET scope=excluded.scope, category=excluded.category,
                memory_key=excluded.memory_key, content_hash=excluded.content_hash, content=excluded.content,
                payload=excluded.payload, superseded_by=excluded.superseded_by""",
                         (self.namespace, record.id, record.scope, record.category, record.key, digest,
                          record.content, json.dumps(record_to_dict(record)), record.superseded_by))
            if self._fts:
                conn.execute("DELETE FROM memory_fts WHERE namespace=? AND id=? AND layer=?", (self.namespace, record.id, record.layer))
                conn.execute("INSERT INTO memory_fts VALUES (?,?,?,?)", (self.namespace, record.id, record.layer, record.content))
        return record

    def get(self, record_id: str) -> MemoryRecord | None:
        with self._connection() as conn:
            for table in self._tables.values():
                row = conn.execute(f"SELECT payload FROM {table} WHERE namespace=? AND id=?", (self.namespace, record_id)).fetchone()
                if row:
                    return self.check_staleness(record_from_dict(json.loads(row[0])))
        return None

    def search(self, query: MemoryQuery) -> list[MemorySearchHit]:
        if not query.layers or not query.scopes:
            return []
        records: dict[str, MemoryRecord] = {}
        bm25: list[str] = []
        with self._connection() as conn:
            for layer in query.layers:
                table = self._tables[layer]
                rows = conn.execute(f"SELECT payload FROM {table} WHERE namespace=?" +
                                    ("" if query.include_superseded else " AND superseded_by IS NULL"), (self.namespace,))
                for row in rows:
                    record = self.check_staleness(record_from_dict(json.loads(row[0])))
                    if record.scope in query.scopes and (query.include_stale or record.stale_status != StaleStatus.STALE):
                        records[record.id] = record
            tokens = sorted(_tokens(query.text))
            if tokens and self._fts:
                expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
                rows = conn.execute("SELECT id FROM memory_fts WHERE memory_fts MATCH ? AND namespace=? ORDER BY bm25(memory_fts),id", (expression, self.namespace))
                bm25 = list(dict.fromkeys(row[0] for row in rows if row[0] in records))
        if not query.text.strip():
            ordered = sorted(records.values(), key=lambda r: (r.updated_at, r.id), reverse=True)
            return [MemorySearchHit(r, r.confidence) for r in ordered[:query.limit]]
        query_tokens, query_tri = _tokens(query.text), _trigrams(query.text)
        lexical_scores: dict[str, float] = {}
        for record in records.values():
            text = " ".join((record.content, record.title, record.key, *record.tags))
            tokens, tri = _tokens(text), _trigrams(text)
            score = len(tokens & query_tokens) / max(1, len(query_tokens))
            score += len(tri & query_tri) / max(1, len(tri | query_tri))
            if score > 0:
                lexical_scores[record.id] = score
        lexical = sorted(lexical_scores, key=lambda rid: (-lexical_scores[rid], rid))
        fused = reciprocal_rank_fusion([bm25, lexical])
        bm_ranks = {rid: i for i, rid in enumerate(bm25, 1)}
        lex_ranks = {rid: i for i, rid in enumerate(lexical, 1)}
        return [MemorySearchHit(records[rid], score, bm_ranks.get(rid), lex_ranks.get(rid), score)
                for rid, score in list(fused.items())[:query.limit]]

    def build_context(self, query: MemoryQuery) -> MemoryContextPack:
        return build_context_pack(self, query)

    def list_records(self, **options: object) -> list[MemoryRecord]:
        """Browse this namespace (see models.list_records_via_search)."""
        return list_records_via_search(self, **options)  # type: ignore[arg-type]

    def stats(self) -> dict[str, object]:
        """Exact live record counts per layer, with staleness checked now."""
        counts: dict[str, int] = {}
        stale = 0
        with self._connection() as conn:
            for layer, table in self._tables.items():
                rows = conn.execute(f"SELECT payload FROM {table} WHERE namespace=? AND superseded_by IS NULL",
                                    (self.namespace,)).fetchall()
                counts[layer.value] = len(rows)
                stale += sum(1 for row in rows if self.check_staleness(
                    record_from_dict(json.loads(row[0]))).stale_status == StaleStatus.STALE)
        return {"counts": counts, "stale_count": stale, "total": sum(counts.values())}

    def healthcheck(self) -> bool:
        try:
            with self._connection() as conn:
                return conn.execute("SELECT 1").fetchone()[0] == 1
        except (sqlite3.Error, RuntimeError):
            return False

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
