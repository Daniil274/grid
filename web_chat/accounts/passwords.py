"""Password hashing with scrypt, and the rules a new password must meet.

scrypt is memory-hard, so guessing costs memory as well as time, and it ships
with Python (``hashlib.scrypt``): no native dependency to build or audit.

A stored hash names everything needed to check it::

    scrypt$<log2 n>$<r>$<p>$<salt, base64>$<key, base64>

so the cost can be raised later: old hashes still verify with the parameters
they were made with, and :func:`needs_rehash` tells the caller to store a
stronger one at the next successful sign-in.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets

#: scrypt cost: n = 2**15, r = 8 takes ~32 MiB and tens of milliseconds per hash.
LOG2_N = 15
R = 8
P = 1
KEY_BYTES = 32
SALT_BYTES = 16
#: hashlib's default memory cap (32 MiB) is exactly the need of n=2**15, r=8.
_MAXMEM = 64 * 1024 * 1024

MIN_LENGTH = 10
#: Hashing cost grows with length; a cap keeps a request from buying minutes.
MAX_LENGTH = 256

_SCHEME = "scrypt"


class WeakPassword(ValueError):
    """The password breaks a rule; the message says which, for the user."""


def check_strength(password: str, *, username: str = "") -> None:
    """Raise :class:`WeakPassword` unless *password* may be set."""
    if len(password) < MIN_LENGTH:
        raise WeakPassword(f"The password must be at least {MIN_LENGTH} characters long.")
    if len(password) > MAX_LENGTH:
        raise WeakPassword(f"The password must be at most {MAX_LENGTH} characters long.")
    if password.strip() != password or not password.strip():
        raise WeakPassword("The password must not start or end with spaces.")
    if username and password.casefold() == username.casefold():
        raise WeakPassword("The password must differ from the username.")
    if len(set(password)) < 4:
        raise WeakPassword("The password needs more than three different characters.")


def hash_password(password: str) -> str:
    """A new salted hash of *password* at the current cost."""
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, LOG2_N, R, P)
    return "$".join((_SCHEME, str(LOG2_N), str(R), str(P), _b64(salt), _b64(key)))


def verify_password(password: str, stored: str) -> bool:
    """Whether *password* matches *stored*; a malformed hash never matches."""
    parsed = _parse(stored)
    if parsed is None or len(password) > MAX_LENGTH:
        return False
    log2_n, r, p, salt, key = parsed
    return hmac.compare_digest(_derive(password, salt, log2_n, r, p), key)


def needs_rehash(stored: str) -> bool:
    """True when *stored* was made at another cost than the current one."""
    parsed = _parse(stored)
    return parsed is None or parsed[:3] != (LOG2_N, R, P)


@functools.lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A valid hash of a random password, made once: checked against when the
    user does not exist, so "no such user" costs the same as "wrong password"."""
    return hash_password(secrets.token_urlsafe(24))


def _derive(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**log2_n, r=r, p=p, maxmem=_MAXMEM, dklen=KEY_BYTES
    )


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _SCHEME:
        return None
    try:
        log2_n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, key = base64.b64decode(parts[4], validate=True), base64.b64decode(parts[5], validate=True)
    except ValueError:
        return None
    # Bounds keep a tampered row from asking for gigabytes.
    if not (10 <= log2_n <= 20 and 1 <= r <= 16 and 1 <= p <= 4 and salt and key):
        return None
    if 128 * r * 2**log2_n > _MAXMEM // 2:  # scrypt's own need: 128 * r * n bytes
        return None
    return log2_n, r, p, salt, key


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
