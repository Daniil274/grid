"""Accounts: who may sign in, for how long, and who may admit whom.

The rules, all in one place:

- A user joins only with an invite, which an admin makes (or the operator,
  from the command line: ``grid-web-chat accounts``). The invite fixes the new
  user's role, expires, and admits one person: spending it and creating the
  user are one transaction.
- Signing in gives a session token, kept by the browser in a cookie. A
  session lasts :data:`SESSION_IDLE` past its last use and never more than
  :data:`SESSION_MAX_AGE` in all. Signing out, a password change, disabling the
  user or a role change ends sessions at once: the next request finds none.
- A failed sign-in says only that the name or the password is wrong, and a
  missing user costs the same time as a wrong password.
- The server always keeps an active admin: the last one cannot be disabled or
  demoted.

Every method is synchronous and quick apart from password hashing (tens of
milliseconds of CPU); the HTTP layer runs those off the event loop.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from web_chat.accounts import passwords, tokens
from web_chat.accounts.store import AccountStore, InviteRecord, UserRecord
from web_chat.identity import ROLES, Role, User

SESSION_IDLE = 14 * 24 * 3600
SESSION_MAX_AGE = 90 * 24 * 3600
#: A session's expiry moves on at most this often, so reads rarely write.
SESSION_TOUCH_INTERVAL = 3600
INVITE_TTL = 7 * 24 * 3600
MAX_INVITE_TTL = 90 * 24 * 3600

USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}")


class AccountError(ValueError):
    """A request the rules refuse; the message is safe to show the user."""


class SignInFailed(AccountError):
    pass


@dataclass(frozen=True)
class Invite:
    """An invite as an admin sees it; the code itself is shown only once."""

    id: str
    role: Role
    note: str
    created_at: float
    expires_at: float
    used_by: Optional[str]
    used_at: Optional[float]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "note": self.note,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "used_by": self.used_by,
            "used_at": self.used_at,
        }


@dataclass(frozen=True)
class Account:
    """A user as an admin sees it."""

    user: User
    disabled: bool
    created_at: float
    turns_today: int = 0
    tokens_today: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.user.id,
            "username": self.user.username,
            "role": self.user.role,
            "disabled": self.disabled,
            "created_at": self.created_at,
            "turns_today": self.turns_today,
            "tokens_today": self.tokens_today,
        }


def check_username(username: str) -> str:
    """The username as stored, or AccountError saying what is wrong with it."""
    if not USERNAME.fullmatch(username):
        raise AccountError(
            "A username is 3-32 characters: letters, digits, '_', '.' or '-', starting with a letter or digit."
        )
    return username


class Accounts:
    """The accounts service over an :class:`AccountStore`."""

    def __init__(self, store: AccountStore, *, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock

    # -- users ---------------------------------------------------------------------
    def create_user(self, username: str, password: str, role: Role = "user") -> User:
        """Create a user directly: the operator's bootstrap and tests."""
        password_hash = self._new_password_hash(username, password)
        with self._store.transaction():
            return self._add_user(username, password_hash, role)

    @staticmethod
    def _new_password_hash(username: str, password: str) -> str:
        """Check a new user's name and password, then hash - outside any transaction,
        so the slow hash never holds the database."""
        passwords.check_strength(password, username=check_username(username))
        return passwords.hash_password(password)

    def _add_user(self, username: str, password_hash: str, role: Role) -> User:
        if role not in ROLES:
            raise AccountError(f"Unknown role {role!r}.")
        user = User(id=uuid.uuid4().hex, username=check_username(username), role=role)
        try:
            self._store.add_user(user, password_hash, self._clock())
        except sqlite3.IntegrityError:
            raise AccountError("That username is taken.") from None
        return user

    def find_user(self, username: str) -> Optional[User]:
        """The user called *username* (any case), or None."""
        record = self._store.user_by_name(username) if USERNAME.fullmatch(username) else None
        return record.user if record else None

    def user(self, user_id: str) -> Optional[User]:
        """The user with id *user_id*, or None."""
        record = self._store.user_by_id(user_id)
        return record.user if record else None

    def has_users(self) -> bool:
        return self._store.count_users() > 0

    def accounts(self) -> list[Account]:
        day = self._today()
        turns = self._store.turns_by_user(day)
        tokens = self._store.tokens_by_user(day)
        return [
            Account(r.user, r.disabled, r.created_at, turns.get(r.user.id, 0), tokens.get(r.user.id, 0))
            for r in self._store.users()
        ]

    # -- usage ---------------------------------------------------------------------
    def count_turn(self, user_id: str, limit: Optional[int]) -> bool:
        """Count a turn the user starts now, unless the day's *limit* is reached;
        whether it was counted (and may start). Days are UTC."""
        return self._store.count_turn(user_id, self._today(), limit)

    def turns_today(self, user_id: str) -> int:
        return self._store.turns_on(user_id, self._today())

    def count_tokens(self, user_id: str, tokens_in: int, tokens_out: int) -> None:
        """Add a finished turn's tokens (input and output) to the user's day."""
        self._store.add_tokens(user_id, self._today(), tokens_in, tokens_out)

    def tokens_today(self, user_id: str) -> int:
        return self._store.tokens_on(user_id, self._today())

    def usage_today(self, user_id: str) -> dict[str, int]:
        """The user's day so far: turns started and tokens spent (input, output)."""
        turns, tokens_in, tokens_out = self._store.usage_on(user_id, self._today())
        return {"turns": turns, "tokens_in": tokens_in, "tokens_out": tokens_out}

    def usage_totals(self, user_id: str) -> dict[str, int]:
        """The user's all-time turns started and tokens spent (input, output)."""
        turns, tokens_in, tokens_out = self._store.usage_totals(user_id)
        return {"turns": turns, "tokens_in": tokens_in, "tokens_out": tokens_out}

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).date().isoformat()

    def set_disabled(self, actor: User, user_id: str, disabled: bool) -> None:
        """Disable or re-enable a user; disabling ends the user's sessions."""
        if disabled and user_id == actor.id:
            raise AccountError("You cannot disable your own account.")
        with self._store.transaction():
            record = self._existing(user_id)
            if disabled and record.user.is_admin and not record.disabled:
                self._keep_an_admin()
            self._store.set_disabled(user_id, disabled)
            if disabled:
                self._store.delete_sessions_of(user_id)

    def set_role(self, actor: User, user_id: str, role: Role) -> None:
        """Change a user's role; the user signs in again with it."""
        if role not in ROLES:
            raise AccountError(f"Unknown role {role!r}.")
        with self._store.transaction():
            record = self._existing(user_id)
            if record.user.role == role:
                return
            if record.user.is_admin and not record.disabled:
                self._keep_an_admin()
            self._store.set_role(user_id, role)
            self._store.delete_sessions_of(user_id)

    def _keep_an_admin(self) -> None:
        if self._store.count_active_admins() <= 1:
            raise AccountError("The server needs at least one active admin.")

    def _existing(self, user_id: str) -> UserRecord:
        record = self._store.user_by_id(user_id)
        if record is None:
            raise AccountError("No such user.")
        return record

    # -- invites -------------------------------------------------------------------
    def create_invite(
        self, created_by: Optional[User], *, role: Role = "user", ttl: float = INVITE_TTL, note: str = ""
    ) -> tuple[str, Invite]:
        """A new invite: (the code to hand over, the invite as listed)."""
        if role not in ROLES:
            raise AccountError(f"Unknown role {role!r}.")
        if not 0 < ttl <= MAX_INVITE_TTL:
            raise AccountError(f"An invite lasts between a second and {MAX_INVITE_TTL // 86400} days.")
        now = self._clock()
        record = InviteRecord(
            id=uuid.uuid4().hex,
            role=role,
            note=note.strip()[:200],
            created_by=created_by.id if created_by else None,
            created_at=now,
            expires_at=now + ttl,
            used_by=None,
            used_at=None,
        )
        code = tokens.new_secret()
        self._store.add_invite(record, tokens.digest(code))
        return code, _invite(record)

    def invites(self) -> list[Invite]:
        return [_invite(record) for record in self._store.invites()]

    def revoke_invite(self, invite_id: str) -> bool:
        """Delete an unused invite; False when there is none by that id."""
        return self._store.delete_invite(invite_id)

    def register(self, code: str, username: str, password: str) -> User:
        """Join with an invite: the user is created and the invite spent together."""
        password_hash = self._new_password_hash(username, password)
        with self._store.transaction():
            invite = self._store.invite_by_code(tokens.digest(code))
            if invite is None or invite.used_at is not None or invite.expires_at <= self._clock():
                raise AccountError("This invite is not valid. Ask for a new one.")
            user = self._add_user(username, password_hash, invite.role)
            if not self._store.mark_invite_used(invite.id, user.id, self._clock()):
                raise AccountError("This invite is not valid. Ask for a new one.")
            return user

    # -- sessions ------------------------------------------------------------------
    def sign_in(self, username: str, password: str) -> tuple[User, str]:
        """(the user, a new session token), or SignInFailed."""
        record = self._store.user_by_name(username) if USERNAME.fullmatch(username) else None
        # A missing user is checked against a dummy hash: same cost, same answer.
        matched = passwords.verify_password(password, record.password_hash if record else passwords.dummy_hash())
        if record is None or not matched:
            raise SignInFailed("Wrong username or password.")
        if record.disabled:
            raise SignInFailed("This account is disabled.")
        if passwords.needs_rehash(record.password_hash):
            self._store.set_password_hash(record.user.id, passwords.hash_password(password))
        return record.user, self.open_session(record.user.id)

    def open_session(self, user_id: str) -> str:
        """A new session token for *user_id*: after sign-in or sign-up."""
        token = tokens.new_secret()
        now = self._clock()
        self._store.add_session(tokens.digest(token), user_id, now, now + SESSION_IDLE)
        return token

    def user_for_session(self, token: str) -> Optional[User]:
        """The signed-in user of *token*, or None when it opens no session."""
        token_hash = tokens.digest(token)
        session = self._store.session(token_hash)
        now = self._clock()
        if session is None:
            return None
        if session.expires_at <= now:
            self._store.delete_session(token_hash)
            return None
        record = self._store.user_by_id(session.user_id)
        if record is None or record.disabled:
            return None
        if now - session.last_seen >= SESSION_TOUCH_INTERVAL:
            expires_at = min(now + SESSION_IDLE, session.created_at + SESSION_MAX_AGE)
            self._store.touch_session(token_hash, now, expires_at)
        return record.user

    def sign_out(self, token: str) -> None:
        self._store.delete_session(tokens.digest(token))

    def change_password(self, user: User, current: str, new: str, *, token: str) -> None:
        """Set a new password; every other session of the user ends."""
        record = self._existing(user.id)
        if not passwords.verify_password(current, record.password_hash):
            raise AccountError("The current password is wrong.")
        passwords.check_strength(new, username=record.user.username)
        new_hash = passwords.hash_password(new)
        with self._store.transaction():
            self._store.set_password_hash(user.id, new_hash)
            self._store.delete_sessions_of(user.id, except_hash=tokens.digest(token))

    def purge_expired_sessions(self) -> int:
        return self._store.delete_expired_sessions(self._clock())


def _invite(record: InviteRecord) -> Invite:
    return Invite(
        id=record.id,
        role=record.role,
        note=record.note,
        created_at=record.created_at,
        expires_at=record.expires_at,
        used_by=record.used_by,
        used_at=record.used_at,
    )
