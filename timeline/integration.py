"""
Timeline server integration — launch dashboard inside the agent system.

Used from agent_chat.py and examples/telegram_bot/telegram_server.py to start timeline
in the background in the same event loop, with access to AgentFactory (needed for rerun).

Usage:
    handle = TimelineHandle()
    asyncio.create_task(run_timeline_server(handle=handle, port=8789))
    # ... later, after factory is created:
    handle.update_factory(factory)
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("grid.timeline.integration")

_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class TimelineHandle:
    """Mutable handle for updating the timeline server factory after startup.
    
    Create a handle, pass it to run_timeline_server(), and later call
    update_factory() once the AgentFactory is ready.
    """
    factory_holder: dict[str, Any] | None = None
    
    def update_factory(self, factory: Any) -> None:
        """Update the factory reference in the running timeline server."""
        if self.factory_holder is not None:
            self.factory_holder["factory"] = factory
        else:
            logger.warning("TimelineHandle.update_factory called but factory_holder not set yet")


def _find_free_port(host: str, start_port: int, max_attempts: int = 10) -> int:
    """Find the first free port starting from *start_port*."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError as e:
                if e.errno == 98:  # EADDRINUSE
                    continue
                raise
    raise OSError(98, f"No free ports in range {start_port}–{start_port + max_attempts - 1}")


async def run_timeline_server(
    handle: TimelineHandle | None = None,
    factory: Any = None,  # deprecated: use handle.update_factory() after start
    port: int | None = None,
    host: str = "127.0.0.1",
    db_path: str | Path | None = None,
) -> None:
    """
    Start timeline server in the background (asyncio task).
    Can be called without await — just asyncio.create_task(run_timeline_server(...)).
    Pass a TimelineHandle to enable late factory binding.

    If *port* is ``None`` or the requested port is already in use, the next free
    port is used automatically (up to 10 attempts).
    """
    try:
        import uvicorn
    except ImportError:
        logger.warning("uvicorn not installed — timeline server disabled. Run: pip install uvicorn")
        return

    from timeline.server import create_app

    db = db_path or _ROOT / "data" / "timeline.db"
    app = create_app(db_path=db, factory=factory if handle is None else None)

    requested_port = port or 8789
    try:
        actual_port = _find_free_port(host, requested_port)
    except OSError as e:
        logger.warning("Timeline dashboard: %s", e)
        return

    config = uvicorn.Config(
        app,
        host=host,
        port=actual_port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # If a handle was provided, bind the factory holder from the app
    if handle is not None:
        # If a factory was also passed directly, update now
        if factory is not None:
            app.state.timeline_factory_holder["factory"] = factory
        # Set the handle's factory_holder so update_factory() works
        handle.factory_holder = app.state.timeline_factory_holder

    url = f"http://{host}:{actual_port}/"
    print(f"Agent Timeline Dashboard: {url}")

    try:
        await server.serve()
    except Exception as e:
        logger.debug(f"Timeline server stopped: {e}")

    return server
