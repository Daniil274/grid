"""Read-only helpers for loading and querying context buckets."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from core.context import ContextManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT_PATH = PROJECT_ROOT / "logs" / "context.json"


def resolve_context_path(path: str | Path | None = None) -> Path:
    """Resolve a context persistence path relative to the project root when needed."""
    if path is None:
        return DEFAULT_CONTEXT_PATH
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    project_path = (PROJECT_ROOT / candidate).resolve(strict=False)
    if project_path.exists():
        return project_path
    cwd_path = candidate.resolve(strict=False)
    if cwd_path.exists():
        return cwd_path
    return project_path


def open_context_manager(
    path: str | Path | None = None,
    *,
    max_history: int = 10_000,
) -> ContextManager:
    """
    Open persistence in read-only inspector mode.

    Does not create a fresh empty session and never writes back to disk.
    """
    resolved = resolve_context_path(path)
    return ContextManager(
        max_history=max_history,
        persist_path=str(resolved) if resolved.exists() or path is not None else str(resolved),
        read_only=True,
        create_initial_context=False,
    )


def reload_context_manager(
    manager: ContextManager,
    path: str | Path | None = None,
) -> ContextManager:
    """Rebuild a read-only manager from disk so the dashboard can refresh."""
    target = path if path is not None else manager.persist_path
    return open_context_manager(target)


def context_status(manager: ContextManager) -> dict[str, Any]:
    """Compact status payload for the dashboard."""
    persist = manager.persist_path
    return {
        "persist_path": str(persist) if persist else None,
        "exists": bool(persist and persist.exists()),
        "context_count": len(manager.list_context_ids()),
        "active_context_id": manager.get_current_context_id(),
        "read_only": bool(getattr(manager, "read_only", False)),
    }
