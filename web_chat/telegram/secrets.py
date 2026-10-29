"""The bot token the admin sets in the web interface: sealed in the server's vault.

The token is the one credential of the channel itself, not of a user, so it is
sealed under a reserved owner and kind. :class:`web_chat.vault.Vault` speaks of
"state" for the sealed mappings of subscription logins; that is the generic
sealed form it offers, and a bot token is stored as its one field. Nothing here
returns the token to a caller that could put it on the wire: the interface gets
:meth:`TelegramSecrets.hint` - the last five characters - and the channel gets
the token to put in its Bot API calls.

Without ``GRID_SECRETS_KEY`` the vault is off; then the channel can still run
on the token in the environment, the page simply cannot set one.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .config import TOKEN_ENV, looks_like_token

logger = logging.getLogger("grid.web_chat.telegram")

#: The reserved owner of the server's own secrets in the users' vault.
OWNER = "server"
#: The kind of the bot token there.
KIND = "telegram_bot"
#: How much of the token a hint shows: enough to tell two bots apart.
HINT_CHARS = 5


class TelegramSecrets:
    """The bot token as the vault keeps it: sealed, hinted, never printed."""

    def __init__(self, vault: Any | None) -> None:
        self._vault = vault

    @property
    def available(self) -> bool:
        """Whether this server can store a token at all (its vault is open)."""
        return self._vault is not None and bool(getattr(self._vault, "enabled", False))

    def token(self) -> str:
        """The stored token, or "" when there is none (or it no longer opens)."""
        if not self.available:
            return ""
        try:
            state = self._vault.state(OWNER, KIND) or {}
        except Exception:  # noqa: BLE001 - an unreadable vault is no token
            logger.warning("The stored Telegram bot token could not be read")
            return ""
        return str(state.get("token") or "").strip()

    def source(self) -> Optional[str]:
        """Where the token the channel runs with comes from: "env", "vault" or None."""
        if str(os.environ.get(TOKEN_ENV) or "").strip():
            return "env"
        return "vault" if self.token() else None

    def hint(self) -> str:
        """``…5kfWw``: the tail of the stored token, never the token."""
        token = self.token()
        return "…" + token[-HINT_CHARS:] if token else ""

    def store(self, token: str) -> str:
        """Seal *token* in the vault; the hint the interface shows for it."""
        token = str(token or "").strip()
        if not looks_like_token(token):
            raise ValueError("That does not look like a bot token.")
        if not self.available:
            raise RuntimeError(
                "This server cannot store a bot token: GRID_SECRETS_KEY is not set."
            )
        self._vault.put_state(OWNER, KIND, {"token": token}, "…" + token[-HINT_CHARS:])
        return "…" + token[-HINT_CHARS:]

    def forget(self) -> bool:
        """Drop the stored token; False when there was none."""
        if self._vault is None:
            return False
        return bool(self._vault.delete(OWNER, KIND))
