"""CLI entry point for Grid Web Chat.

    grid-web-chat [options]                 one local user, no sign-in (the default)
    grid-web-chat --accounts [options]      many users, each signed in, each in a space of their own
    grid-web-chat accounts <command>        manage accounts (web_chat.accounts.cli)

With ``--accounts`` everything the server keeps lives in ``--data-dir``: the
accounts database and, per user, ``users/<id>/`` with the user's conversations,
agent sessions and workspace. Every user's agents then run in a container of
their own, whatever the config's ``isolation.enabled`` says (a one-user server
follows that flag); the server refuses to start without a usable Docker,
unless ``--trusted-users`` says every user may run commands on this machine.
"""

from __future__ import annotations

import argparse
import asyncio
import os
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
        help="Run users' agents without containers, on this machine (every user is trusted)",
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
    restart_requested = False

    def restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        runtime.should_exit = True

    app = create_app(deployment, **options, restart_callback=restart)
    runtime = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, **proxy))
    runtime.run()
    if not runtime.started:
        raise SystemExit(3)  # Preserve uvicorn.run's startup-failure exit status.
    if restart_requested:
        # Lifespan shutdown closed SQLite/MCP/container resources first. Re-exec
        # loads the new code with the same interpreter, arguments and service PID.
        os.execv(sys.executable, [sys.executable, "-m", "web_chat", *sys.argv[1:]])


def single_user_options(deployment, args: argparse.Namespace) -> dict:
    from web_chat.identity import single_user
    from web_chat.space import UserSpace
    from web_chat.spaces import SpacePool

    from web_chat.system_activity import SystemActivity
    from web_chat.usage import UsageStore

    action_review_token = secrets.token_urlsafe(32)
    print(f"Working directory: {deployment.config.get_working_directory()}")
    print(f"Action review token: {action_review_token}")
    records = Path(deployment.config.get_logs_directory())
    activity = SystemActivity(records / "system_activity.json")
    usage = UsageStore(records / "usage.db")
    return {
        # The one user owns the server: they test the drafts.
        "spaces": (
            pool := SpacePool(
                lambda user_id: UserSpace(
                    deployment, user_id=user_id, activity=activity, usage=usage, admin=True,
                    # What the system builder makes shows in every space once it is free.
                    on_systems_changed=lambda: pool.invalidate(),
                )
            )
        ),
        "activity": activity,
        "usage": usage,
        "submissions_dir": records / "system_submissions",
        "restart_path": records / "server_restart.json",
        "identify": single_user(args.user_id),
        "action_review_token": action_review_token,
        "warm_user": args.user_id,
        # Beside the single user's own records (web_chat.space.single_user_records).
        "reviews": open_reviews(deployment, Path(deployment.config.get_logs_directory()) / "reviews"),
    }


def multi_user_options(deployment, args: argparse.Namespace) -> dict:
    from web_chat.accounts import open_accounts
    from web_chat.accounts.http import SessionAuth
    from web_chat.space import SpaceLayout, UserSpace
    from web_chat.spaces import SpacePool

    if args.path:
        raise SystemExit("--path sets the single user's workspace; with --accounts every user has their own.")
    if not args.trusted_users:
        # With accounts every user's agents run in a container, whatever the
        # config's isolation.enabled says; the config gives image and limits.
        from core.managers.container_manager import ContainerManager

        if not ContainerManager(deployment.config, enabled=True).enabled:
            raise SystemExit(
                "With accounts, every user's agents run in a container, but Docker is not usable here "
                "(the docker SDK is missing or the daemon does not answer). Start Docker, or pass "
                "--trusted-users if every user may run commands on this machine."
            )
    from web_chat.system_activity import SystemActivity
    from web_chat.usage import UsageStore

    data_dir = args.data_dir.expanduser().resolve()
    accounts = open_accounts(data_dir)
    usage = UsageStore(data_dir / "usage.db")
    usage.import_legacy(accounts.usage_history())
    users_dir = data_dir / "users"
    activity = SystemActivity(data_dir / "systems" / "activity.json")

    def build(user_id: str) -> UserSpace:
        if not USER_ID.fullmatch(user_id):
            raise ValueError(f"Not a user id: {user_id!r}")
        return UserSpace(
            deployment,
            user_id=user_id,
            layout=SpaceLayout.under(users_dir / user_id),
            require_isolation=not args.trusted_users,
            turn_counter=accounts,
            activity=activity,
            usage=usage,
            # Drafts, admins-only systems and the system builder are for admins;
            # a role change applies when the space is next built.
            admin=bool((user := accounts.user(user_id)) and user.is_admin),
            on_systems_changed=lambda: pool.invalidate(),
        )

    print(f"Data: {data_dir}")
    if not accounts.has_users():
        print("No accounts yet. Create the first admin: grid-web-chat accounts create-admin --username NAME")
    pool = SpacePool(build, idle_seconds=IDLE_SPACE_SECONDS)
    return {
        "spaces": pool,
        "auth": SessionAuth(accounts, secure_cookies=args.secure_cookies),
        "allowed_origins": tuple(args.allowed_origin),
        "warm_user": None,
        "reviews": open_reviews(deployment, data_dir / "reviews"),
        "activity": activity,
        "usage": usage,
        "submissions_dir": data_dir / "systems" / "submissions",
        "restart_path": data_dir / "server_restart.json",
    }


def open_reviews(deployment, root: Path):
    """The review desk of this server, keeping its reviews under *root* (web_chat.review)."""
    from web_chat.review.agents import ReviewAgents
    from web_chat.review.desk import ReviewDesk
    from web_chat.review.evolution import EvolutionOutbox
    from web_chat.review.store import ReviewStore

    store = ReviewStore(root)
    return ReviewDesk(
        store,
        lambda: deployment.review_policy,
        agents=ReviewAgents(store, root),
        evolution=EvolutionOutbox.from_env(),
    )


if __name__ == "__main__":
    main()
