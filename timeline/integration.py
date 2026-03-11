"""
Timeline server integration — запуск dashboard внутри агентной системы.

Используется из agent_chat.py и examples/telegram_bot/telegram_server.py для запуска timeline
в фоне в том же event loop, с доступом к AgentFactory (нужно для rerun).

Использование:
    factory = AgentFactory(...)
    asyncio.create_task(run_timeline_server(factory, port=8789))
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("grid.timeline.integration")

_ROOT = Path(__file__).resolve().parent.parent


async def run_timeline_server(
    factory: Any = None,
    port: int = 8789,
    host: str = "127.0.0.1",
    db_path: str | Path | None = None,
) -> None:
    """
    Запустить timeline сервер в фоне (asyncio task).
    Можно вызвать без await — просто как asyncio.create_task(run_timeline_server(...)).
    """
    try:
        import uvicorn
    except ImportError:
        logger.warning("uvicorn not installed — timeline server disabled. Run: pip install uvicorn")
        return

    from timeline.server import create_app

    db = db_path or _ROOT / "data" / "timeline.db"
    app = create_app(db_path=db, factory=factory)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)

    url = f"http://{host}:{port}/"
    print(f"Agent Timeline Dashboard: {url}")

    try:
        await server.serve()
    except Exception as e:
        logger.debug(f"Timeline server stopped: {e}")
