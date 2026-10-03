"""The accounts database: users, their sessions and the invites that admit them.

SQLite in one file (``accounts.db`` in the server's data directory), opened
once per process. Every statement runs under one lock, so the connection is
shared safely by the event loop and worker threads; each operation is a few
indexed row reads or writes, far below a millisecond.

The schema is versioned with ``PRAGMA user_version``: :data:`MIGRATIONS` holds
one script per version, applied in order inside a transaction, so a database
is always at exactly one known version.

This module only stores and fetches. What may be stored - who can sign in,
which invite admits whom - is decided by web_chat.accounts.service.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from web_chat.identity import Role, User

MIGRATIONS: tuple[str, ...] = (
    # 1: users, sessions, invites.
    """
    CREATE TABLE users (
        id            TEXT PRIMARY KEY,
        username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
        disabled      INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
        created_at    REAL NOT NULL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        last_seen  REAL NOT NULL
    );
    CREATE INDEX sessions_by_user ON sessions (user_id);
    CREATE TABLE invites (
        id         TEXT PRIMARY KEY,
        code_hash  TEXT NOT NULL UNIQUE,
        role       TEXT NOT NULL CHECK (role IN ('admin', 'user')),
        note       TEXT NOT NULL DEFAULT '',
        created_by TEXT REFERENCES users (id) ON DELETE SET NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        used_by    TEXT REFERENCES users (id) ON DELETE SET NULL,
        used_at    REAL
    );
    """,
    # 2: turns started per user and UTC day, for the daily limit.
    """
    CREATE TABLE usage (
        user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        day     TEXT NOT NULL,
        turns   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    );
    """,
    # 3: tokens spent per user and UTC day, for the token budgets.
    """
    ALTER TABLE usage ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE usage ADD COLUMN tokens_out INTEGER NOT NULL DEFAULT 0;
    """,
)


@dataclass(frozen=True)
class UserRecord:
    """A users row: the identity plus what only the accounts service reads."""

    user: User
    password_hash: str
    disabled: bool
    created_at: float


@dataclass(frozen=True)
class SessionRecord:
    user_id: str
    created_at: float
    expires_at: float
    last_seen: float


@dataclass(frozen=True)
class InviteRecord:
    id: str
    role: Role
    note: str
    created_by: Optional[str]
    created_at: float
    expires_at: float
    used_by: Optional[str]
    used_at: Optional[float]


class AccountStore:
    """Rows of the accounts database; no rules beyond the schema's."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit; multi-statement work goes through transaction().
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- plumbing ----------------------------------------------------------------
    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise RuntimeError(
                f"The accounts database is at version {version}, newer than this code "
                f"({len(MIGRATIONS)}). Upgrade Grid before opening it."
            )
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.transaction():
                for statement in filter(str.strip, script.split(";")):
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {number}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the block's statements as one unit: all or none of them apply."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _one(self, sql: str, *args: object) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _all(self, sql: str, *args: object) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _write(self, sql: str, *args: object) -> int:
        """Run a write; the number of rows it changed."""
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # -- users -------------------------------------------------------------------
    def add_user(self, user: User, password_hash: str, created_at: float) -> None:
        """Insert a user; raises sqlite3.IntegrityError when the name is taken."""
        self._write(
            "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            user.id, user.username, password_hash, user.role, created_at,
        )

    def user_by_id(self, user_id: str) -> Optional[UserRecord]:
        return _user(self._one("SELECT * FROM users WHERE id = ?", user_id))

    def user_by_name(self, username: str) -> Optional[UserRecord]:
        return _user(self._one("SELECT * FROM users WHERE username = ?", username))

    def users(self) -> list[UserRecord]:
        return [_user(row) for row in self._all("SELECT * FROM users ORDER BY created_at")]

    def count_active_admins(self) -> int:
        return self._one("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0")[0]

    def count_users(self) -> int:
        return self._one("SELECT COUNT(*) FROM users")[0]

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        self._write("UPDATE users SET password_hash = ? WHERE id = ?", password_hash, user_id)

    def set_disabled(self, user_id: str, disabled: bool) -> bool:
        return self._write("UPDATE users SET disabled = ? WHERE id = ?", int(disabled), user_id) == 1

    def set_role(self, user_id: str, role: Role) -> bool:
        return self._write("UPDATE users SET role = ? WHERE id = ?", role, user_id) == 1

    # -- sessions ----------------------------------------------------------------
    def add_session(self, token_hash: str, user_id: str, now: float, expires_at: float) -> None:
        self._write(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen) VALUES (?, ?, ?, ?, ?)",
            token_hash, user_id, now, expires_at, now,
        )

    def session(self, token_hash: str) -> Optional[SessionRecord]:
        row = self._one("SELECT * FROM sessions WHERE token_hash = ?", token_hash)
        if row is None:
            return None
        return SessionRecord(row["user_id"], row["created_at"], row["expires_at"], row["last_seen"])

    def touch_session(self, token_hash: str, last_seen: float, expires_at: float) -> None:
        self._write(
            "UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
            last_seen, expires_at, token_hash,
        )

    def delete_session(self, token_hash: str) -> None:
        self._write("DELETE FROM sessions WHERE token_hash = ?", token_hash)

    def delete_sessions_of(self, user_id: str, *, except_hash: Optional[str] = None) -> int:
        return self._write(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash IS NOT ?", user_id, except_hash
        )

    def delete_expired_sessions(self, now: float) -> int:
        return self._write("DELETE FROM sessions WHERE expires_at <= ?", now)

    # -- usage -------------------------------------------------------------------
    def usage_history(self) -> list[dict]:
        """Daily totals for the one-time analytics baseline; no account secrets."""
        return [dict(row) for row in self._all("SELECT user_id, day, tokens_in, tokens_out FROM usage")]

    def turns_on(self, user_id: str, day: str) -> int:
        row = self._one("SELECT turns FROM usage WHERE user_id = ? AND day = ?", user_id, day)
        return row[0] if row else 0

    def turns_by_user(self, day: str) -> dict[str, int]:
        return {row["user_id"]: row["turns"] for row in self._all("SELECT user_id, turns FROM usage WHERE day = ?", day)}

    def count_turn(self, user_id: str, day: str, limit: Optional[int]) -> bool:
        """Count one more turn of the user on *day*, unless that would pass
        *limit*; whether it was counted. Checking and counting are one step."""
        with self.transaction():
            if limit is not None and self.turns_on(user_id, day) >= limit:
                return False
            self._write(
                "INSERT INTO usage (user_id, day, turns) VALUES (?, ?, 1) "
                "ON CONFLICT (user_id, day) DO UPDATE SET turns = turns + 1",
                user_id, day,
            )
            return True

    def add_tokens(self, user_id: str, day: str, tokens_in: int, tokens_out: int) -> None:
        """Add a finished turn's tokens to the user's day."""
        self._write(
            "INSERT INTO usage (user_id, day, turns, tokens_in, tokens_out) VALUES (?, ?, 0, ?, ?) "
            "ON CONFLICT (user_id, day) DO UPDATE SET "
            "tokens_in = tokens_in + excluded.tokens_in, tokens_out = tokens_out + excluded.tokens_out",
            user_id, day, tokens_in, tokens_out,
        )

    def tokens_on(self, user_id: str, day: str) -> int:
        """The tokens (input plus output) the user spent on *day*."""
        row = self._one(
            "SELECT tokens_in + tokens_out AS tokens FROM usage WHERE user_id = ? AND day = ?",
            user_id, day,
        )
        return row["tokens"] if row else 0

    def tokens_by_user(self, day: str) -> dict[str, int]:
        """Every user's tokens (input plus output) spent on *day*."""
        return {
            row["user_id"]: row["tokens"]
            for row in self._all(
                "SELECT user_id, tokens_in + tokens_out AS tokens FROM usage WHERE day = ?", day
            )
        }

    def usage_totals(self, user_id: str) -> tuple[int, int, int]:
        """The user's all-time turns, prompt tokens and completion tokens."""
        row = self._one(
            "SELECT COALESCE(SUM(turns), 0) AS turns, COALESCE(SUM(tokens_in), 0) AS tokens_in, "
            "COALESCE(SUM(tokens_out), 0) AS tokens_out FROM usage WHERE user_id = ?",
            user_id,
        )
        return (row["turns"], row["tokens_in"], row["tokens_out"])

    def usage_on(self, user_id: str, day: str) -> tuple[int, int, int]:
        """The user's *day*: (turns, prompt tokens, completion tokens)."""
        row = self._one(
            "SELECT turns, tokens_in, tokens_out FROM usage WHERE user_id = ? AND day = ?",
            user_id, day,
        )
        return (row["turns"], row["tokens_in"], row["tokens_out"]) if row else (0, 0, 0)

    # -- invites -----------------------------------------------------------------
    def add_invite(self, invite: InviteRecord, code_hash: str) -> None:
        self._write(
            "INSERT INTO invites (id, code_hash, role, note, created_by, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            invite.id, code_hash, invite.role, invite.note, invite.created_by, invite.created_at, invite.expires_at,
        )

    def invite_by_code(self, code_hash: str) -> Optional[InviteRecord]:
        return _invite(self._one("SELECT * FROM invites WHERE code_hash = ?", code_hash))

    def invites(self) -> list[InviteRecord]:
        return [_invite(row) for row in self._all("SELECT * FROM invites ORDER BY created_at DESC")]

    def mark_invite_used(self, invite_id: str, user_id: str, now: float) -> bool:
        """Spend the invite; False when it was spent already (a concurrent use)."""
        return self._write(
            "UPDATE invites SET used_by = ?, used_at = ? WHERE id = ? AND used_at IS NULL",
            user_id, now, invite_id,
        ) == 1

    def delete_invite(self, invite_id: str) -> bool:
        return self._write("DELETE FROM invites WHERE id = ? AND used_at IS NULL", invite_id) == 1


def _user(row: Optional[sqlite3.Row]) -> Optional[UserRecord]:
    if row is None:
        return None
    return UserRecord(
        user=User(id=row["id"], username=row["username"], role=row["role"]),
        password_hash=row["password_hash"],
        disabled=bool(row["disabled"]),
        created_at=row["created_at"],
    )


def _invite(row: Optional[sqlite3.Row]) -> Optional[InviteRecord]:
    if row is None:
        return None
    return InviteRecord(
        id=row["id"],
        role=row["role"],
        note=row["note"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        used_by=row["used_by"],
        used_at=row["used_at"],
    )
