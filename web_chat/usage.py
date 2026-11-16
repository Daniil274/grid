"""Token analytics from reported model responses, without storing prompt text."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

UNKNOWN_MODEL = "Unknown model"
FIELDS = ("tokens_in", "tokens_out", "cached_in", "reasoning_out", "responses")
#: Money columns, summed beside FIELDS: what the calls were worth, and the part
#: the operator paid (calls on a user's own key or a subscription are not charged).
COST_FIELDS = {
    "cost_micro": "cost_micro",
    "charged_micro": "CASE WHEN charged = 1 THEN cost_micro ELSE 0 END",
}
#: Columns added after the first release; an older database gains them on open.
ADDED_COLUMNS = (
    ("cost_micro", "INTEGER NOT NULL DEFAULT 0"),
    ("cost_basis", "TEXT NOT NULL DEFAULT ''"),
    ("credential_source", "TEXT NOT NULL DEFAULT ''"),
    ("charged", "INTEGER NOT NULL DEFAULT 1"),
)


def counters(row: dict) -> dict:
    values = {key: int(row.get(key) or 0) for key in FIELDS}
    values["total_tokens"] = values["tokens_in"] + values["tokens_out"]
    for key in COST_FIELDS:
        values[key] = int(row.get(key) or 0)
        values[key.replace("_micro", "_usd")] = values[key] / 1_000_000
    return values


class UsageStore:
    def __init__(self, path: Path | None = None):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS usage_events (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    tokens_in INTEGER NOT NULL CHECK(tokens_in >= 0),
                    tokens_out INTEGER NOT NULL CHECK(tokens_out >= 0),
                    cached_in INTEGER NOT NULL CHECK(cached_in >= 0),
                    reasoning_out INTEGER NOT NULL CHECK(reasoning_out >= 0),
                    responses INTEGER NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_by_user_time ON usage_events(user_id, occurred_at);
                CREATE INDEX IF NOT EXISTS usage_by_time ON usage_events(occurred_at);
                CREATE TABLE IF NOT EXISTS usage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            have = {row["name"] for row in self._db.execute("PRAGMA table_info(usage_events)")}
            for name, definition in ADDED_COLUMNS:
                if name not in have:
                    self._db.execute(f"ALTER TABLE usage_events ADD COLUMN {name} {definition}")
            self._db.execute("INSERT OR IGNORE INTO usage_meta VALUES ('detailed_since', ?)", (str(time.time()),))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record(self, *, event_id: str, user_id: str, model: str, provider: str = "",
               tokens_in: int, tokens_out: int, cached_in: int = 0, reasoning_out: int = 0,
               occurred_at: float | None = None, cost_micro: int = 0, cost_basis: str = "",
               credential_source: str = "", charged: bool = True) -> None:
        incoming, outgoing = max(0, int(tokens_in)), max(0, int(tokens_out))
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO usage_events (id, user_id, occurred_at, model, provider, tokens_in, tokens_out, "
                "cached_in, reasoning_out, responses, source, cost_micro, cost_basis, credential_source, charged) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'response', ?, ?, ?, ?)",
                (event_id, user_id, occurred_at if occurred_at is not None else time.time(),
                 model or UNKNOWN_MODEL, provider, incoming, outgoing,
                 min(incoming, max(0, int(cached_in))), min(outgoing, max(0, int(reasoning_out))),
                 max(0, int(cost_micro)), cost_basis, credential_source, int(bool(charged))),
            )

    def spent_micro(self, user_id: str, since: float) -> int:
        """Micro-dollars the operator was charged for the user's calls since *since* (epoch seconds)."""
        with self._lock:
            return int(self._db.execute(
                "SELECT COALESCE(SUM(cost_micro), 0) FROM usage_events WHERE user_id = ? AND occurred_at >= ? AND charged = 1",
                (user_id, since),
            ).fetchone()[0])

    def tokens_since(self, user_id: str, provider: str, since: float) -> tuple[int, float | None]:
        """Tokens (in + out) of the user's calls through *provider* since *since*, and when the first was."""
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(tokens_in + tokens_out), 0), MIN(occurred_at) FROM usage_events "
                "WHERE user_id = ? AND provider = ? AND occurred_at >= ?",
                (user_id, provider, since),
            ).fetchone()
        return int(row[0]), (float(row[1]) if row[1] is not None else None)

    def provider_summary(
        self,
        *,
        provider: str,
        user_id: str | None = None,
        now: float | None = None,
        charged_only: bool = False,
    ) -> dict:
        """Metered usage of *provider* across 5h, today, 7d, month and all time."""
        from web_chat.limits import utc_day_start, utc_month_start

        current = time.time() if now is None else float(now)
        periods = {
            "five_hour": current - 5 * 3600.0,
            "day": utc_day_start(current),
            "week": current - 7 * 86400.0,
            "month": utc_month_start(current),
            "total": 0.0,
        }
        where = ["provider = ?", "source != 'legacy'"]
        base_params: list[object] = [provider]
        if user_id is not None:
            where.append("user_id = ?")
            base_params.append(user_id)
        if charged_only:
            where.append("charged = 1")
        clause = " AND ".join(where)
        sums = ", ".join(
            [f"COALESCE(SUM({field}), 0) AS {field}" for field in FIELDS]
            + [f"COALESCE(SUM({expression}), 0) AS {name}" for name, expression in COST_FIELDS.items()]
            + ["MAX(occurred_at) AS last_at", "MIN(occurred_at) AS first_at"]
        )
        result: dict[str, object] = {"provider": provider}
        with self._lock:
            for label, since in periods.items():
                row = dict(
                    self._db.execute(
                        f"SELECT {sums} FROM usage_events WHERE {clause} AND occurred_at >= ?",
                        [*base_params, since],
                    ).fetchone()
                )
                entry = counters(row)
                entry["first_at"] = float(row["first_at"]) if row["first_at"] is not None else None
                entry["last_at"] = float(row["last_at"]) if row["last_at"] is not None else None
                result[label] = entry
        total = result["total"]
        assert isinstance(total, dict)
        result["last_at"] = total.get("last_at")
        return result

    def import_legacy(self, rows: Iterable[dict]) -> None:
        """One atomic baseline at installation; restarts never import it twice."""
        with self._lock:
            if self._db.execute("SELECT 1 FROM usage_meta WHERE key='legacy_imported'").fetchone():
                return
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    incoming, outgoing = int(row["tokens_in"]), int(row["tokens_out"])
                    if not incoming and not outgoing:
                        continue
                    timestamp = datetime.combine(date.fromisoformat(row["day"]), datetime.min.time(), timezone.utc).timestamp()
                    self._db.execute(
                        "INSERT OR IGNORE INTO usage_events (id, user_id, occurred_at, model, provider, tokens_in, tokens_out, "
                        "cached_in, reasoning_out, responses, source) VALUES (?, ?, ?, ?, '', ?, ?, 0, 0, 0, 'legacy')",
                        (f"legacy:{row['user_id']}:{row['day']}", row["user_id"], timestamp, UNKNOWN_MODEL, incoming, outgoing),
                    )
                self._db.execute("INSERT INTO usage_meta VALUES ('legacy_imported', ?)", (str(time.time()),))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def report(self, *, user_id: str | None, start: date, end: date,
               bucket: str = "auto", model: tuple[str, str] | None = None) -> dict:
        if end < start or (end - start).days > 3659 or end == date.max:
            raise ValueError("Choose a date range of at most ten years, with the end on or after the start.")
        days = (end - start).days + 1
        if bucket == "auto":
            bucket = "hour" if days <= 2 else "day" if days <= 120 else "week"
        seconds = {"hour": 3600, "day": 86400, "week": 604800}.get(bucket)
        if seconds is None:
            raise ValueError("Grouping must be auto, hour, day or week.")
        count = (days * 86400 + seconds - 1) // seconds
        if count > 1000:
            raise ValueError("This range has too many points. Choose daily or weekly grouping.")
        begin = datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()
        finish = datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()
        conditions, params = ["occurred_at >= ?", "occurred_at < ?"], [begin, finish]
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if model is not None:
            conditions.extend(["provider = ?", "model = ?"])
            params.extend(model)
        where = " AND ".join(conditions)
        sums = ", ".join(
            [f"COALESCE(SUM({field}), 0) AS {field}" for field in FIELDS]
            + [f"COALESCE(SUM({expression}), 0) AS {name}" for name, expression in COST_FIELDS.items()]
        )
        with self._lock:
            totals = dict(self._db.execute(f"SELECT {sums} FROM usage_events WHERE {where}", params).fetchone())
            models = [dict(row) for row in self._db.execute(
                f"SELECT model, provider, {sums} FROM usage_events WHERE {where} GROUP BY model, provider "
                "ORDER BY SUM(tokens_in + tokens_out) DESC, model, provider", params,
            )]
            timeline_condition = where + (" AND source != 'legacy'" if bucket == "hour" else "")
            timeline = {int(row["slot"]): dict(row) for row in self._db.execute(
                f"SELECT CAST((occurred_at - ?) / ? AS INTEGER) AS slot, {sums} FROM usage_events "
                f"WHERE {timeline_condition} GROUP BY slot", [begin, seconds, *params],
            )}
            legacy = self._db.execute(
                f"SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM usage_events WHERE {where} AND source='legacy'", params,
            ).fetchone()[0]
            scope_where, scope_params = ("WHERE user_id=?", [user_id]) if user_id is not None else ("", [])
            options = [dict(row) for row in self._db.execute(
                f"SELECT DISTINCT model, provider FROM usage_events {scope_where} ORDER BY model, provider", scope_params,
            )]
            earliest = self._db.execute(f"SELECT MIN(occurred_at) FROM usage_events {scope_where}", scope_params).fetchone()[0]
            detailed_since = float(self._db.execute("SELECT value FROM usage_meta WHERE key='detailed_since'").fetchone()[0])
        return {
            "start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC", "bucket": bucket,
            "summary": counters(totals),
            "models": [{**row, **counters(row)} for row in models],
            "model_options": [{**row, "key": json.dumps([row["provider"], row["model"]])} for row in options],
            "series": [
                {"at": datetime.fromtimestamp(begin + index * seconds, timezone.utc).isoformat(),
                 **counters(timeline.get(index, {}))} for index in range(count)
            ],
            "coverage": {
                "legacy_tokens": int(legacy), "legacy_omitted_from_chart": bucket == "hour" and legacy > 0,
                "detailed_since": datetime.fromtimestamp(detailed_since, timezone.utc).isoformat(),
                "earliest": datetime.fromtimestamp(earliest, timezone.utc).date().isoformat() if earliest is not None else None,
            },
        }
