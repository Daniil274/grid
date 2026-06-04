"""CLI entry point for Grid Web Chat."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Grid Web Chat")
    parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--path",
        "-p",
        default=None,
        help="Working directory (overrides config when allow_path_override is true)",
    )
    parser.add_argument(
        "--user-id",
        "-u",
        default="default_user",
        help="User identifier for container isolation workspace",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("uvicorn is required. Run: pip install 'uvicorn[standard]'") from exc

    from web_chat.runtime import WebChatRuntime
    from web_chat.server import create_app

    runtime = WebChatRuntime(
        config_path=args.config,
        working_directory=args.path,
        user_id=args.user_id,
    )

    print(f"Config: {runtime.config_path}")
    print(f"Working directory: {runtime.workspace_path}")
    print(f"Sessions: {runtime.persist_path / 'context.json'}")
    print(f"Open http://{args.host}:{args.port}/")

    uvicorn.run(create_app(runtime), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
