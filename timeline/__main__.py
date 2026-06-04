"""CLI entry point for Agent Timeline dashboard.

Run without agent_chat::

    python -m timeline
    grid-timeline

With rerun support (loads AgentFactory from config)::

    python -m timeline --with-factory -c config.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional

if sys.platform == "win32":
    try:
        import asyncio
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_config_path(config_path: str) -> Path:
    path = Path(config_path).expanduser()
    if path.is_absolute():
        return path.resolve(strict=False)

    project_path = PROJECT_ROOT / path
    if project_path.exists():
        return project_path.resolve(strict=False)

    cwd_path = path.resolve(strict=False)
    if cwd_path.exists():
        return cwd_path

    return project_path.resolve(strict=False)


def _isolation_enabled(config: Any) -> bool:
    isolation_cfg = getattr(config.config, "isolation", None)
    if not isolation_cfg:
        return False
    if isinstance(isolation_cfg, dict):
        return bool(isolation_cfg.get("enabled", False))
    return bool(getattr(isolation_cfg, "enabled", False))


def _build_factory(
    *,
    config_path: str,
    working_directory: Optional[str],
    user_id: str,
) -> Any:
    from core.agent_factory import AgentFactory
    from core.config import Config

    resolved = _resolve_config_path(config_path)
    config = Config(str(resolved), working_directory)
    container_id: Optional[str] = None

    if _isolation_enabled(config):
        try:
            from core.managers.container_manager import ContainerManager
        except Exception:
            ContainerManager = None  # type: ignore[misc, assignment]

        if ContainerManager is not None:
            if working_directory is not None:
                user_workspace = Path(working_directory).expanduser().resolve()
            else:
                user_workspace = (
                    resolved.parent / "workspace" / f"user_{user_id}"
                ).resolve()
            user_workspace.mkdir(parents=True, exist_ok=True)

            container_manager = ContainerManager(config)
            if container_manager.enabled:
                try:
                    container = container_manager.get_or_create_container(
                        user_id,
                        workspace=user_workspace,
                    )
                    if container:
                        container_id = container.id
                except Exception:
                    pass
            config = Config(str(resolved), str(user_workspace))

    return AgentFactory(
        config=config,
        working_directory=config.get_working_directory(),
        container_id=container_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent Timeline Dashboard (standalone, no agent_chat)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8789, help="Bind port")
    parser.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="SQLite trace database (default: core/data/timeline.db)",
    )
    parser.add_argument(
        "--with-factory",
        action="store_true",
        help="Load AgentFactory from config for node rerun in the dashboard",
    )
    parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="Config path (used with --with-factory)",
    )
    parser.add_argument(
        "--path",
        "-p",
        default=None,
        help="Working directory override (used with --with-factory)",
    )
    parser.add_argument(
        "--user-id",
        "-u",
        default="default_user",
        help="User id for container workspace (used with --with-factory)",
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "uvicorn is required. Run: pip install 'uvicorn[standard]'"
        ) from exc

    from timeline.server import create_app

    factory = None
    if args.with_factory:
        factory = _build_factory(
            config_path=args.config,
            working_directory=args.path,
            user_id=args.user_id,
        )

    app = create_app(db_path=args.db_path, factory=factory)
    url = f"http://{args.host}:{args.port}/"
    print(f"Agent Timeline Dashboard: {url}")
    if factory is None:
        print("Rerun: disabled (pass --with-factory -c config.yaml to enable)")
    else:
        print(f"Rerun: enabled (config={_resolve_config_path(args.config)})")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="error",
        access_log=False,
    )


if __name__ == "__main__":
    main()
