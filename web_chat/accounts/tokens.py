"""Secrets handed to a browser or a person: session tokens and invite codes.

Each is 32 random bytes, URL-safe. Only its SHA-256 is stored, so the database
alone - a backup, a copied file - opens no session and redeems no invite. A
plain hash is enough: the secret is random, not a password, so there is
nothing to guess.
"""

from __future__ import annotations

import hashlib
import secrets

TOKEN_BYTES = 32


def new_secret() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def digest(secret: str) -> str:
    """What the database keeps of *secret*."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()
