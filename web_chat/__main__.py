"""CLI entry point for Grid Web Chat."""

from __future__ import annotations

import argparse
import asyncio
import secrets
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
    # Windows consoles commonly use cp1251 while Grid logs contain Unicode.
    # Logging must never hide the actual startup error behind an encoding error.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Grid Web Chat")
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="Run a single system from this config (default: route across --routing)",
    )
    parser.add_argument(
        "--routing",
        default="routing.yaml",
        help="System catalog used when --config is not given",
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

    from dotenv import load_dotenv

    from web_chat.deployment import Deployment
    from web_chat.identity import single_user
    from web_chat.server import create_app
    from web_chat.space import UserSpace
    from web_chat.spaces import SpacePool

    # Match CLI credentials without requiring a separate shell export.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    deployment = Deployment(
        config_path=args.config,
        routing_path=args.routing,
        working_directory=args.path,
    )
    spaces = SpacePool(lambda user_id: UserSpace(deployment, user_id=user_id))
    action_review_token = secrets.token_urlsafe(32)

    print(f"Catalog: {deployment.routing_path if deployment.catalog else 'none'}")
    print(f"Config: {deployment.config_path}")
    print(f"Working directory: {deployment.config.get_working_directory()}")
    print(f"Action review token: {action_review_token}")
    print(f"Open http://{args.host}:{args.port}/")

    app = create_app(
        deployment,
        spaces,
        identify=single_user(args.user_id),
        action_review_token=action_review_token,
        warm_user=args.user_id,
    )
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
