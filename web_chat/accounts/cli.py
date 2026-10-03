"""The operator's account commands: ``grid-web-chat accounts <command>``.

    create-admin --username NAME   the first admin (or another one); asks for the password
    invite [--role user] [--days 7] [--note TEXT]   prints a sign-up link's code
    list                           the users and the open invites
    import-chats --username NAME [--config C | --routing R] [--path P]
                                   copy a one-user server's chats into NAME's space

They open the same database the server uses (``--data-dir``), so they work
while it runs. A password is only ever typed at the prompt, never passed as an
argument: arguments end up in shell history and process lists.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from web_chat.accounts import open_accounts
from web_chat.accounts.passwords import WeakPassword
from web_chat.accounts.service import AccountError, Accounts


def add_data_dir(parser: argparse.ArgumentParser) -> None:
    from utils.grid_paths import get_grid_home

    parser.add_argument(
        "--data-dir",
        type=Path,
        default=get_grid_home() / "web",
        help="Where the server keeps accounts and user spaces (default: ~/.grid/web)",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="grid-web-chat accounts", description="Manage web chat accounts")
    add_data_dir(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-admin", help="Create an admin account")
    create.add_argument("--username", required=True)

    invite = commands.add_parser("invite", help="Create an invite")
    invite.add_argument("--role", choices=("user", "admin"), default="user")
    invite.add_argument("--tier", default="", help="The plan the new user is on (a tier of the config; default tier if empty)")
    invite.add_argument("--days", type=float, default=7.0, help="How long the invite stays valid")
    invite.add_argument("--note", default="", help="Who the invite is for, for the list")

    settier = commands.add_parser("set-tier", help="Move a user to another plan")
    settier.add_argument("--username", required=True)
    settier.add_argument("--tier", required=True, help="A tier of the config; '' for the default tier")

    commands.add_parser("list", help="List users and open invites")

    adopt = commands.add_parser(
        "import-chats", help="Copy the chats of a one-user server into a user's space"
    )
    adopt.add_argument("--username", required=True, help="Whose space receives the chats")
    adopt.add_argument("--config", default=None, help="The single system the one-user server ran")
    adopt.add_argument("--routing", default="routing.yaml", help="The catalog it routed across (default)")
    adopt.add_argument("--path", default=None, help="The --path it ran with, if any")

    args = parser.parse_args(argv)
    accounts = open_accounts(args.data_dir)
    try:
        if args.command == "create-admin":
            return _create_admin(accounts, args.username)
        if args.command == "set-tier":
            user = accounts.find_user(args.username)
            if user is None:
                raise AccountError(f"No user named {args.username!r}.")
            accounts.set_tier(user.id, args.tier)
            print(f"{user.username} is now on {args.tier or 'the default tier'}.")
            return 0
        if args.command == "import-chats":
            return _import_chats(accounts, args)
        if args.command == "invite":
            code, created = accounts.create_invite(
                None, role=args.role, tier=args.tier, ttl=args.days * 86400, note=args.note
            )
            plan = f" on tier {created.tier}" if created.tier else ""
            print(f"Invite for a new {created.role}{plan}, valid until {_when(created.expires_at)}:")
            print(f"  /login#invite={code}")
            print("Open it on the server's address, e.g. https://chat.example.com/login#invite=...")
            return 0
        _list(accounts)
        return 0
    except (AccountError, WeakPassword) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _create_admin(accounts: Accounts, username: str) -> int:
    password = getpass.getpass(f"Password for {username}: ")
    if getpass.getpass("Repeat the password: ") != password:
        print("error: the passwords differ", file=sys.stderr)
        return 1
    user = accounts.create_user(username, password, role="admin")
    print(f"Admin {user.username} created.")
    return 0


def _import_chats(accounts: Accounts, args: argparse.Namespace) -> int:
    """Copy - never move - the one-user server's records into the user's space.

    Only into a space that has none yet: merging two histories is not done.
    The server should not run meanwhile, or the user must not have opened
    their space since it started.
    """
    import shutil
    import sqlite3

    from web_chat.deployment import Deployment
    from web_chat.space import SpaceLayout, single_user_records

    user = accounts.find_user(args.username)
    if user is None:
        raise AccountError(f"No user named {args.username!r}.")
    deployment = Deployment(config_path=args.config, routing_path=args.routing, working_directory=args.path)
    conversations, sessions = single_user_records(deployment.config)
    if not conversations.exists():
        raise AccountError(f"No chats to import: {conversations} does not exist.")
    target = SpaceLayout.under(args.data_dir.expanduser().resolve() / "users" / user.id)
    for existing in (target.conversations, target.agent_sessions):
        if existing.exists():
            raise AccountError(f"{user.username} already has records ({existing}); nothing was copied.")

    target.conversations.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(conversations, target.conversations)
    if sessions.exists():
        # The backup API copies a consistent database, write-ahead log included.
        source, copy = sqlite3.connect(sessions), sqlite3.connect(target.agent_sessions)
        try:
            source.backup(copy)
        finally:
            source.close()
            copy.close()
    print(f"Copied the chats of {conversations.parent} into {user.username}'s space.")
    if not sessions.exists():
        print(f"No agent sessions at {sessions}: the chats read as before, agents start them afresh.")
    return 0


def _list(accounts: Accounts) -> None:
    print("Users:")
    for account in accounts.accounts():
        state = "disabled" if account.disabled else "active"
        tier = account.user.tier or "(default)"
        print(
            f"  {account.user.username:<32} {account.user.role:<6} {tier:<12} {state:<9} "
            f"since {_when(account.created_at)}"
        )
    print("Open invites:")
    for invite in accounts.invites():
        if invite.used_at is None:
            print(f"  {invite.id}  {invite.role:<6} {invite.tier or '(default)':<12} until {_when(invite.expires_at)}  {invite.note}")


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
