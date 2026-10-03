"""Credentials users keep for themselves: their own provider keys and subscription logins.

A secret is sealed with Fernet (AES-128-CBC with an HMAC, from ``cryptography``)
under the operator's ``GRID_SECRETS_KEY`` before it reaches the database, and
opened only when a request is about to be signed with it. Several keys, comma
separated, are accepted: the first seals, any opens, so a key is rotated by
putting a new one in front. The sealed payload names its owner and kind, so a row
copied to another user's id does not open.

Nothing here is ever sent back to a browser: the interface learns only that a
credential exists, its last four characters and whether it still works.
Without ``GRID_SECRETS_KEY`` the vault is off - the server runs, users simply
cannot store credentials - rather than keeping them in the clear.

Subscription logins are OAuth tokens that expire. Their *kind* registers an
:class:`OAuthKind` that turns the stored state into a credential, refreshing it
first when needed; the refreshed state is stored back. Refreshing is serialised
per user and kind, as a refresh token is typically good for one use.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core.credentials import PROVIDER_PRESETS, Credential
from web_chat.accounts.store import CredentialRow

logger = logging.getLogger(__name__)

SECRETS_KEY = "GRID_SECRETS_KEY"
NEEDS_REAUTH = "needs_reauth"


class ReauthRequired(Exception):
    """A stored login can no longer be refreshed: the user must sign in again."""


class VaultUnavailable(RuntimeError):
    """The operator has not set GRID_SECRETS_KEY."""


class Cipher:
    def __init__(self, keys: list[str]) -> None:
        self._fernet = MultiFernet([Fernet(key.encode()) for key in keys])

    @classmethod
    def from_environment(cls, environ: Optional[Mapping[str, str]] = None) -> Optional["Cipher"]:
        """The cipher of ``GRID_SECRETS_KEY``, or None when it is unset. A malformed key is an error."""
        raw = (environ if environ is not None else os.environ).get(SECRETS_KEY, "")
        keys = [part.strip() for part in raw.split(",") if part.strip()]
        if not keys:
            return None
        try:
            return cls(keys)
        except (ValueError, TypeError) as exc:
            raise VaultUnavailable(
                f"{SECRETS_KEY} is not a valid key. Make one with: python -m web_chat.vault new-key"
            ) from exc

    @classmethod
    def local_file(cls, path: Path) -> "Cipher":
        """The cipher of a key kept in *path* (owner-only), made on first use.

        For a server of one person on their own machine, where there is no
        operator to set GRID_SECRETS_KEY: the secrets are as safe as that file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as out:
                out.write(cls.new_key())
        return cls([path.read_text().strip()])

    @staticmethod
    def new_key() -> str:
        return Fernet.generate_key().decode()

    def seal(self, payload: Mapping[str, Any], binding: str) -> bytes:
        return self._fernet.encrypt(json.dumps({"for": binding, **payload}).encode())

    def open(self, token: bytes, binding: str) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(self._fernet.decrypt(token))
        except (InvalidToken, ValueError):
            return None
        if not isinstance(payload, dict) or payload.pop("for", None) != binding:
            return None
        return payload


class CredentialStore(Protocol):
    """Where sealed credentials are kept: the accounts database, or a file for one user."""

    def put_credential(self, user_id: str, kind: str, ciphertext: bytes, hint: str, now: float) -> None: ...

    def credential(self, user_id: str, kind: str) -> Optional[CredentialRow]: ...

    def credentials_of(self, user_id: str) -> list[CredentialRow]: ...

    def set_credential_status(self, user_id: str, kind: str, status: str) -> None: ...

    def delete_credential(self, user_id: str, kind: str) -> bool: ...


class FileCredentialStore:
    """Sealed credentials in one JSON file, written atomically and owner-only."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _save(self, rows: Dict[str, Dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self._path.parent, prefix=self._path.name, suffix=".tmp")
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(rows, out)
            os.replace(temporary, self._path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def _row(key: str, value: Dict[str, Any]) -> CredentialRow:
        user_id, _, kind = key.partition("|")
        return CredentialRow(
            user_id, kind, base64.b64decode(value["ciphertext"]), value["hint"], value["status"],
            value["created_at"], value["updated_at"],
        )

    def put_credential(self, user_id: str, kind: str, ciphertext: bytes, hint: str, now: float) -> None:
        with self._lock:
            rows = self._load()
            key = f"{user_id}|{kind}"
            created = rows.get(key, {}).get("created_at", now)
            rows[key] = {
                "ciphertext": base64.b64encode(ciphertext).decode(), "hint": hint, "status": "ok",
                "created_at": created, "updated_at": now,
            }
            self._save(rows)

    def credential(self, user_id: str, kind: str) -> Optional[CredentialRow]:
        with self._lock:
            value = self._load().get(f"{user_id}|{kind}")
            return self._row(f"{user_id}|{kind}", value) if value else None

    def credentials_of(self, user_id: str) -> list[CredentialRow]:
        with self._lock:
            return [self._row(key, value) for key, value in self._load().items() if key.startswith(f"{user_id}|")]

    def set_credential_status(self, user_id: str, kind: str, status: str) -> None:
        with self._lock:
            rows = self._load()
            if f"{user_id}|{kind}" in rows:
                rows[f"{user_id}|{kind}"]["status"] = status
                self._save(rows)

    def delete_credential(self, user_id: str, kind: str) -> bool:
        with self._lock:
            rows = self._load()
            if rows.pop(f"{user_id}|{kind}", None) is None:
                return False
            self._save(rows)
            return True


class OAuthKind(Protocol):
    """How one kind of subscription login becomes a credential."""

    async def credential(self, state: Dict[str, Any], now: float) -> tuple[Credential, Optional[Dict[str, Any]]]:
        """The credential for *state*, and the new state when it had to be refreshed.

        Raises ReauthRequired when the login is gone for good.
        """


def _binding(user_id: str, kind: str) -> str:
    return f"{user_id}|{kind}"


class Vault:
    """A user's stored credentials; implements entitlements.OwnCredentials."""

    def __init__(
        self,
        store: CredentialStore,
        cipher: Optional[Cipher],
        *,
        oauth: Optional[Mapping[str, OAuthKind]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._cipher = cipher
        self._oauth = dict(oauth or {})
        self._clock = clock
        self._locks: Dict[tuple[str, str], asyncio.Lock] = {}

    @property
    def enabled(self) -> bool:
        return self._cipher is not None

    def _need(self) -> Cipher:
        if self._cipher is None:
            raise VaultUnavailable(f"This server cannot store credentials: {SECRETS_KEY} is not set.")
        return self._cipher

    # -- writing -----------------------------------------------------------------
    def put_api_key(self, user_id: str, kind: str, secret: str) -> str:
        """Store the user's key for provider preset *kind*; the hint shown for it."""
        if kind not in PROVIDER_PRESETS:
            raise ValueError(f"No provider preset named {kind!r}.")
        hint = "…" + secret[-4:] if len(secret) >= 12 else "…"
        sealed = self._need().seal({"type": "api_key", "secret": secret}, _binding(user_id, kind))
        self._store.put_credential(user_id, kind, sealed, hint, self._clock())
        return hint

    def put_state(self, user_id: str, kind: str, state: Mapping[str, Any], hint: str = "") -> None:
        """Store the state of a subscription login (tokens, expiry, account)."""
        sealed = self._need().seal({"type": "oauth", **state}, _binding(user_id, kind))
        self._store.put_credential(user_id, kind, sealed, hint, self._clock())

    def delete(self, user_id: str, kind: str) -> bool:
        return self._store.delete_credential(user_id, kind)

    def state(self, user_id: str, kind: str) -> Optional[Dict[str, Any]]:
        """The opened state of a subscription login, to revoke it on sign-out; None if absent."""
        row = self._store.credential(user_id, kind)
        if row is None or self._cipher is None:
            return None
        payload = self._cipher.open(row.ciphertext, _binding(user_id, kind))
        if payload is None or payload.pop("type", None) != "oauth":
            return None
        return payload

    # -- reading -----------------------------------------------------------------
    def listing(self, user_id: str) -> list[dict[str, Any]]:
        """What the interface may know: kind, hint, status, when - never a secret."""
        return [
            {"kind": row.kind, "hint": row.hint, "status": row.status, "updated_at": row.updated_at}
            for row in self._store.credentials_of(user_id)
        ]

    def has(self, user_id: str, kind: str) -> bool:
        row = self._store.credential(user_id, kind)
        return row is not None and row.status == "ok"

    async def credential(self, user_id: str, kind: str) -> Optional[Credential]:
        """The credential to sign a request with now, or None when it cannot be used."""
        row = self._store.credential(user_id, kind)
        if row is None or row.status != "ok" or self._cipher is None:
            return None
        payload = self._cipher.open(row.ciphertext, _binding(user_id, kind))
        if payload is None:
            logger.error("A stored credential of %s could not be opened (wrong GRID_SECRETS_KEY?)", kind)
            return None
        if payload.get("type") == "api_key":
            return Credential(payload["secret"], "own", charged=False)
        handler = self._oauth.get(kind)
        if handler is None:
            return None
        async with self._locks.setdefault((user_id, kind), asyncio.Lock()):
            # Another request may have refreshed while this one waited.
            row = self._store.credential(user_id, kind)
            if row is None or row.status != "ok":
                return None
            payload = self._cipher.open(row.ciphertext, _binding(user_id, kind)) or payload
            payload.pop("type", None)
            try:
                found, renewed = await handler.credential(payload, self._clock())
            except ReauthRequired:
                self._store.set_credential_status(user_id, kind, NEEDS_REAUTH)
                return None
            if renewed is not None:
                self.put_state(user_id, kind, renewed, row.hint)
            return found


def local_vault(records: Path, oauth: Optional[Mapping[str, OAuthKind]] = None) -> Vault:
    """The vault of a server of one person: files beside their other records, key included."""
    return Vault(
        FileCredentialStore(records / "credentials.json"), Cipher.local_file(records / "secrets.key"), oauth=oauth
    )


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m web_chat.vault", description="Secrets key for users' stored credentials")
    parser.add_argument("command", choices=("new-key",), help="Print a new key to put in GRID_SECRETS_KEY")
    parser.parse_args(argv)
    print(Cipher.new_key())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
