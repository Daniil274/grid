"""
ExecutionTracer — сохраняет дерево выполнения агентов в SQLite.
Хукается в систему трассировки Agents SDK через TracingExporter.
Поддерживает live-обновления через WebSocket callbacks.

Данные хранятся в data/timeline.db:
  - traces: верхнеуровневые запуски
  - nodes: отдельные шаги (спаны) с parent-child связями
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agents.tracing.processor_interface import TracingExporter
from agents.tracing.traces import Trace
from agents.tracing.spans import Span

logger = logging.getLogger("grid.timeline")

# ─── Путь к БД по умолчанию ─────────────────────────────────────────────────
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "timeline.db"


# ─── DDL ─────────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id            TEXT PRIMARY KEY,
    workflow_name TEXT,
    group_id      TEXT,
    metadata      TEXT,
    status        TEXT DEFAULT 'running',
    started_at    TEXT,
    ended_at      TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS nodes (
    id            TEXT PRIMARY KEY,
    trace_id      TEXT NOT NULL,
    parent_id     TEXT,
    node_type     TEXT,
    name          TEXT,
    input_data    TEXT,
    output_data   TEXT,
    status        TEXT DEFAULT 'running',
    started_at    TEXT,
    ended_at      TEXT,
    duration_ms   REAL,
    metadata      TEXT,
    edited_output TEXT,
    FOREIGN KEY(trace_id) REFERENCES traces(id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_trace  ON nodes(trace_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at DESC);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_ms(started_at: str | None, ended_at: str | None) -> float | None:
    if not started_at or not ended_at:
        return None
    try:
        s = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        e = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        return (e - s).total_seconds() * 1000
    except Exception:
        return None


def _dumps(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


class ExecutionTracer(TracingExporter):
    """
    Записывает трейсы и спаны из Agents SDK в SQLite.
    Регистрируется как TracingExporter в ImmediateTraceProcessor.
    """

    def __init__(self, db_path: str | Path = _DEFAULT_DB):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ws_callbacks: list[Callable[[dict], None]] = []
        self._init_db()

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def register_ws_callback(self, cb: Callable[[dict], None]) -> None:
        """Зарегистрировать callback для WebSocket live-обновлений."""
        with self._lock:
            self._ws_callbacks.append(cb)

    def unregister_ws_callback(self, cb: Callable[[dict], None]) -> None:
        with self._lock:
            self._ws_callbacks = [c for c in self._ws_callbacks if c is not cb]

    def get_traces(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Список трейсов (новые первые)."""
        # #region agent log
        _log = {"id": "log_get_traces_enter", "timestamp": datetime.now(timezone.utc).timestamp() * 1000, "location": "core/timeline_tracer.py:get_traces", "message": "get_traces enter", "data": {"db_path": str(self._db_path), "limit": limit, "offset": offset}, "runId": "serve", "hypothesisId": "H2"}
        try:
            with open("/home/user/grid/.cursor/debug-11be9a.log", "a") as f:
                f.write(json.dumps(_log, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # #endregion
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    SELECT t.*,
                           COUNT(n.id) as node_count
                    FROM traces t
                    LEFT JOIN nodes n ON n.trace_id = t.id
                    GROUP BY t.id
                    ORDER BY t.started_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                )
                rows = cur.fetchall()
                result = [dict(r) for r in rows]
            # #region agent log
            _log2 = {"id": "log_get_traces_exit", "timestamp": datetime.now(timezone.utc).timestamp() * 1000, "location": "core/timeline_tracer.py:get_traces", "message": "get_traces exit", "data": {"row_count": len(rows), "db_path": str(self._db_path)}, "runId": "serve", "hypothesisId": "H3"}
            try:
                with open("/home/user/grid/.cursor/debug-11be9a.log", "a") as f:
                    f.write(json.dumps(_log2, ensure_ascii=False) + "\n")
            except Exception:
                pass
            # #endregion
            return result
        except Exception as e:
            # #region agent log
            _log3 = {"id": "log_get_traces_error", "timestamp": datetime.now(timezone.utc).timestamp() * 1000, "location": "core/timeline_tracer.py:get_traces", "message": "get_traces error", "data": {"error": str(e), "db_path": str(self._db_path)}, "runId": "serve", "hypothesisId": "H3"}
            try:
                with open("/home/user/grid/.cursor/debug-11be9a.log", "a") as f:
                    f.write(json.dumps(_log3, ensure_ascii=False) + "\n")
            except Exception:
                pass
            # #endregion
            raise

    def get_trace(self, trace_id: str) -> dict | None:
        """Полное дерево трейса: trace + вложенные nodes."""
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,))
            row = cur.fetchone()
            if not row:
                return None
            trace = dict(row)

            cur2 = conn.execute(
                "SELECT * FROM nodes WHERE trace_id = ? ORDER BY started_at",
                (trace_id,),
            )
            nodes = [dict(r) for r in cur2.fetchall()]

        trace["nodes"] = self._build_tree(nodes)
        trace["nodes_flat"] = nodes
        return trace

    def update_node_output(self, node_id: str, edited_output: str) -> bool:
        """Сохранить отредактированный вывод ноды."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE nodes SET edited_output = ? WHERE id = ?",
                (edited_output, node_id),
            )
        return True

    def get_node(self, node_id: str) -> dict | None:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    # ──────────────────────────────────────────────────────────────────────────
    # TracingExporter interface
    # ──────────────────────────────────────────────────────────────────────────

    def export(self, items: list[Trace | Span[Any]]) -> None:
        for item in items:
            try:
                data = item.export()
                if not data:
                    continue
                obj_type = data.get("object")
                if obj_type == "trace":
                    self._handle_trace(data)
                elif obj_type == "trace.span":
                    self._handle_span(data)
            except Exception as e:
                logger.debug(f"ExecutionTracer.export error: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Internal: trace / span handling
    # ──────────────────────────────────────────────────────────────────────────

    def _handle_trace(self, data: dict) -> None:
        trace_id = data.get("id") or ""
        if not trace_id:
            return
        # #region agent log
        _log = {"id": "log_handle_trace", "timestamp": datetime.now(timezone.utc).timestamp() * 1000, "location": "core/timeline_tracer.py:_handle_trace", "message": "writing trace to DB", "data": {"trace_id": trace_id, "db_path": str(self._db_path)}, "runId": "agent", "hypothesisId": "H1"}
        try:
            with open("/home/user/grid/.cursor/debug-11be9a.log", "a") as f:
                f.write(json.dumps(_log, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # #endregion
        row = {
            "id": trace_id,
            "workflow_name": data.get("workflow_name"),
            "group_id": data.get("group_id"),
            "metadata": _dumps(data.get("metadata")),
            "started_at": data.get("started_at") or _now_iso(),
            "ended_at": data.get("ended_at"),
            "status": "completed" if data.get("ended_at") else "running",
        }

        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE traces SET
                    ended_at = :ended_at,
                    status   = :status,
                    metadata = COALESCE(:metadata, metadata)
                WHERE id = :id
                """,
                row,
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO traces (id, workflow_name, group_id, metadata, started_at, ended_at, status)
                    VALUES (:id, :workflow_name, :group_id, :metadata, :started_at, :ended_at, :status)
                    """,
                    row,
                )

        self._notify_ws({"event": "trace_update", "trace": row})

    def _handle_span(self, data: dict) -> None:
        span_id = data.get("id") or ""
        trace_id = data.get("trace_id") or ""
        if not span_id or not trace_id:
            return

        span_data = data.get("span_data") or {}
        span_type = span_data.get("type") or "unknown"

        # Не трекаем mcp_tools (только инициализация инструментов, не вызовы)
        if span_type == "mcp_tools":
            return

        started_at = data.get("started_at") or _now_iso()
        ended_at = data.get("ended_at")
        duration = _duration_ms(started_at, ended_at)

        name = (
            span_data.get("name")
            or span_data.get("server")
            or span_type
        )

        # input / output зависят от типа спана
        input_data: Any = None
        output_data: Any = None
        metadata: dict = {}

        if span_type == "agent":
            input_data = span_data.get("input")
            output_data = span_data.get("output")
            name = span_data.get("name") or "agent"

        elif span_type == "generation":
            input_data = span_data.get("input")
            output_data = span_data.get("output")
            usage = span_data.get("usage") or {}
            metadata = {
                "model": span_data.get("model"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "input_cached_tokens": usage.get("input_tokens_details", {}).get("cached_tokens"),
            }

        elif span_type == "function":
            input_data = span_data.get("input")
            output_data = span_data.get("output")
            mcp_data = span_data.get("mcp_data") or {}
            if mcp_data:
                metadata = {"mcp_server": mcp_data.get("server"), "mcp_tool": mcp_data.get("tool")}

        elif span_type == "handoff":
            input_data = {"from_agent": span_data.get("from_agent")}
            output_data = {"to_agent": span_data.get("to_agent")}
            name = f"{span_data.get('from_agent')} → {span_data.get('to_agent')}"

        row = {
            "id": span_id,
            "trace_id": trace_id,
            "parent_id": data.get("parent_id"),
            "node_type": span_type,
            "name": name,
            "input_data": _dumps(input_data),
            "output_data": _dumps(output_data),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration,
            "status": "completed" if ended_at else "running",
            "metadata": _dumps(metadata) if metadata else None,
        }

        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE nodes SET
                    ended_at    = :ended_at,
                    duration_ms = :duration_ms,
                    status      = :status,
                    output_data = COALESCE(:output_data, output_data),
                    metadata    = COALESCE(:metadata, metadata)
                WHERE id = :id
                """,
                row,
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO nodes (id, trace_id, parent_id, node_type, name, input_data, output_data,
                                       started_at, ended_at, duration_ms, status, metadata)
                    VALUES (:id, :trace_id, :parent_id, :node_type, :name, :input_data, :output_data,
                            :started_at, :ended_at, :duration_ms, :status, :metadata)
                    """,
                    row,
                )

        self._notify_ws({"event": "node_update", "node": row})

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _build_tree(self, nodes: list[dict]) -> list[dict]:
        """Построить дерево nodes по parent_id. Возвращает корневые узлы."""
        by_id: dict[str, dict] = {n["id"]: {**n, "children": []} for n in nodes}
        roots: list[dict] = []

        for node in by_id.values():
            pid = node.get("parent_id")
            if pid and pid in by_id:
                by_id[pid]["children"].append(node)
            else:
                roots.append(node)

        return roots

    def _notify_ws(self, payload: dict) -> None:
        with self._lock:
            callbacks = list(self._ws_callbacks)
        for cb in callbacks:
            try:
                cb(payload)
            except Exception:
                pass


# ─── Глобальный синглтон ─────────────────────────────────────────────────────
_tracer: ExecutionTracer | None = None


def get_tracer(db_path: str | Path = _DEFAULT_DB) -> ExecutionTracer:
    """Возвращает (или создаёт) глобальный экземпляр ExecutionTracer."""
    global _tracer
    if _tracer is None:
        _tracer = ExecutionTracer(db_path)
    return _tracer
