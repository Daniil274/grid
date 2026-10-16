"""A user's own devices: paired once with a code, then known by a token.

The user asks the web chat for a pairing code; the node on their PC or phone
(grid_node) trades the code - once, within ``CODE_SECONDS`` - for a device
id and a token it keeps, and connects with that token from then on. Only
hashes of codes and tokens are stored. Revoking a device ends its token.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

#: Seconds a pairing code can be redeemed.
CODE_SECONDS = 600
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PairedDevice:
    id: str
    user_id: str
    name: str
    kind: str
    created_at: float
    last_seen: Optional[float]


class PairingStore:
    """Paired devices and pending codes, in one SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, last_seen REAL, revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS codes (
                    code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def new_code(self, user_id: str) -> Tuple[str, float]:
        """A one-time code for *user_id* to pair a device with, and when it expires."""
        code = "".join(secrets.choice(_ALPHABET) for _ in range(10))
        expires = time.time() + CODE_SECONDS
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM codes WHERE expires_at < ?", (time.time(),))
            db.execute("INSERT INTO codes VALUES (?, ?, ?)", (_hash(code), user_id, expires))
        return code, expires

    def redeem(self, code: str, name: str, kind: str) -> Optional[Tuple[PairedDevice, str]]:
        """The device a valid *code* pairs and its token; None for a wrong or used code."""
        code = (code or "").strip().upper().replace("-", "")
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT user_id FROM codes WHERE code_hash = ? AND expires_at >= ?", (_hash(code), time.time())
            ).fetchone()
            db.execute("DELETE FROM codes WHERE code_hash = ?", (_hash(code),))
            if row is None:
                return None
            device = PairedDevice(
                id=f"{kind[:10]}-{secrets.token_hex(4)}", user_id=row[0], name=(name or kind)[:80],
                kind=kind[:20], created_at=time.time(), last_seen=None,
            )
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (device.id, device.user_id, device.name, device.kind, _hash(token), device.created_at),
            )
        return device, token

    def verify(self, token: str) -> Optional[PairedDevice]:
        """The device *token* belongs to, unless it was revoked; it is seen now."""
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT id, user_id, name, kind, created_at FROM devices WHERE token_hash = ? AND revoked_at IS NULL",
                (_hash(token or ""),),
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now, row[0]))
        return PairedDevice(*row, last_seen=now)

    def devices(self, user_id: str) -> List[PairedDevice]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT id, user_id, name, kind, created_at, last_seen FROM devices "
                "WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return [PairedDevice(*row) for row in rows]

    def revoke(self, user_id: str, device_id: str) -> bool:
        with self._lock, self._connect() as db:
            changed = db.execute(
                "UPDATE devices SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                (time.time(), device_id, user_id),
            ).rowcount
        return bool(changed)
