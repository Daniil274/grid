"""CLI entry point for Context Inspector dashboard.

Run::

    python -m context_inspector
    grid-context-inspector

Examples::

    python -m context_inspector --port 8790
    python -m context_inspector --context logs/context.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Context Inspector Dashboard (read-only, standalone)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8790, help="Bind port")
    parser.add_argument(
        "--context",
        dest="context_path",
        default=None,
        help="Path to context.json (default: logs/context.json)",
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "uvicorn is required. Run: pip install 'uvicorn[standard]'"
        ) from exc

    from context_inspector.server import create_app
    from context_inspector.service import resolve_context_path

    resolved = resolve_context_path(args.context_path)
    app = create_app(context_path=resolved)

    url = f"http://{args.host}:{args.port}/"
    print(f"Context Inspector: {url}")
    print(f"Persistence: {resolved}" + (" (missing)" if not resolved.exists() else ""))
    print("Mode: read-only (no writes to context.json)")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="error",
        access_log=False,
    )


if __name__ == "__main__":
    main()
