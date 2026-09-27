"""User accounts of a multi-user web chat server.

- ``passwords``: scrypt hashing and the rules for a new password
- ``tokens``: session tokens and invite codes, stored only as hashes
- ``store``: the SQLite database of users, sessions and invites
- ``service``: the rules - invites, sessions, roles, the last admin
- ``limits``: slowing down password guessing
- ``http``: the session cookie, the auth routes and the admin API
- ``cli``: the operator's commands (first admin, invites)
"""

from __future__ import annotations

from pathlib import Path

from web_chat.accounts.service import Accounts
from web_chat.accounts.store import AccountStore

DATABASE = "accounts.db"


def open_accounts(data_dir: Path) -> Accounts:
    """The accounts of the server whose data lives in *data_dir*."""
    return Accounts(AccountStore(data_dir / DATABASE))
