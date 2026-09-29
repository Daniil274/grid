"""Durable experiment journal. No runtime state is kept in the candidate."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, experiment TEXT NOT NULL, time TEXT DEFAULT CURRENT_TIMESTAMP, data TEXT NOT NULL)"
            )
            # Tasks the operator's side sends to the workshop (see service.py):
            # open until the workshop takes one, then linked to its experiment.
            db.execute(
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP,"
                " status TEXT NOT NULL CHECK (status IN ('open', 'taken')), experiment TEXT, data TEXT NOT NULL)"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(
        self, experiment: str, data: dict[str, Any], status: str = "running"
    ) -> None:
        if status not in {"queued", "running"}:
            raise ValueError("Invalid initial status")
        with self.connect() as db:
            db.execute(
                "INSERT INTO experiments VALUES (?, ?, ?)",
                (experiment, status, json.dumps(data)),
            )

    def start(self, experiment: str) -> None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE experiments SET status='running' WHERE id=? AND status='queued'",
                (experiment,),
            ).rowcount
            if changed != 1:
                raise ValueError("Experiment is not queued")

    def pending(self, status: str) -> list[str]:
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT id FROM experiments WHERE status=? ORDER BY rowid",
                    (status,),
                )
            ]

    def event(self, experiment: str, data: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO events (experiment, data) VALUES (?, ?)",
                (experiment, json.dumps(data)),
            )

    def finish(self, experiment: str, status: str, result: dict[str, Any]) -> None:
        # completed: a development trial finished; it carries no verdict.
        if status not in {"accepted", "rejected", "failed", "completed"}:
            raise ValueError("Invalid terminal status")
        with self.connect() as db:
            changed = db.execute(
                "UPDATE experiments SET status=? WHERE id=? AND status='running'",
                (status, experiment),
            ).rowcount
            if changed != 1:
                raise ValueError("Experiment missing or already finished")
            db.execute(
                "INSERT INTO events (experiment, data) VALUES (?, ?)",
                (experiment, json.dumps(result)),
            )

    def promote(self, experiment: str, data: dict[str, Any]) -> None:
        """Mark an accepted experiment as the one ``stable`` now points to."""
        with self.connect() as db:
            changed = db.execute(
                "UPDATE experiments SET status='promoted' WHERE id=? AND status='accepted'",
                (experiment,),
            ).rowcount
            if changed != 1:
                raise ValueError("Only an accepted experiment can be promoted")
            db.execute(
                "INSERT INTO events (experiment, data) VALUES (?, ?)",
                (experiment, json.dumps(data)),
            )

    def get(self, experiment: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT status, data FROM experiments WHERE id=?", (experiment,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown experiment")
            events = db.execute(
                "SELECT time, data FROM events WHERE experiment=? ORDER BY sequence",
                (experiment,),
            ).fetchall()
        return {
            "id": experiment,
            "status": row[0],
            **json.loads(row[1]),
            "events": [{"time": stamp, **json.loads(data)} for stamp, data in events],
        }

    # -- tasks ---------------------------------------------------------------------
    def add_task(self, task: str, data: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO tasks (id, status, data) VALUES (?, 'open', ?)", (task, json.dumps(data)))

    def open_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT id, created, data FROM tasks WHERE status='open' ORDER BY rowid").fetchall()
        return [{"id": task, "created": created, **json.loads(data)} for task, created, data in rows]

    def task(self, task: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT created, status, experiment, data FROM tasks WHERE id=?", (task,)).fetchone()
        if row is None:
            raise ValueError("Unknown task")
        created, status, experiment, data = row
        return {"id": task, "created": created, "status": status, "experiment": experiment, **json.loads(data)}

    def take_task(self, task: str) -> None:
        with self.connect() as db:
            if db.execute("UPDATE tasks SET status='taken' WHERE id=? AND status='open'", (task,)).rowcount != 1:
                raise ValueError("The task is not open")

    def link_task(self, task: str, experiment: str) -> None:
        """Record the experiment a taken task became."""
        with self.connect() as db:
            if db.execute(
                "UPDATE tasks SET experiment=? WHERE id=? AND status='taken'", (experiment, task)
            ).rowcount != 1:
                raise ValueError("Only a taken task can be linked to an experiment")

    @contextmanager
    def lease(self, experiment: str) -> Iterator[None]:
        """OS-owned lock: released on process death, never guessed from timestamps."""
        import re

        if not re.fullmatch(r"[a-f0-9]{32}", experiment):
            raise ValueError("Invalid experiment ID")
        lock_path = self.path.parent / (experiment + ".lock")
        with lock_path.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            import os

            if os.name == "nt":
                import msvcrt

                acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                acquire = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                release = lambda: fcntl.flock(handle, fcntl.LOCK_UN)
            try:
                acquire()
            except OSError as error:
                raise RuntimeError(
                    "Experiment is owned by a running controller"
                ) from error
            try:
                yield
            finally:
                handle.seek(0)
                release()
