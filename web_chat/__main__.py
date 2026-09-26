"""CLI entry point for Grid Web Chat.

    grid-web-chat [options]                 one local user, no sign-in (the default)
    grid-web-chat --accounts [options]      many users, each signed in, each in a space of their own
    grid-web-chat accounts <command>        manage accounts (web_chat.accounts.cli)

With ``--accounts`` everything the server keeps lives in ``--data-dir``: the
accounts database and, per user, ``users/<id>/`` with the user's conversations,
agent sessions and workspace. Agents then run their tools only in containers:
the server refuses to start when the base system's isolation is off, unless
``--trusted-users`` says every user may run commands on this machine.
"""

from __future__ import annotations

import argparse
import asyncio
import re
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

#: The ids the accounts service gives users: 32 hex digits, safe as a directory name.
USER_ID = re.compile(r"[0-9a-f]{32}")
#: A space nobody used for this long is unloaded; it loads again on the next request.
IDLE_SPACE_SECONDS = 30 * 60


def parse_args(argv: list[str]) -> argparse.Namespace:
    from web_chat.accounts.cli import add_data_dir

    parser = argparse.ArgumentParser(
        description="Grid Web Chat",
        epilog="Account management: grid-web-chat accounts --help",
    )
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
        help="Working directory of the single user (overrides config when allow_path_override is true)",
    )
    parser.add_argument(
        "--user-id",
        "-u",
        default="default_user",
        help="The single user's identifier, naming its container isolation workspace",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)

    multi = parser.add_argument_group("accounts")
    multi.add_argument("--accounts", action="store_true", help="Serve many users who sign in")
    add_data_dir(multi)
    multi.add_argument(
        "--secure-cookies",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Mark the session cookie Secure (default: when the request came over HTTPS)",
    )
    multi.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        metavar="URL",
        help="Another origin whose pages may use the API, e.g. a proxy's https://chat.example.com",
    )
    multi.add_argument(
        "--forwarded-allow-ips",
        default=None,
        metavar="IPS",
        help="Proxies whose X-Forwarded-For/-Proto are trusted, comma-separated "
        "(default: 127.0.0.1). Sign-in limits count the real client address only through them",
    )
    multi.add_argument(
        "--trusted-users",
        action="store_true",
        help="Allow agents to run tools on this machine without containers (every user is trusted)",
    )
    return parser.parse_args(argv)


def main() -> None:
    # Windows consoles commonly use cp1251 while Grid logs contain Unicode.
    # Logging must never hide the actual startup error behind an encoding error.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")

    if sys.argv[1:2] == ["accounts"]:
        from web_chat.accounts.cli import main as accounts_main

        raise SystemExit(accounts_main(sys.argv[2:]))

    args = parse_args(sys.argv[1:])

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("uvicorn is required. Run: pip install 'uvicorn[standard]'") from exc

    from dotenv import load_dotenv

    from web_chat.deployment import Deployment
    from web_chat.server import create_app

    # Match CLI credentials without requiring a separate shell export.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    deployment = Deployment(
        config_path=args.config,
        routing_path=args.routing,
        working_directory=None if args.accounts else args.path,
    )
    print(f"Catalog: {deployment.routing_path if deployment.catalog else 'none'}")
    print(f"Config: {deployment.config_path}")

    options = multi_user_options(deployment, args) if args.accounts else single_user_options(deployment, args)
    print(f"Open http://{args.host}:{args.port}/")
    proxy = {"forwarded_allow_ips": args.forwarded_allow_ips} if args.forwarded_allow_ips else {}
    uvicorn.run(create_app(deployment, **options), host=args.host, port=args.port, **proxy)


def single_user_options(deployment, args: argparse.Namespace) -> dict:
    from web_chat.identity import single_user
    from web_chat.space import UserSpace
    from web_chat.spaces import SpacePool

    action_review_token = secrets.token_urlsafe(32)
    print(f"Working directory: {deployment.config.get_working_directory()}")
    print(f"Action review token: {action_review_token}")
    return {
        "spaces": SpacePool(lambda user_id: UserSpace(deployment, user_id=user_id)),
        "identify": single_user(args.user_id),
        "action_review_token": action_review_token,
        "warm_user": args.user_id,
    }


def multi_user_options(deployment, args: argparse.Namespace) -> dict:
    from web_chat.accounts import open_accounts
    from web_chat.accounts.http import SessionAuth
    from web_chat.space import SpaceLayout, UserSpace
    from web_chat.spaces import SpacePool

    if args.path:
        raise SystemExit("--path sets the single user's workspace; with --accounts every user has their own.")
    if not deployment.isolated and not args.trusted_users:
        raise SystemExit(
            f"Isolation is off in {deployment.config_path}: agents would run commands on this machine "
            "for every user. Enable isolation (Docker), or pass --trusted-users if every user may do that."
        )
    data_dir = args.data_dir.expanduser().resolve()
    accounts = open_accounts(data_dir)
    users_dir = data_dir / "users"

    def build(user_id: str) -> UserSpace:
        if not USER_ID.fullmatch(user_id):
            raise ValueError(f"Not a user id: {user_id!r}")
        return UserSpace(
            deployment,
            user_id=user_id,
            layout=SpaceLayout.under(users_dir / user_id),
            require_isolation=not args.trusted_users,
            turn_counter=accounts,
        )

    print(f"Data: {data_dir}")
    if not accounts.has_users():
        print("No accounts yet. Create the first admin: grid-web-chat accounts create-admin --username NAME")
    return {
        "spaces": SpacePool(build, idle_seconds=IDLE_SPACE_SECONDS),
        "auth": SessionAuth(accounts, secure_cookies=args.secure_cookies),
        "allowed_origins": tuple(args.allowed_origin),
        "warm_user": None,
    }


if __name__ == "__main__":
    main()
