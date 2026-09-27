"""Top-level Grid CLI entrypoint."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Grid CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mcp_parser = subparsers.add_parser(
        "serve-mcp",
        help="Serve the minimal Codex-to-Grid worker interface over STDIO",
    )
    mcp_parser.add_argument("--config", default="config.yaml")
    mcp_parser.add_argument("--workdir", default=None)
    mcp_parser.add_argument("--tools-dir", default="examples/coder/tools")
    mcp_parser.add_argument("--provider", default="opencode")
    mcp_parser.add_argument("--max-concurrency", type=int, default=4)
    mcp_parser.add_argument("--default-timeout", type=int, default=300)
    mcp_parser.add_argument("--max-timeout", type=int, default=900)

    args = parser.parse_args()
    if args.command == "serve-mcp":
        from grid_mcp import serve

        serve(
            config_path=args.config,
            workdir=args.workdir,
            tools_directory=args.tools_dir,
            provider=args.provider,
            max_concurrency=args.max_concurrency,
            default_timeout_seconds=args.default_timeout,
            max_timeout_seconds=args.max_timeout,
        )
        return 0
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
