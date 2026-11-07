"""Where lazy agents keep their state: one SQLite file per server.

Everything a lazy task needs to wake up after a restart lives here, as JSON
rows: the tasks and their steps, each user's settings, the ledger of runs, the
learned rates and prices, and the quota readings the providers sent. Nothing
here runs or plans; it only remembers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterator, List, Optional

from core.lazy.estimate import Calibration
from core.lazy.forecast import UserRates
from core.lazy.model import Run, Settings, Task
from core.quota_telemetry import QuotaReading

logger = logging.getLogger("grid.lazy.store")

#: Readings kept per account; older ones only taught the rates.
READINGS_KEPT = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, finished INTEGER NOT NULL DEFAULT 0,
    -- The version and the caller's key are copied out of the JSON, so a change
    -- can be a conditional write and a creation a unique one (save_task,
    -- save_new_task). The rows carry the truth; the data column repeats it.
    version INTEGER NOT NULL DEFAULT 0, request_id TEXT,
    updated_at REAL NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_user ON tasks (user_id);
CREATE TABLE IF NOT EXISTS settings (user_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, account TEXT NOT NULL, ended_at REAL NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_account ON runs (account, ended_at);
CREATE TABLE IF NOT EXISTS learned (key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS readings (
    account TEXT NOT NULL, observed_at REAL NOT NULL, data TEXT NOT NULL, PRIMARY KEY (account, observed_at)
);
"""


class LazyStore:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _rows(self, sql: str, *args) -> List[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _write(self, sql: str, *args) -> None:
        with self._lock:
            self._db.execute(sql, args)

    def _migrate(self) -> None:
        """Add the columns older stores lack, and copy their values out of the JSON."""
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(tasks)").fetchall()}
        added = [name for name in ("version", "request_id") if name not in columns]
        if "version" in added:
            self._db.execute("ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
        if "request_id" in added:
            self._db.execute("ALTER TABLE tasks ADD COLUMN request_id TEXT")
        if added:
            for task_id, data in self._db.execute("SELECT id, data FROM tasks").fetchall():
                stored = json.loads(data)
                self._db.execute(
                    "UPDATE tasks SET version = ?, request_id = ? WHERE id = ?",
                    (int(stored.get("version") or 0), stored.get("request_id"), task_id),
                )
        try:
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS tasks_request "
                "ON tasks (user_id, request_id) WHERE request_id IS NOT NULL"
            )
        except sqlite3.IntegrityError:
            # A store written before this guard may hold two rows for one key
            # (the race the index closes). Keep it usable: creation still checks
            # the key inside one write transaction (save_new_task).
            logger.warning("lazy store: duplicate request_id rows; the key is not unique for old data")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS tasks_request "
                "ON tasks (user_id, request_id) WHERE request_id IS NOT NULL"
            )

    # -- tasks -------------------------------------------------------------------------
    def tasks(self, user_id: str, *, finished: bool = True) -> List[Task]:
        sql = "SELECT data FROM tasks WHERE user_id = ?" + ("" if finished else " AND finished = 0")
        return sorted((Task.from_dict(json.loads(row[0])) for row in self._rows(sql, user_id)), key=lambda t: t.created_at)

    def task(self, user_id: str, task_id: str) -> Optional[Task]:
        rows = self._rows("SELECT data FROM tasks WHERE id = ? AND user_id = ?", task_id, user_id)
        return Task.from_dict(json.loads(rows[0][0])) if rows else None

    def task_by_request(self, user_id: str, request_id: Optional[str]) -> Optional[Task]:
        """The task *user_id* created with *request_id*, if it is still there.

        The key is the caller's, not the task's: a repeated request finds its
        own task and nothing else, so a retry cannot make a second one.
        """
        if not request_id:
            return None
        rows = self._rows(
            "SELECT data FROM tasks WHERE user_id = ? AND request_id = ? ORDER BY updated_at DESC",
            user_id, request_id,
        )
        return Task.from_dict(json.loads(rows[0][0])) if rows else None

    def save_task(self, task: Task, *, expected_version: Optional[int] = None) -> bool:
        """Persist *task*, moving its version on; False when it moved since.

        Every saved change bumps ``Task.version``. With *expected_version* the
        check and the write are one SQLite transaction (``BEGIN IMMEDIATE``), so
        of two writers naming the same version only one is written: a bare
        increment would let both through and lose the first change.

        The transaction, not a lock of this object, is what holds the promise:
        SQLite's write lock is the file's, so the compare-and-swap also holds
        between processes - a second server on the same store, a maintenance
        script - not only between the threads of one server.
        """
        with self._lock:
            self._begin()
            try:
                row = self._db.execute(
                    "SELECT version FROM tasks WHERE id = ? AND user_id = ?", (task.id, task.user_id)
                ).fetchone()
                stored = int(row[0]) if row else None
                if expected_version is not None:
                    if stored is None or int(expected_version) != stored:
                        self._rollback()
                        return False
                    task.version = stored + 1
                else:
                    task.version = (stored or 0) + 1
                data = json.dumps(task.to_dict(), ensure_ascii=False)
                if row is None:
                    self._db.execute(
                        "INSERT INTO tasks (id, user_id, finished, version, request_id, updated_at, data) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (task.id, task.user_id, int(task.finished), task.version, task.request_id,
                         task.updated_at, data),
                    )
                else:
                    changed = self._db.execute(
                        "UPDATE tasks SET finished = ?, version = ?, request_id = ?, updated_at = ?, data = ? "
                        "WHERE id = ? AND user_id = ? AND version = ?",
                        (int(task.finished), task.version, task.request_id, task.updated_at, data,
                         task.id, task.user_id, stored),
                    ).rowcount
                    if changed != 1:  # the row left between the read and the write
                        self._rollback()
                        return False
                self._db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
        return True

    def save_new_task(self, task: Task) -> bool:
        """Insert *task* as a new task; False when its key is already taken.

        One write transaction and one unique key per (user, ``request_id``) make
        creation idempotent across processes: of two servers (or two threads of
        one) creating with one key, exactly one inserts; the other gets False and
        reads the task the winner made, unchanged (``LazyAgents.create_or_get``).
        Without a ``request_id`` only the task's own random id could collide.
        """
        with self._lock:
            self._begin()
            try:
                if task.request_id:
                    taken = self._db.execute(
                        "SELECT 1 FROM tasks WHERE user_id = ? AND request_id = ?",
                        (task.user_id, task.request_id),
                    ).fetchone()
                    if taken is not None:
                        self._rollback()
                        return False
                if self._db.execute("SELECT 1 FROM tasks WHERE id = ?", (task.id,)).fetchone() is not None:
                    self._rollback()
                    return False
                task.version = 1
                self._db.execute(
                    "INSERT INTO tasks (id, user_id, finished, version, request_id, updated_at, data) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (task.id, task.user_id, int(task.finished), task.version, task.request_id,
                     task.updated_at, json.dumps(task.to_dict(), ensure_ascii=False)),
                )
                self._db.execute("COMMIT")
            except sqlite3.IntegrityError:
                self._rollback()
                return False
            except BaseException:
                self._rollback()
                raise
        return True

    def _begin(self) -> None:
        """Take SQLite's write lock; other writers - other processes too - wait."""
        self._db.execute("BEGIN IMMEDIATE")

    def _rollback(self) -> None:
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")

    def delete_task(self, user_id: str, task_id: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)).rowcount > 0

    def users(self) -> List[str]:
        """The users with an unfinished task."""
        return [row[0] for row in self._rows("SELECT DISTINCT user_id FROM tasks WHERE finished = 0")]

    # -- settings ----------------------------------------------------------------------
    def settings_users(self) -> List[str]:
        return [row[0] for row in self._rows("SELECT user_id FROM settings")]

    def settings(self, user_id: str) -> Settings:
        rows = self._rows("SELECT data FROM settings WHERE user_id = ?", user_id)
        return Settings.from_dict(json.loads(rows[0][0])) if rows else Settings()

    def has_settings(self, user_id: str) -> bool:
        return bool(self._rows("SELECT 1 FROM settings WHERE user_id = ?", user_id))

    def save_settings(self, user_id: str, settings: Settings) -> None:
        self._write(
            "INSERT INTO settings (user_id, data) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
            user_id, json.dumps(settings.to_dict(), ensure_ascii=False),
        )

    # -- the ledger --------------------------------------------------------------------
    def add_run(self, run: Run) -> None:
        self._write(
            "INSERT OR REPLACE INTO runs (id, user_id, account, ended_at, data) VALUES (?, ?, ?, ?, ?)",
            run.id, run.user_id, run.account, run.ended_at, json.dumps(run.to_dict()),
        )

    def runs(self, account: str, since: float) -> List[Run]:
        rows = self._rows("SELECT data FROM runs WHERE account = ? AND ended_at >= ? ORDER BY ended_at", account, since)
        return [Run.from_dict(json.loads(row[0])) for row in rows]

    # -- what was learned --------------------------------------------------------------
    def rates(self, user_id: str) -> UserRates:
        rows = self._rows("SELECT data FROM learned WHERE key = ?", f"rates:{user_id}")
        return UserRates.from_dict(json.loads(rows[0][0])) if rows else UserRates()

    def save_rates(self, user_id: str, rates: UserRates) -> None:
        self._save_learned(f"rates:{user_id}", rates.to_dict())

    def calibration(self, account: str) -> Calibration:
        rows = self._rows("SELECT data FROM learned WHERE key = ?", f"price:{account}")
        return Calibration.from_dict(json.loads(rows[0][0])) if rows else Calibration()

    def save_calibration(self, account: str, calibration: Calibration) -> None:
        self._save_learned(f"price:{account}", calibration.to_dict())

    def _save_learned(self, key: str, data: dict) -> None:
        self._write(
            "INSERT INTO learned (key, data) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET data = excluded.data",
            key, json.dumps(data),
        )

    # -- quota readings ----------------------------------------------------------------
    def add_reading(self, reading: QuotaReading) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO readings (account, observed_at, data) VALUES (?, ?, ?)",
                (reading.account, reading.observed_at, json.dumps(reading.to_dict())),
            )
            self._db.execute(
                "DELETE FROM readings WHERE account = ? AND observed_at < "
                "(SELECT observed_at FROM readings WHERE account = ? ORDER BY observed_at DESC LIMIT 1 OFFSET ?)",
                (reading.account, reading.account, READINGS_KEPT - 1),
            )

    def latest_reading(self, account: str) -> Optional[QuotaReading]:
        rows = self._rows("SELECT data FROM readings WHERE account = ? ORDER BY observed_at DESC LIMIT 1", account)
        return QuotaReading.from_dict(json.loads(rows[0][0])) if rows else None

    def readings(self, account: str, since: float) -> Iterator[QuotaReading]:
        rows = self._rows("SELECT data FROM readings WHERE account = ? AND observed_at >= ? ORDER BY observed_at", account, since)
        return (QuotaReading.from_dict(json.loads(row[0])) for row in rows)
