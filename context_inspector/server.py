"""
Context Inspector API Server — read-only FastAPI surface for session buckets.

Endpoints:
  GET  /                                        — dashboard HTML
  GET  /api/status                              — path / counts
  GET  /api/contexts                            — context summaries
  GET  /api/contexts/{id}                       — detail overview
  GET  /api/contexts/{id}/assembly              — last model-context assembly
  GET  /api/contexts/{id}/messages              — conversation messages
  GET  /api/contexts/{id}/executions            — execution history
  POST /api/reload                              — re-read persistence from disk
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from context_inspector.service import (
    context_status,
    open_context_manager,
    reload_context_manager,
    resolve_context_path,
)

logger = logging.getLogger("grid.context_inspector.server")

ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_DIR = Path(__file__).resolve().parent
_DASHBOARD_CANDIDATES = (
    _PACKAGE_DIR / "dashboard.html",
    ROOT / "context_inspector" / "dashboard.html",
)


def _resolve_dashboard_html() -> Path:
    for candidate in _DASHBOARD_CANDIDATES:
        if candidate.exists():
            return candidate
    return _DASHBOARD_CANDIDATES[0]


def create_app(
    context_path: str | Path | None = None,
    context_manager: Optional[Any] = None,
) -> FastAPI:
    """
    Create the FastAPI inspector.

    Args:
        context_path: Path to logs/context.json (or equivalent).
        context_manager: Optional injected manager (tests / shared process).
            If provided, it should already be read-only.
    """
    resolved = resolve_context_path(context_path)
    state: dict[str, Any] = {
        "path": resolved,
        "manager": context_manager
        if context_manager is not None
        else open_context_manager(resolved),
    }

    app = FastAPI(title="Context Inspector", docs_url=None, redoc_url=None)

    def _manager():
        return state["manager"]

    def _not_found(context_id: str) -> HTTPException:
        return HTTPException(status_code=404, detail=f"Context not found: {context_id}")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        dashboard_html = _resolve_dashboard_html()
        if not dashboard_html.exists():
            return HTMLResponse(
                "<h1>context_inspector/dashboard.html not found</h1>",
                status_code=503,
            )
        return HTMLResponse(dashboard_html.read_text(encoding="utf-8"))

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(context_status(_manager()))

    @app.post("/api/reload")
    async def api_reload() -> JSONResponse:
        try:
            state["manager"] = reload_context_manager(_manager(), state["path"])
        except Exception as exc:
            logger.exception("Failed to reload contexts")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **context_status(state["manager"])})

    @app.get("/api/contexts")
    async def list_contexts(
        q: Optional[str] = Query(default=None, description="Filter by id/agent/preview"),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> JSONResponse:
        summaries = _manager().list_context_summaries()
        if q:
            needle = q.strip().lower()
            if needle:
                summaries = [
                    item
                    for item in summaries
                    if needle in str(item.get("id") or "").lower()
                    or needle in str(item.get("agent") or "").lower()
                    or needle in str(item.get("user_id") or "").lower()
                    or needle in str(item.get("last_message_preview") or "").lower()
                ]
        return JSONResponse(
            {
                "contexts": summaries[:limit],
                "count": len(summaries[:limit]),
                "total": len(summaries),
                **context_status(_manager()),
            }
        )

    @app.get("/api/contexts/{context_id}")
    async def get_context(
        context_id: str,
        include_messages: bool = True,
        include_executions: bool = True,
        include_assembly: bool = True,
        include_raw_metadata: bool = False,
        max_messages: int = Query(default=50, ge=0, le=500),
        max_executions: int = Query(default=50, ge=0, le=500),
        preview_limit: int = Query(default=240, ge=40, le=4000),
        include_full_messages: bool = False,
        include_full_executions: bool = False,
        include_full_runtime: bool = False,
    ) -> JSONResponse:
        detail = _manager().get_context_bucket(
            context_id,
            include_messages=include_messages,
            include_executions=include_executions,
            include_assembly=include_assembly,
            include_raw_metadata=include_raw_metadata,
            preview_limit=preview_limit,
            include_full_messages=include_full_messages,
            include_full_executions=include_full_executions,
            include_full_runtime=include_full_runtime,
            max_messages=max_messages if max_messages > 0 else None,
            max_executions=max_executions if max_executions > 0 else None,
        )
        if detail is None:
            raise _not_found(context_id)
        return JSONResponse(detail)

    @app.get("/api/contexts/{context_id}/assembly")
    async def get_assembly(context_id: str) -> JSONResponse:
        payload = _manager().get_context_assembly(context_id)
        if payload is None:
            raise _not_found(context_id)
        return JSONResponse(payload)

    @app.get("/api/contexts/{context_id}/messages")
    async def get_messages(
        context_id: str,
        limit: int = Query(default=100, ge=0, le=1000),
        preview_limit: int = Query(default=500, ge=40, le=8000),
        include_full: bool = False,
    ) -> JSONResponse:
        payload = _manager().get_context_messages(
            context_id,
            limit=limit if limit > 0 else None,
            preview_limit=preview_limit,
            include_full=include_full,
        )
        if payload is None:
            raise _not_found(context_id)
        return JSONResponse(payload)

    @app.get("/api/contexts/{context_id}/executions")
    async def get_executions(
        context_id: str,
        limit: int = Query(default=100, ge=0, le=1000),
        preview_limit: int = Query(default=500, ge=40, le=8000),
        include_full: bool = False,
    ) -> JSONResponse:
        payload = _manager().get_context_executions(
            context_id,
            limit=limit if limit > 0 else None,
            preview_limit=preview_limit,
            include_full=include_full,
        )
        if payload is None:
            raise _not_found(context_id)
        return JSONResponse(payload)

    app.state.context_inspector = state
    return app
