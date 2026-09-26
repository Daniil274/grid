"""The operator's account commands: ``grid-web-chat accounts <command>``.

    create-admin --username NAME   the first admin (or another one); asks for the password
    invite [--role user] [--days 7] [--note TEXT]   prints a sign-up link's code
    list                           the users and the open invites

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
    invite.add_argument("--days", type=float, default=7.0, help="How long the invite stays valid")
    invite.add_argument("--note", default="", help="Who the invite is for, for the list")

    commands.add_parser("list", help="List users and open invites")

    args = parser.parse_args(argv)
    accounts = open_accounts(args.data_dir)
    try:
        if args.command == "create-admin":
            return _create_admin(accounts, args.username)
        if args.command == "invite":
            code, created = accounts.create_invite(None, role=args.role, ttl=args.days * 86400, note=args.note)
            print(f"Invite for a new {created.role}, valid until {_when(created.expires_at)}:")
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


def _list(accounts: Accounts) -> None:
    print("Users:")
    for account in accounts.accounts():
        state = "disabled" if account.disabled else "active"
        print(f"  {account.user.username:<32} {account.user.role:<6} {state:<9} since {_when(account.created_at)}")
    print("Open invites:")
    for invite in accounts.invites():
        if invite.used_at is None:
            print(f"  {invite.id}  {invite.role:<6} until {_when(invite.expires_at)}  {invite.note}")


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
