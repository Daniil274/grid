"""The channel's one SQLite file: links, link codes, chats, approvals, reminders.

One connection per process behind one lock, as the accounts and daily-digest
ledgers do. Only hashes of link codes are stored; a code is single-use and
expires. Nothing here talks to Telegram - the bot, the runner and the service
do, and they ask this module only what is linked to what.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from .config import LINK_TTL


def code_hash(code: str) -> str:
    """The stored form of a link code: upper-cased, trimmed, SHA-256."""
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def _forget_chat(db: sqlite3.Connection, chat_id: int) -> None:
    """Everything the channel keeps for one chat: its conversation and the rows
    addressed to it. Called inside a transaction, so a chat never changes hands
    while part of its old state is still there."""
    db.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
    db.execute("DELETE FROM approvals WHERE chat_id=?", (chat_id,))
    db.execute("DELETE FROM reminders WHERE chat_id=?", (chat_id,))


@dataclass(frozen=True)
class Link:
    user_id: str
    chat_id: int
    thread_id: int
    #: The Telegram id of the person in the chat; a private chat's id, and the
    #: only sender the channel accepts a message or a button from.
    tg_user_id: int
    username: str
    linked_at: float


@dataclass(frozen=True)
class Reminder:
    id: str
    user_id: str
    chat_id: int
    thread_id: int
    text: str
    due_at: float
    repeat_seconds: float
    created_at: float


@dataclass(frozen=True)
class Approval:
    approval_id: str
    user_id: str
    chat_id: int
    thread_id: int
    context_id: str
    message_id: int | None
    created_at: float


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


class TelegramStore:
    """Links, single-use codes, chat conversations, approvals and reminders."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS links (
                user_id   TEXT PRIMARY KEY,
                chat_id   INTEGER NOT NULL,
                thread_id INTEGER NOT NULL DEFAULT 0,
                tg_user_id INTEGER NOT NULL DEFAULT 0,
                username  TEXT NOT NULL DEFAULT '',
                linked_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS links_by_chat ON links (chat_id, thread_id);
            CREATE TABLE IF NOT EXISTS codes (
                code_hash  TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (
                chat_id    INTEGER NOT NULL,
                thread_id  INTEGER NOT NULL DEFAULT 0,
                user_id    TEXT NOT NULL,
                context_id TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (chat_id, thread_id)
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                chat_id     INTEGER NOT NULL,
                thread_id   INTEGER NOT NULL DEFAULT 0,
                context_id  TEXT NOT NULL,
                message_id  INTEGER,
                created_at  REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id             TEXT PRIMARY KEY,
                user_id        TEXT NOT NULL,
                chat_id        INTEGER NOT NULL,
                thread_id      INTEGER NOT NULL DEFAULT 0,
                text           TEXT NOT NULL,
                due_at         REAL NOT NULL,
                repeat_seconds REAL NOT NULL DEFAULT 0,
                created_at     REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS code_attempts (
                chat_id  INTEGER PRIMARY KEY,
                count    INTEGER NOT NULL,
                reset_at REAL NOT NULL
            );
            """
        )
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database made by an earlier build up to the current schema.

        ``links.tg_user_id`` was added after the channel shipped: the first
        version linked any chat and stored only the chat id. A private chat's
        id is its user's id, so that is the honest backfill; a chat that was
        really a group keeps a negative id there and can never match a real
        sender, which is what should happen to it.
        """
        columns = {str(row["name"]) for row in self.db.execute("PRAGMA table_info(links)")}
        if "tg_user_id" not in columns:
            with self._transaction() as db:
                db.execute("ALTER TABLE links ADD COLUMN tg_user_id INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE links SET tg_user_id=chat_id WHERE tg_user_id=0")
        self.db.commit()
    # -- lifecycle -----------------------------------------------------------
    @locked
    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    # -- link codes ----------------------------------------------------------
    @locked
    def create_code(self, user_id: str, now: float | None = None, ttl: float = LINK_TTL) -> dict[str, Any]:
        """A fresh single-use code; any earlier code of this user stops working."""
        now = time.time() if now is None else now
        code = secrets.token_hex(4).upper()
        expires_at = now + float(ttl)
        with self._transaction() as db:
            db.execute("DELETE FROM codes WHERE user_id=?", (user_id,))
            db.execute(
                "INSERT INTO codes (code_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (code_hash(code), user_id, now, expires_at),
            )
        return {"code": code, "expires_at": expires_at, "ttl": float(ttl)}

    @locked
    def redeem_code(self, code: str, chat_id: int, thread_id: int, now: float | None = None,
                    username: str = "", tg_user_id: int | None = None) -> str | None:
        """Spend *code* for this chat; the user id it linked, or None.

        A code is deleted as it is spent, so a replay links nobody.

        Only a private chat can be linked, where the chat id *is* the user's
        Telegram id: a code sent to a group would otherwise let every member
        act as the user who redeemed it. The ids are compared here, in the
        store, so no caller can link a group by forgetting to check.
        """
        now = time.time() if now is None else now
        tg_user_id = int(chat_id) if tg_user_id is None else int(tg_user_id)
        if tg_user_id != int(chat_id):
            raise ValueError("only a private chat can be linked: chat id must be the user's id")
        digest = code_hash(code)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM codes WHERE code_hash=?", (digest,)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM codes WHERE code_hash=?", (digest,))
            if float(row["expires_at"]) <= now:
                return None
            user_id = str(row["user_id"])
            previous = db.execute("SELECT chat_id FROM links WHERE user_id=?", (user_id,)).fetchone()
            owner = db.execute("SELECT user_id FROM links WHERE chat_id=? AND thread_id=?",
                               (chat_id, thread_id)).fetchone()
            db.execute("DELETE FROM links WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM links WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))
            # A chat belongs to one user at a time: the chat the user left and
            # the chat joining now both lose their conversation, reminders and
            # waiting approvals, so nothing of a previous owner survives.
            if previous is not None and int(previous["chat_id"]) != int(chat_id):
                _forget_chat(db, int(previous["chat_id"]))
            if owner is not None and str(owner["user_id"]) != user_id:
                _forget_chat(db, int(chat_id))
            db.execute(
                "INSERT INTO links (user_id, chat_id, thread_id, tg_user_id, username, linked_at) "
                "VALUES (?,?,?,?,?,?)",
                (user_id, chat_id, thread_id, tg_user_id, username[:64], now),
            )
            db.execute("DELETE FROM code_attempts WHERE chat_id=?", (int(chat_id),))
        return user_id

    @locked
    def link(self, user_id: str) -> Link | None:
        row = self.db.execute("SELECT * FROM links WHERE user_id=?", (user_id,)).fetchone()
        return _link(row)

    @locked
    def user_for_chat(self, chat_id: int, thread_id: int = 0) -> str | None:
        """Who owns this chat; a private chat has one link whatever its thread."""
        row = self.db.execute(
            "SELECT user_id FROM links WHERE chat_id=? ORDER BY (thread_id=?) DESC LIMIT 1",
            (chat_id, int(thread_id or 0)),
        ).fetchone()
        return str(row["user_id"]) if row else None

    @locked
    def unlink(self, user_id: str) -> bool:
        """Forget the user's chat: its conversation mapping, approvals and reminders."""
        with self._transaction() as db:
            row = db.execute("SELECT chat_id FROM links WHERE user_id=?", (user_id,)).fetchone()
            removed = db.execute("DELETE FROM links WHERE user_id=?", (user_id,)).rowcount
            db.execute("DELETE FROM chats WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM approvals WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM reminders WHERE user_id=?", (user_id,))
            if row is not None:
                _forget_chat(db, int(row["chat_id"]))
        return bool(removed)

    # -- link code attempts --------------------------------------------------
    @locked
    def spend_code_attempt(self, chat_id: int, now: float, *, limit: int, window: float) -> bool:
        """Count one code attempt from this chat; False once its window is spent.

        The count lives in the database rather than in the channel's memory, so
        a restart does not hand a guesser a fresh allowance.
        """
        with self._transaction() as db:
            row = db.execute("SELECT * FROM code_attempts WHERE chat_id=?", (int(chat_id),)).fetchone()
            if row is None or float(row["reset_at"]) <= now:
                db.execute(
                    "INSERT OR REPLACE INTO code_attempts (chat_id, count, reset_at) VALUES (?,?,?)",
                    (int(chat_id), 1, float(now) + float(window)),
                )
                return True
            if int(row["count"]) >= int(limit):
                return False
            db.execute("UPDATE code_attempts SET count=count+1 WHERE chat_id=?", (int(chat_id),))
            return True

    @locked
    def forget_code_attempts(self, chat_id: int) -> None:
        """A successful link starts the chat's allowance afresh."""
        with self._transaction() as db:
            db.execute("DELETE FROM code_attempts WHERE chat_id=?", (int(chat_id),))

    # -- chat conversations --------------------------------------------------
    @locked
    def context(self, chat_id: int, thread_id: int = 0) -> str | None:
        row = self.db.execute(
            "SELECT context_id FROM chats WHERE chat_id=? AND thread_id=?",
            (chat_id, int(thread_id or 0)),
        ).fetchone()
        return str(row["context_id"]) if row else None

    @locked
    def set_context(self, chat_id: int, thread_id: int, user_id: str, context_id: str,
                    now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO chats (chat_id, thread_id, user_id, context_id, updated_at) "
                "VALUES (?,?,?,?,?)",
                (chat_id, int(thread_id or 0), user_id, context_id, now),
            )

    @locked
    def chats(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM chats WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
        return [dict(row) for row in rows]

    # -- long-poll cursor ----------------------------------------------------
    @locked
    def offset(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='update_offset'").fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    @locked
    def set_offset(self, offset: int) -> None:
        with self._transaction() as db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('update_offset', ?)", (str(int(offset)),))

    # -- approvals -----------------------------------------------------------
    @locked
    def announce_approval(self, approval_id: str, user_id: str, chat_id: int, thread_id: int,
                          context_id: str, now: float | None = None) -> bool:
        """Remember a pending call once; False when it was announced already."""
        now = time.time() if now is None else now
        with self._transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO approvals "
                "(approval_id, user_id, chat_id, thread_id, context_id, message_id, created_at) "
                "VALUES (?,?,?,?,?,NULL,?)",
                (approval_id, user_id, chat_id, int(thread_id or 0), context_id, now),
            )
            return cursor.rowcount == 1

    @locked
    def set_approval_message(self, approval_id: str, message_id: int) -> None:
        with self._transaction() as db:
            db.execute("UPDATE approvals SET message_id=? WHERE approval_id=?", (int(message_id), approval_id))

    @locked
    def approval(self, approval_id: str) -> Approval | None:
        row = self.db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            return None
        return Approval(
            str(row["approval_id"]), str(row["user_id"]), int(row["chat_id"]), int(row["thread_id"]),
            str(row["context_id"]), row["message_id"], float(row["created_at"]),
        )

    @locked
    def approvals(self, user_id: str) -> list[Approval]:
        rows = self.db.execute("SELECT * FROM approvals WHERE user_id=?", (user_id,))
        return [Approval(str(r["approval_id"]), str(r["user_id"]), int(r["chat_id"]), int(r["thread_id"]),
                         str(r["context_id"]), r["message_id"], float(r["created_at"])) for r in rows]

    @locked
    def forget_approval(self, approval_id: str) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM approvals WHERE approval_id=?", (approval_id,))

    @locked
    def forget_context_approvals(self, context_id: str) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM approvals WHERE context_id=?", (context_id,))

    # -- reminders -----------------------------------------------------------
    @locked
    def add_reminder(self, user_id: str, chat_id: int, thread_id: int, text: str,
                     due_at: float, repeat_seconds: float = 0.0,
                     now: float | None = None) -> Reminder:
        now = time.time() if now is None else now
        reminder = Reminder(uuid.uuid4().hex[:12], user_id, int(chat_id), int(thread_id or 0),
                            text.strip(), float(due_at), float(repeat_seconds or 0), now)
        with self._transaction() as db:
            db.execute(
                "INSERT INTO reminders (id, user_id, chat_id, thread_id, text, due_at, repeat_seconds, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (reminder.id, reminder.user_id, reminder.chat_id, reminder.thread_id,
                 reminder.text, reminder.due_at, reminder.repeat_seconds, reminder.created_at),
            )
        return reminder

    @locked
    def reminders(self, user_id: str) -> list[Reminder]:
        rows = self.db.execute("SELECT * FROM reminders WHERE user_id=? ORDER BY due_at", (user_id,))
        return [_reminder(row) for row in rows]

    @locked
    def reminder(self, reminder_id: str) -> Reminder | None:
        row = self.db.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        return _reminder(row) if row else None

    @locked
    def due_reminders(self, now: float) -> list[Reminder]:
        rows = self.db.execute("SELECT * FROM reminders WHERE due_at<=? ORDER BY due_at", (now,))
        return [_reminder(row) for row in rows]

    @locked
    def finish_reminder(self, reminder_id: str, next_due: float | None) -> None:
        """Delete a one-off reminder, or move a repeating one to its next time."""
        with self._transaction() as db:
            if next_due is None:
                db.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
            else:
                db.execute("UPDATE reminders SET due_at=? WHERE id=?", (float(next_due), reminder_id))

    @locked
    def cancel_reminder(self, user_id: str, reminder_id: str) -> bool:
        with self._transaction() as db:
            removed = db.execute(
                "DELETE FROM reminders WHERE user_id=? AND id=?", (user_id, reminder_id)
            ).rowcount
        return bool(removed)

    @locked
    def cancel_all_reminders(self, user_id: str) -> int:
        with self._transaction() as db:
            return db.execute("DELETE FROM reminders WHERE user_id=?", (user_id,)).rowcount


def _link(row: sqlite3.Row | None) -> Link | None:
    if row is None:
        return None
    return Link(str(row["user_id"]), int(row["chat_id"]), int(row["thread_id"]),
                int(row["tg_user_id"]), str(row["username"]), float(row["linked_at"]))


def _reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(str(row["id"]), str(row["user_id"]), int(row["chat_id"]), int(row["thread_id"]),
                    str(row["text"]), float(row["due_at"]), float(row["repeat_seconds"]),
                    float(row["created_at"]))
