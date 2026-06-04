"""
Timeline API Server — FastAPI + WebSocket for visualizing agent execution.

Endpoints:
  GET  /                                        — dashboard HTML
  GET  /api/traces                              — trace list
  GET  /api/traces/{id}                         — trace tree
  GET  /api/traces/{id}/nodes/{nid}/messages    — message snapshot from generation node
  POST /api/traces/{id}/nodes/{nid}/edit        — edit node output
  POST /api/traces/{id}/nodes/{nid}/rerun       — rerun from snapshot (requires factory)
  WS   /ws                                      — live updates
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("grid.timeline.server")

ROOT = Path(__file__).resolve().parent.parent
_TIMELINE_DIR = Path(__file__).resolve().parent
_DASHBOARD_CANDIDATES = (
    ROOT / "timeline_dashboard.html",
    _TIMELINE_DIR / "timeline_dashboard.html",
    ROOT / "scripts" / "timeline_dashboard.html",
)


def _resolve_dashboard_html() -> Path:
    for candidate in _DASHBOARD_CANDIDATES:
        if candidate.exists():
            return candidate
    return _DASHBOARD_CANDIDATES[0]


# ─── Pydantic models ──────────────────────────────────────────────────────────

class EditRequest(BaseModel):
    output: str


class RerunRequest(BaseModel):
    messages: list[dict]          # modified messages from generation snapshot
    user_id: str = "default_user"


# ─── WebSocket manager ────────────────────────────────────────────────────────

class WSManager:
    def __init__(self) -> None:
        self._clients: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.append(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients = [c for c in self._clients if c is not ws]

    async def broadcast(self, payload: dict) -> None:
        msg = json.dumps(payload, ensure_ascii=False)
        async with self._lock:
            clients = list(self._clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                self._clients = [c for c in self._clients if c not in dead]


# ─── App factory ──────────────────────────────────────────────────────────────

def create_app(
    db_path: str | Path | None = None,
    factory: Optional[Any] = None,
) -> FastAPI:
    from core.tracing.tracer import get_tracer, ExecutionTracer

    tracer: ExecutionTracer = get_tracer(db_path) if db_path else get_tracer()

    ws_manager = WSManager()
    loop_holder: dict[str, Any] = {}
    # Mutable holder so the factory can be updated after server start
    factory_holder: dict[str, Any] = {"factory": factory}

    def _on_tracer_event(payload: dict) -> None:
        loop = loop_holder.get("loop")
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(ws_manager.broadcast(payload), loop)

    tracer.register_ws_callback(_on_tracer_event)

    app = FastAPI(title="Agent Timeline", docs_url=None, redoc_url=None)

    @app.on_event("startup")
    async def _startup() -> None:
        loop_holder["loop"] = asyncio.get_event_loop()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        tracer.unregister_ws_callback(_on_tracer_event)

    # ── Routes ───────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        dashboard_html = _resolve_dashboard_html()
        if not dashboard_html.exists():
            return HTMLResponse("<h1>timeline_dashboard.html not found</h1>", status_code=503)
        return HTMLResponse(dashboard_html.read_text(encoding="utf-8"))

    @app.get("/api/traces")
    async def list_traces(limit: int = 100, offset: int = 0) -> JSONResponse:
        # #region agent log
        try:
            _log = {"id": "log_list_traces", "timestamp": __import__("time").time() * 1000, "location": "timeline/server.py:list_traces", "message": "GET /api/traces", "data": {"limit": limit, "offset": offset, "tracer_db": str(getattr(tracer, "_db_path", "?"))}, "runId": "serve", "hypothesisId": "H2"}
            open("/home/user/grid/.cursor/debug-11be9a.log", "a").write(__import__("json").dumps(_log, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # #endregion
        traces = tracer.get_traces(limit=limit, offset=offset)
        # #region agent log
        try:
            _log = {"id": "log_list_traces_result", "timestamp": __import__("time").time() * 1000, "location": "timeline/server.py:list_traces", "message": "get_traces result", "data": {"count": len(traces), "first_id": traces[0]["id"] if traces else None}, "runId": "serve", "hypothesisId": "H1"}
            open("/home/user/grid/.cursor/debug-11be9a.log", "a").write(__import__("json").dumps(_log, ensure_ascii=False) + "\n")
        except Exception:
            pass
        # #endregion
        return JSONResponse({"traces": traces, "limit": limit, "offset": offset})

    @app.get("/api/traces/{trace_id}")
    async def get_trace(trace_id: str) -> JSONResponse:
        trace = tracer.get_trace(trace_id)
        if not trace:
            raise HTTPException(status_code=404, detail="Trace not found")
        return JSONResponse(trace)

    @app.get("/api/traces/{trace_id}/nodes/{node_id}/messages")
    async def get_node_messages(trace_id: str, node_id: str) -> JSONResponse:
        """Return message snapshot from generation span for editing."""
        node = tracer.get_node(node_id)
        if not node or node.get("trace_id") != trace_id:
            raise HTTPException(status_code=404, detail="Node not found")

        from timeline.rerun import extract_messages
        raw_input = node.get("input_data")
        messages = extract_messages(raw_input)
        return JSONResponse({
            "node_id": node_id,
            "node_type": node.get("node_type"),
            "messages": messages,
            "rerun_available": factory_holder["factory"] is not None,
        })

    @app.post("/api/traces/{trace_id}/nodes/{node_id}/edit")
    async def edit_node(trace_id: str, node_id: str, body: EditRequest) -> JSONResponse:
        node = tracer.get_node(node_id)
        if not node or node.get("trace_id") != trace_id:
            raise HTTPException(status_code=404, detail="Node not found")
        tracer.update_node_output(node_id, body.output)
        await ws_manager.broadcast({
            "event": "node_edited",
            "trace_id": trace_id,
            "node_id": node_id,
            "edited_output": body.output,
        })
        return JSONResponse({"ok": True})

    @app.post("/api/traces/{trace_id}/nodes/{node_id}/rerun")
    async def rerun_node(trace_id: str, node_id: str, body: RerunRequest) -> JSONResponse:
        """
        Restart the agent from a generation span snapshot with possibly modified messages.
        Requires that factory was passed to create_app().
        """
        if factory_holder["factory"] is None:
            raise HTTPException(
                status_code=503,
                detail="Rerun unavailable: no AgentFactory bound. "
                       "Start with: python -m timeline --with-factory -c config.yaml",
            )

        node = tracer.get_node(node_id)
        if not node or node.get("trace_id") != trace_id:
            raise HTTPException(status_code=404, detail="Node not found")

        from timeline.rerun import rerun_from_node
        try:
            result = await rerun_from_node(
                factory=factory_holder["factory"],
                node=node,
                modified_messages=body.messages,
                user_id=body.user_id,
            )
        except Exception as e:
            logger.error(f"Rerun failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        return JSONResponse(result)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, ConnectionResetError, OSError):
            pass
        finally:
            try:
                await ws_manager.disconnect(websocket)
            except Exception:
                pass

    # Expose factory holder for late binding (update factory after server start)
    app.state.timeline_factory_holder = factory_holder

    return app
