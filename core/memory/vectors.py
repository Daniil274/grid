"""Embedding vectors of memory records, kept beside them in the same SQLite file.

One row per (namespace, record, model): unit-length float32 vectors plus the hash
of the text they were computed from, so an edited record is re-embedded and a
change of embedding model simply leaves the old model's rows unused. A memory is a
compact store of distilled records - hundreds, not millions - so nearest
neighbours are an exact scan; no vector database is needed.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from array import array
from collections.abc import Iterable, Sequence
from pathlib import Path

from .models import MemoryRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_vectors (
    namespace TEXT NOT NULL,
    id TEXT NOT NULL,
    model TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (namespace, id, model)
)
"""


def embedding_text(record: MemoryRecord) -> str:
    """What a record is embedded as: its text and its search tags."""
    tags = ", ".join(record.tags)
    return f"{record.content}\n{tags}" if tags else record.content


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _pack(vector: Sequence[float]) -> bytes:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return array("f", (x / norm for x in vector)).tobytes()


def _unpack(blob: bytes) -> array:
    vector = array("f")
    vector.frombytes(blob)
    return vector


class VectorIndex:
    """The vectors of one memory namespace in *db_path*."""

    def __init__(self, db_path: str | Path, namespace: str) -> None:
        self.db_path = Path(db_path)
        self.namespace = namespace
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def missing(self, model: str, records: Iterable[MemoryRecord]) -> list[MemoryRecord]:
        """Records without a vector of *model* for their current text."""
        records = list(records)
        with self._connect() as conn:
            known = dict(conn.execute(
                "SELECT id, text_hash FROM memory_vectors WHERE namespace=? AND model=?",
                (self.namespace, model)).fetchall())
        return [r for r in records if known.get(r.id) != _hash(embedding_text(r))]

    def put(self, model: str, pairs: Iterable[tuple[MemoryRecord, Sequence[float]]]) -> int:
        rows = [(self.namespace, record.id, model, _hash(embedding_text(record)), _pack(vector))
                for record, vector in pairs if vector]
        with self._connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO memory_vectors VALUES (?, ?, ?, ?, ?)", rows)
        return len(rows)

    def nearest(self, model: str, query: Sequence[float], ids: Iterable[str]) -> list[tuple[str, float]]:
        """Cosine similarity of the query to each of *ids* that has a vector, best first."""
        wanted = set(ids)
        if not wanted or not query:
            return []
        norm = math.sqrt(sum(x * x for x in query)) or 1.0
        unit = [x / norm for x in query]
        with self._connect() as conn:
            rows = conn.execute("SELECT id, vector FROM memory_vectors WHERE namespace=? AND model=?",
                                (self.namespace, model)).fetchall()
        scored = []
        for record_id, blob in rows:
            if record_id in wanted:
                vector = _unpack(blob)
                if len(vector) == len(unit):
                    scored.append((record_id, sum(a * b for a, b in zip(unit, vector))))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored

    def prune(self, live_ids: Iterable[str]) -> int:
        """Drop the vectors of records that no longer exist (forgotten or replaced)."""
        live = set(live_ids)
        with self._connect() as conn:
            stale = [(self.namespace, rid) for (rid,) in conn.execute(
                "SELECT DISTINCT id FROM memory_vectors WHERE namespace=?", (self.namespace,)) if rid not in live]
            conn.executemany("DELETE FROM memory_vectors WHERE namespace=? AND id=?", stale)
        return len(stale)
