"""Grid user-level paths (~/.grid/...)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def get_grid_home() -> Path:
    """Return the Grid user data directory (~/.grid)."""
    return Path.home() / ".grid"


def get_default_logs_dir() -> Path:
    """Return the default logs directory (~/.grid/logs)."""
    return get_grid_home() / "logs"


def resolve_logs_directory(
    configured: Optional[str] = None,
    *,
    working_directory: Optional[str] = None,
) -> Path:
    """
    Resolve the logs directory.

    - When ``settings.logs_directory`` is set in config: use it
      (absolute/~ paths as-is; relative paths against working_directory).
    - When unset or blank: default to ~/.grid/logs.
    """
    if configured is not None:
        configured = str(configured).strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path.resolve()
        base = Path(working_directory or os.getcwd())
        return (base / path).resolve()

    return get_default_logs_dir().resolve()
