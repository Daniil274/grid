"""Where lazy agents keep their state: one SQLite file per server.

Everything a lazy task needs to wake up after a restart lives here, as JSON
rows: the tasks and their steps, each user's settings, the ledger of runs, the
learned rates and prices, and the quota readings the providers sent. Nothing
here runs or plans; it only remembers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterator, List, Optional

from core.lazy.estimate import Calibration
from core.lazy.forecast import UserRates
from core.lazy.model import Run, Settings, Task
from core.quota_telemetry import QuotaReading

#: Readings kept per account; older ones only taught the rates.
READINGS_KEPT = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, finished INTEGER NOT NULL DEFAULT 0,
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

    # -- tasks -------------------------------------------------------------------------
    def tasks(self, user_id: str, *, finished: bool = True) -> List[Task]:
        sql = "SELECT data FROM tasks WHERE user_id = ?" + ("" if finished else " AND finished = 0")
        return sorted((Task.from_dict(json.loads(row[0])) for row in self._rows(sql, user_id)), key=lambda t: t.created_at)

    def task(self, user_id: str, task_id: str) -> Optional[Task]:
        rows = self._rows("SELECT data FROM tasks WHERE id = ? AND user_id = ?", task_id, user_id)
        return Task.from_dict(json.loads(rows[0][0])) if rows else None

    def save_task(self, task: Task) -> None:
        self._write(
            "INSERT INTO tasks (id, user_id, finished, updated_at, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET finished = excluded.finished, updated_at = excluded.updated_at, data = excluded.data",
            task.id, task.user_id, int(task.finished), task.updated_at, json.dumps(task.to_dict(), ensure_ascii=False),
        )

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
