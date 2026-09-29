"""The reviews of a server: their rows, and the evidence each one froze.

``reviews.db`` (SQLite) holds one row per review; the evidence, often large,
is a JSON file beside it in ``cases/<id>.json``, written whole or not at all.
Both live outside every user's space, in the directory the server gives.

As in web_chat.accounts.store, every statement runs under one lock and the
schema is versioned with ``PRAGMA user_version``. This module only stores and
fetches; who may file or read a review is web_chat.review.desk's to decide.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

Status = Literal["new", "in_review", "proposed", "closed"]
STATUSES: tuple[Status, ...] = ("new", "in_review", "proposed", "closed")
#: "user": filed by the user from their chat; "admin": opened by an admin on
#: a chat the user did not send.
Origin = Literal["user", "admin"]

MIGRATIONS: tuple[str, ...] = (
    # 1: reviews.
    """
    CREATE TABLE reviews (
        id         TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        user_id    TEXT NOT NULL,
        username   TEXT NOT NULL,
        context_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        system     TEXT,
        agent      TEXT,
        note       TEXT NOT NULL DEFAULT '',
        status     TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'in_review', 'proposed', 'closed')),
        origin     TEXT NOT NULL CHECK (origin IN ('user', 'admin')),
        opened_by  TEXT
    );
    CREATE INDEX reviews_by_user ON reviews (user_id, created_at);
    CREATE INDEX reviews_by_status ON reviews (status, created_at);
    """,
)


@dataclass(frozen=True)
class Review:
    id: str
    created_at: float
    user_id: str
    username: str
    context_id: str
    message_id: str
    system: Optional[str]
    agent: Optional[str]
    note: str
    status: Status
    origin: Origin
    #: The admin who opened it, for origin "admin".
    opened_by: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class ReviewStore:
    """Rows of the reviews database and the evidence files beside it."""

    def __init__(self, root: Path) -> None:
        self._cases = root / "cases"
        self._cases.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(root / "reviews.db"), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise RuntimeError(
                f"The reviews database is at version {version}, newer than this code "
                f"({len(MIGRATIONS)}). Upgrade Grid before opening it."
            )
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for statement in filter(str.strip, script.split(";")):
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {number}")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # -- reviews -----------------------------------------------------------------
    def add(self, review: Review, evidence: dict[str, Any]) -> None:
        """Store *review* with its evidence: the file first, so a row always has one."""
        path = self._case(review.id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO reviews (id, created_at, user_id, username, context_id, message_id, system, agent,"
                    " note, status, origin, opened_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        review.id, review.created_at, review.user_id, review.username, review.context_id,
                        review.message_id, review.system, review.agent, review.note, review.status,
                        review.origin, review.opened_by,
                    ),
                )
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def get(self, review_id: str) -> Optional[Review]:
        with self._lock:
            row = self._db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return _review(row) if row is not None else None

    def evidence(self, review_id: str) -> Optional[dict[str, Any]]:
        path = self._case(review_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def of_user(self, user_id: str) -> list[Review]:
        return self._reviews("SELECT * FROM reviews WHERE user_id = ? ORDER BY created_at DESC", user_id)

    def all(self, status: Optional[Status] = None) -> list[Review]:
        if status is None:
            return self._reviews("SELECT * FROM reviews ORDER BY created_at DESC")
        return self._reviews("SELECT * FROM reviews WHERE status = ? ORDER BY created_at DESC", status)

    def set_status(self, review_id: str, status: Status) -> bool:
        with self._lock:
            return self._db.execute("UPDATE reviews SET status = ? WHERE id = ?", (status, review_id)).rowcount == 1

    def count_filed_since(self, user_id: str, since: float) -> int:
        """Reviews *user_id* filed themselves since *since*: what the daily limit counts."""
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM reviews WHERE user_id = ? AND origin = 'user' AND created_at >= ?",
                (user_id, since),
            ).fetchone()[0]

    # -- plumbing ----------------------------------------------------------------
    def _case(self, review_id: str) -> Path:
        if not review_id.isalnum():
            raise ValueError(f"Not a review id: {review_id!r}")
        return self._cases / f"{review_id}.json"

    def _reviews(self, sql: str, *args: object) -> list[Review]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [_review(row) for row in rows]


def _review(row: sqlite3.Row) -> Review:
    return Review(**{key: row[key] for key in row.keys()})
