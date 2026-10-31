"""Opt-in daily alignment reports. No agent, model, tool registry or write access.

Only this module's SQLite ledger is written. Reports and episode fingerprints commit
in one transaction; a report is not a chat and survives chat deletion. The deliberately
small rule engine reports signals, not inferred satisfaction or general intelligence.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 4
BACKOFF = 900
RULE_VERSION = 1


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    hour_utc: int = 3
    max_records: int = 200
    max_chars: int = 64000
    max_record_chars: int = 8000

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        for name, low, high in (("hour_utc", 0, 23), ("max_records", 1, 2000),
                                ("max_chars", 1000, 1000000), ("max_record_chars", 100, 262144)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in {low}..{high}")


@dataclass(frozen=True)
class Episode:
    key: str
    fingerprint: str
    kind: str
    source_id: str
    index: int | None
    timestamp: str
    text: str
    role: str = ""


def episode(kind: str, source_id: str, index: int | None, timestamp: str,
            text: str, role: str = "") -> Episode:
    key = encoded([kind, source_id, index])
    digest = hashlib.sha256(encoded([RULE_VERSION, timestamp, text, role]).encode()).hexdigest()
    return Episode(key, digest, kind, source_id, index, timestamp, text, role)


def collect(space: Any) -> tuple[list[Episode], dict]:
    """Read exactly one already-authorized UserSpace. Never enumerate accounts/files.

    conversation_views copies metadata and message references, not agent histories.
    Non-text/multimodal content is explicitly left unprocessed.
    """
    records: list[Episode] = []
    coverage: dict = {"conversations": 0, "projects": 0, "excluded_reports": 0,
                      "unsupported_messages": 0, "excluded_non_dialogue_messages": 0,
                      "errors": []}
    # Failure of the inventory is fatal: do not claim an empty successful analysis.
    try:
        manager = space.context_manager()
        views = manager.conversation_views()
    except Exception:  # noqa: BLE001 - failed inventory must remain unprocessed
        coverage["errors"].append({"source": "conversations", "code": "inventory_read_failed"})
        return records, coverage
    for view in sorted(views, key=lambda v: v["id"]):
        cid = view["id"]
        if (view.get("metadata") or {}).get("daily_digest"):
            coverage["excluded_reports"] += 1
            continue
        coverage["conversations"] += 1
        try:
            for index, message in enumerate(view["messages"]):
                def field(name: str, default: Any = "", item: Any = message) -> Any:
                    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)

                role, content = field("role"), field("content")
                if role not in ("user", "assistant"):
                    coverage["excluded_non_dialogue_messages"] += 1
                    continue
                if not isinstance(content, str):
                    coverage["unsupported_messages"] += 1
                    continue
                stamp = field("timestamp")
                stamp = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp or "")
                records.append(episode("message", cid, index, stamp, content, role))
        except Exception:  # noqa: BLE001 - record failure, never leak payloads
            coverage["errors"].append({"source": cid, "code": "conversation_read_failed"})
    try:
        # Real UserSpace supplies a strict, side-effect-free disk snapshot. Keep
        # the list-only adapter contract for existing embedded/test spaces.
        snapshot = getattr(space, "project_snapshot", None)
        projects = snapshot() if snapshot is not None else space.project_store.list()
        for project in sorted(projects, key=lambda p: p.id):
            coverage["projects"] += 1
            records.append(episode("project", project.id, None, str(project.updated_at),
                                   encoded(project.to_dict())))
    except Exception:  # noqa: BLE001 - retain projects as unprocessed
        coverage["errors"].append({"source": "projects", "code": "project_read_failed"})
    return records, coverage


# Signals are intentionally narrow; quoted user content is data, never instructions.
CORRECTION = re.compile(r"\b(?:incorrect|wrong|instead|исправь|неверно|ошибка|вместо)\b", re.IGNORECASE)


def analyze(records: list[Episode]) -> list[dict]:
    """Pure, capability-free analysis of immutable, bounded text episodes."""
    findings = []
    for record in records:
        reference = {"kind": record.kind, "id": record.source_id,
                     "message_index": record.index, "timestamp": record.timestamp,
                     "fingerprint": record.fingerprint}
        if record.kind == "message" and record.role == "user":
            match = CORRECTION.search(record.text)
            if match:
                start = max(0, match.start() - 80)
                findings.append({"observation": "User text contains an explicit correction signal.",
                                 "hypothesis": "This may indicate a missed requirement; a keyword alone does not establish an agent error.",
                                 "recommendation": "Review this episode and confirm the corrected requirement with the user before changing anything.",
                                 "evidence": {**reference, "quote": record.text[start:start + 320]}})
        elif record.kind == "project":
            project = json.loads(record.text)
            if not project.get("archived") and not project.get("description", "").strip():
                findings.append({"observation": "Active project has an empty description.",
                                 "hypothesis": "Its goal may be documented elsewhere; missing description is not evidence of dissatisfaction.",
                                 "recommendation": "Ask whether a short project goal and completion criterion would help; do not edit automatically.",
                                 "evidence": {**reference, "quote": "description is empty", "title": project.get("title", "")}})
    return findings


class CollectionError(RuntimeError):
    def __init__(self, coverage: dict) -> None:
        super().__init__("source_read_failed")
        self.coverage = coverage


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class DigestStore:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=1, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS digest_settings(user_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS digest_seen(user_id TEXT, episode_key TEXT, fingerprint TEXT NOT NULL,
                PRIMARY KEY(user_id, episode_key));
            CREATE TABLE IF NOT EXISTS digest_pending(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                episode_key TEXT NOT NULL, UNIQUE(user_id, episode_key));
            CREATE TABLE IF NOT EXISTS digest_runs(user_id TEXT, day TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, error TEXT, report TEXT, diagnostics TEXT,
                PRIMARY KEY(user_id, day));
        """)
        if "diagnostics" not in {row[1] for row in self.db.execute("PRAGMA table_info(digest_runs)")}:
            self.db.execute("ALTER TABLE digest_runs ADD COLUMN diagnostics TEXT")

    @locked
    def close(self) -> None:
        self.db.close()

    @locked
    def settings(self, user_id: str) -> Settings:
        row = self.db.execute("SELECT body FROM digest_settings WHERE user_id=?", (user_id,)).fetchone()
        return Settings(**json.loads(row[0])) if row else Settings()

    @locked
    def configure(self, user_id: str, settings: Settings) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO digest_settings VALUES (?,?)", (user_id, encoded(asdict(settings))))

    @locked
    def users(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT user_id FROM digest_settings")
                if self.settings(row[0]).enabled]

    @locked
    def reports(self, user_id: str) -> list[dict]:
        rows = self.db.execute("SELECT day,attempts,next_try,error,report,diagnostics FROM digest_runs WHERE user_id=? ORDER BY day DESC LIMIT 31", (user_id,))
        return [{**dict(row), "report": json.loads(row["report"]) if row["report"] else None,
                 "diagnostics": json.loads(row["diagnostics"]) if row["diagnostics"] else None} for row in rows]

    @locked
    def readiness(self, user_id: str, now: float) -> dict | None:
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        row = self.db.execute("SELECT * FROM digest_runs WHERE user_id=? AND day=?", (user_id, day)).fetchone()
        if row and row["report"]:
            return {"status": "already_done", "report": json.loads(row["report"])}
        if row and (row["attempts"] >= MAX_ATTEMPTS or row["next_try"] > now):
            return {"status": "backoff", "attempts": row["attempts"], "next_try": row["next_try"]}
        return None

    @locked
    def unavailable(self, user_id: str, now: float) -> dict:
        """Persist bounded retries even when SpacePool cannot build the user's space."""
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            ready = self.readiness(user_id, now)
            if ready:
                self.db.commit()
                return ready
            self.db.execute("INSERT OR IGNORE INTO digest_runs(user_id,day) VALUES (?,?)", (user_id, day))
            self.db.execute("UPDATE digest_runs SET attempts=attempts+1,next_try=? + ?*(attempts+1),error=? WHERE user_id=? AND day=?",
                            (now, BACKOFF, "space_unavailable", user_id, day))
            self.db.commit()
            return {"status": "unavailable"}
        except BaseException:
            self.db.rollback()
            raise

    @locked
    def run(self, user_id: str, space: Any, now: float) -> dict:
        """Synchronous transaction: no awaits, concurrent workers serialize in SQLite.

        Failure attempt commits, but report and progress never commit separately.
        A process crash rolls back all work, so the same day is safe to retry.
        """
        if space.user_id != user_id:
            raise ValueError("UserSpace identity mismatch")
        settings = self.settings(user_id)
        if not settings.enabled:
            return {"status": "disabled"}
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT OR IGNORE INTO digest_runs(user_id,day) VALUES (?,?)", (user_id, day))
            row = self.db.execute("SELECT * FROM digest_runs WHERE user_id=? AND day=?", (user_id, day)).fetchone()
            if row["report"]:
                result = {"status": "already_done", "report": json.loads(row["report"])}
            elif row["attempts"] >= MAX_ATTEMPTS or row["next_try"] > now:
                result = {"status": "backoff", "attempts": row["attempts"], "next_try": row["next_try"]}
            else:
                # SAVEPOINT protects against partial progress even if publication fails.
                self.db.execute("SAVEPOINT analysis")
                try:
                    records, coverage = collect(space)
                    if coverage["errors"]:
                        raise CollectionError(coverage)
                    seen = dict(self.db.execute("SELECT episode_key,fingerprint FROM digest_seen WHERE user_id=?", (user_id,)))
                    changed = [r for r in records if seen.get(r.key) != r.fingerprint]
                    # Persistent FIFO of first-observed pending keys. New traffic
                    # cannot jump ahead of yesterday's backlog, even on restart
                    # or when a waiting episode is edited. Queue changes share
                    # the report/seen savepoint, including publication rollback.
                    waiting = dict(self.db.execute(
                        "SELECT episode_key,sequence FROM digest_pending WHERE user_id=?", (user_id,)))
                    changed_keys = {r.key for r in changed}
                    self.db.executemany(
                        "DELETE FROM digest_pending WHERE user_id=? AND episode_key=?",
                        [(user_id, key) for key in waiting if key not in changed_keys])
                    self.db.executemany(
                        "INSERT OR IGNORE INTO digest_pending(user_id,episode_key) VALUES (?,?)",
                        [(user_id, r.key) for r in changed if r.key not in waiting])
                    order = dict(self.db.execute(
                        "SELECT episode_key,sequence FROM digest_pending WHERE user_id=?", (user_id,)))
                    changed.sort(key=lambda r: order[r.key])
                    selected: list[Episode] = []
                    chars = 0
                    oversized = 0
                    for record in changed:
                        size = len(record.text)
                        if size > settings.max_record_chars:
                            oversized += 1
                            continue
                        if len(selected) >= settings.max_records or chars + size > settings.max_chars:
                            continue
                        selected.append(record)
                        chars += size
                    findings = analyze(selected)
                    coverage.update({"available_text_records": len(records), "unchanged": len(records) - len(changed),
                                     "processed": len(selected), "pending": len(changed) - len(selected),
                                     "oversized_pending": oversized, "processed_chars": chars})
                    report = {"id": day, "user_id": user_id, "created_at": now, "rule_version": RULE_VERSION,
                              "status": "partial" if coverage["pending"] or coverage["errors"] or coverage["unsupported_messages"] else "complete",
                              "coverage": coverage, "findings": findings,
                              "limits": asdict(settings),
                              "scope": "Text-only correction signals and project-description checks; no plan board, files, tools, or semantic satisfaction inference.",
                              "notice": "No corrections found does not mean satisfaction. Recommendations require human review. Nothing was changed."}
                    for record in selected:
                        self.db.execute("INSERT OR REPLACE INTO digest_seen VALUES (?,?,?)", (user_id, record.key, record.fingerprint))
                        self.db.execute("DELETE FROM digest_pending WHERE user_id=? AND episode_key=?",
                                        (user_id, record.key))
                    self.db.execute("UPDATE digest_runs SET report=?,error=NULL,diagnostics=NULL,next_try=0,attempts=attempts+1 WHERE user_id=? AND day=?",
                                    (encoded(report), user_id, day))
                    result = {"status": "done", "report": report}
                    self.db.execute("RELEASE analysis")
                except Exception as exc:  # noqa: BLE001 - rollback the entire publication
                    self.db.execute("ROLLBACK TO analysis")
                    self.db.execute("RELEASE analysis")
                    # Do not persist exception text: it may contain private payloads.
                    attempts = row["attempts"] + 1
                    diagnostics = encoded(exc.coverage) if isinstance(exc, CollectionError) else None
                    self.db.execute("UPDATE digest_runs SET attempts=?,next_try=?,error=?,diagnostics=? WHERE user_id=? AND day=?",
                                    (attempts, now + BACKOFF * attempts, "analysis_failed", diagnostics, user_id, day))
                    result = {"status": "failed", "attempts": attempts, "error": "analysis_failed"}
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise


class DailyDigest:
    def __init__(self, store: DigestStore, spaces: Any, *, allowed=lambda user_id: True) -> None:
        self.store, self.spaces, self.allowed = store, spaces, allowed
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    async def run(self, user_id: str, now: float | None = None) -> dict:
        if not self.allowed(user_id):
            return {"status": "unavailable"}
        # Check opt-in before acquiring a space: disabled users are not even read.
        if not self.store.settings(user_id).enabled:
            return {"status": "disabled"}
        now = time.time() if now is None else now
        async with self.lock:
            ready = self.store.readiness(user_id, now)
            if ready:
                return ready
            try:
                async with self.spaces.use(user_id) as space:
                    return self.store.run(user_id, space, now)
            except Exception:  # noqa: BLE001 - bounded retry without exposing payloads
                # Space acquisition failures are transient and must not kill the loop.
                # They cannot advance a watermark; retry budget survives restarts.
                log.warning("Daily digest space or ledger unavailable")
                return self.store.unavailable(user_id, now)

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        hour = datetime.fromtimestamp(now, UTC).hour
        for user_id in self.store.users():
            if hour >= self.store.settings(user_id).hour_utc:
                await self.run(user_id, now)

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="daily-digest")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - upkeep must survive one bad tick
                log.warning("Daily digest tick failed")
            await asyncio.sleep(60)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.store.close()


def register_digest_routes(api: Any, current_user: Any, service: DailyDigest) -> None:
    from fastapi import Depends, HTTPException

    identified = Depends(current_user)

    @api.get("/api/daily-digest")
    async def state(user=identified):
        return {"settings": asdict(service.store.settings(user.id)), "runs": service.store.reports(user.id)}

    @api.put("/api/daily-digest/settings")
    async def configure(body: dict, user=identified):
        try:
            settings = Settings(**body)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None
        service.store.configure(user.id, settings)
        return asdict(settings)

    @api.post("/api/daily-digest/run")
    async def run(user=identified):
        return await service.run(user.id)
