"""The channel's owner in the server: it can be turned on and off without a restart.

The admin sets a bot token in the web interface; the channel has to appear,
and to go away again, without the server being restarted. So the server holds
this manager, not a channel: the manager reads the token (the environment
first, the vault after it), builds the channel, starts its long poll, and on a
new token stops the old channel - its poller, reminders, bot client - and
starts a fresh one. The web routes live beside it and are always there: with
the channel off they say so.

The manager is the only place that knows where the token comes from, so
``/api/admin/telegram`` shows the source and a hint, never the token.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

from web_chat.errors import error_text

from .bot import Bot, BotError
from .config import TOKEN_ENV, ConfigError, Settings, looks_like_token
from .secrets import TelegramSecrets
from .service import TelegramChannel, TelegramDisabled
from .store import TelegramStore

logger = logging.getLogger("grid.web_chat.telegram")


class TelegramManager:
    """Owns the channel: builds it from the token, starts and stops it at runtime."""

    def __init__(
        self,
        path: Path | str,
        spaces: Any,
        *,
        vault: Any | None = None,
        allowed: Callable[[str], bool] = lambda user_id: True,
        voice_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] | None = None,
        bot_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.path = path
        self.spaces = spaces
        self.secrets = TelegramSecrets(vault)
        self._allowed = allowed
        self._voice_factory = voice_factory
        self._clock = clock
        self._bot_factory = bot_factory or (lambda token: Bot(token))
        self._channel: Optional[TelegramChannel] = None
        self._settings: Optional[Settings] = None
        self._error = ""
        self._lock = asyncio.Lock()

    # -- what the server and the routes ask ----------------------------------
    @property
    def channel(self) -> Optional[TelegramChannel]:
        """The running channel, or None when the channel is off."""
        return self._channel

    @property
    def settings(self) -> Optional[Settings]:
        """The settings the channel was last resolved with; None before the first start."""
        return self._settings

    @property
    def enabled(self) -> bool:
        return self._channel is not None

    def resolve(self) -> Settings:
        """The settings the environment and the vault add up to (may raise ConfigError)."""
        return Settings.from_sources(self.secrets.token())

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        """Resolve the token and run the channel if it is on; a no-op when it is off."""
        async with self._lock:
            await self._stop_locked()
            try:
                settings = self.resolve()
            except ConfigError as exc:
                # As before: a server told to run the channel without a token
                # does not start half-configured.
                self._error = error_text(exc)
                raise
            self._settings = settings
            self._error = ""
            if not settings.enabled:
                logger.info("Telegram channel is off (no token, or not enabled)")
                return
            self._channel = self._build(settings)
            try:
                await self._channel.start()
            except Exception as exc:  # noqa: BLE001 - the server still serves the pages
                self._error = f"The Telegram channel could not start: {type(exc).__name__}"
                logger.warning("The Telegram channel could not start: %s", type(exc).__name__)
                await self._stop_locked()
                return
            # Said once, so an operator can tell from the log that the bot is up.
            username = await self._channel.bot_username()
            logger.info("Telegram channel is on%s", f" as @{username}" if username else " (Telegram did not name the bot)")

    async def close(self) -> None:
        """Stop the channel and forget it; the server is going away."""
        async with self._lock:
            await self._stop_locked()

    async def reconfigure(self) -> None:
        """Start the channel again from the token there is now (used after a change)."""
        await self.start()

    # -- what the admin's page asks ------------------------------------------
    async def apply_token(self, token: str) -> dict[str, Any]:
        """Seal *token*, check it with Telegram, then run the channel on it."""
        token = str(token or "").strip()
        if not self.secrets.available:
            raise TelegramDisabled(
                "This server cannot store a bot token: GRID_SECRETS_KEY is not set."
            )
        await self._verify(token)
        self.secrets.store(token)
        await self.reconfigure()
        return await self.status()

    async def forget_token(self) -> dict[str, Any]:
        """Drop the stored token; the channel stops unless the environment has one."""
        self.secrets.forget()
        await self.reconfigure()
        return await self.status()

    async def status(self) -> dict[str, Any]:
        """What the admin's page shows: on or off, from where, and a hint."""
        source = self.secrets.source()
        return {
            "enabled": self.enabled,
            "source": source,
            "from_env": source == "env",
            "hint": self._hint(source),
            "can_store": self.secrets.available,
            "error": self._error,
            "bot_username": await self.bot_username(),
        }

    async def bot_username(self) -> str:
        """The bot's @username, "" when it is off or Telegram does not answer."""
        channel = self._channel
        return await channel.bot_username() if channel is not None else ""

    # -- internals -----------------------------------------------------------
    def _hint(self, source: Optional[str]) -> str:
        if source == "env":
            token = str(os.environ.get(TOKEN_ENV) or "").strip()
            return "…" + token[-5:] if token else ""
        return self.secrets.hint()

    def _build(self, settings: Settings) -> TelegramChannel:
        kwargs: dict[str, Any] = {
            "allowed": self._allowed,
            "voice_factory": self._voice_factory,
        }
        if self._clock is not None:
            kwargs["clock"] = self._clock
        return TelegramChannel(
            TelegramStore(self.path),
            self.spaces,
            settings,
            bot=self._bot_factory(settings.token),
            **kwargs,
        )

    async def _stop_locked(self) -> None:
        channel, self._channel = self._channel, None
        if channel is not None:
            try:
                await channel.close()
            except Exception:  # noqa: BLE001 - a half-closed channel is still gone
                logger.warning("The Telegram channel did not close cleanly")

    async def _verify(self, token: str) -> None:
        """Ask Telegram who the bot is, so a wrong token is refused here, not later."""
        if not looks_like_token(token):
            raise TelegramDisabled("That does not look like a bot token.")
        bot = self._bot_factory(token)
        try:
            get_me = getattr(bot, "get_me", None)
            if get_me is not None:
                await get_me()
        except Exception as exc:  # noqa: BLE001 - Telegram's own reason, never the token
            # BotError carries Telegram's description, which names no token.
            detail = str(exc) if isinstance(exc, BotError) else type(exc).__name__
            raise TelegramDisabled(f"Telegram refused the token: {detail}") from None
        finally:
            close = getattr(bot, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - the client is being thrown away
                    pass
