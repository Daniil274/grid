"""Settings of the Telegram channel: off until a bot token says on.

The token comes from the environment (``GRID_TELEGRAM_BOT_TOKEN``) or, when
that is empty, from the server's vault - the admin sets it in the web
interface, it is stored sealed (``web_chat/telegram/secrets.py``) and never
written to a config file, to a log or back to a browser. The environment wins:
a token there is used whatever the vault holds, and a token there without
``GRID_TELEGRAM_ENABLED=1`` keeps the channel off, as it always has.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

#: Where the bot token lives. Nothing else reads it.
TOKEN_ENV = "GRID_TELEGRAM_BOT_TOKEN"
#: ``1`` turns the channel on; without a token it refuses to start.
ENABLED_ENV = "GRID_TELEGRAM_ENABLED"
#: Voice replies are opt-in: they need speech synthesis and ffmpeg.
VOICE_ENV = "GRID_TELEGRAM_VOICE_REPLIES"

API_ROOT = "https://api.telegram.org"
#: A link code is good for ten minutes and for one use.
LINK_TTL = 600.0
#: How long the long poll holds a request open.
POLL_TIMEOUT = 25
#: How often due reminders are looked for.
REMINDER_TICK = 20.0
#: How many link codes one chat may try, and over what window.
CODE_ATTEMPTS = 5
CODE_ATTEMPT_WINDOW = 900.0


class ConfigError(ValueError):
    """The environment asks for a channel it cannot build."""


#: A BotFather token: the bot's numeric id, a colon, then its secret.
TOKEN_SHAPE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")


def looks_like_token(token: str) -> bool:
    """Whether *token* has the shape BotFather gives a bot (not whether it works)."""
    return bool(TOKEN_SHAPE.match(str(token or "").strip()))


def _flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    #: The bot token; never printed (``repr=False``) and never logged.
    token: str = field(default="", repr=False)
    api_root: str = API_ROOT
    poll_timeout: int = POLL_TIMEOUT
    link_ttl: float = LINK_TTL
    reminder_tick: float = REMINDER_TICK
    #: A link code is eight hex digits; five tries per quarter of an hour leaves
    #: a guesser no chance and a mistyping user plenty.
    code_attempts: int = CODE_ATTEMPTS
    code_attempt_window: float = CODE_ATTEMPT_WINDOW
    voice_replies: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if type(self.voice_replies) is not bool:
            raise ValueError("voice_replies must be a boolean")
        if not 1 <= int(self.poll_timeout) <= 50:
            raise ValueError("poll_timeout must be an integer in 1..50")
        if not 60 <= float(self.link_ttl) <= 3600:
            raise ValueError("link_ttl must be a number of seconds in 60..3600")
        if not 1 <= float(self.reminder_tick) <= 3600:
            raise ValueError("reminder_tick must be a number of seconds in 1..3600")
        if not 1 <= int(self.code_attempts) <= 100:
            raise ValueError("code_attempts must be an integer in 1..100")
        if not 1 <= float(self.code_attempt_window) <= 86400:
            raise ValueError("code_attempt_window must be a number of seconds in 1..86400")
        if self.enabled and not self.token:
            raise ValueError(f"{ENABLED_ENV} is set but {TOKEN_ENV} is empty")
        if self.token and not self.token.strip():
            raise ValueError("the bot token is blank")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """The settings this process runs with; disabled without a token.

        ``GRID_TELEGRAM_ENABLED=1`` without ``GRID_TELEGRAM_BOT_TOKEN`` raises:
        the service does not start half-configured.
        """
        source = os.environ if environ is None else environ
        token = str(source.get(TOKEN_ENV) or "").strip()
        wanted = _flag(source.get(ENABLED_ENV))
        if wanted and not token:
            raise ConfigError(f"{ENABLED_ENV} is set but {TOKEN_ENV} is empty")
        return cls(
            enabled=bool(token) and wanted,
            token=token,
            voice_replies=_flag(source.get(VOICE_ENV)),
        )

    @classmethod
    def from_sources(
        cls, stored_token: str = "", environ: Mapping[str, str] | None = None
    ) -> Settings:
        """The settings of the environment and the vault, the environment first.

        A token in the environment is the operator's word: it is used whatever
        the vault holds and, as :meth:`from_env` has always done, it starts the
        channel only with ``GRID_TELEGRAM_ENABLED=1``. A token the admin stored
        in the web interface turns the channel on by itself - setting it is the
        act of turning it on - and removing it turns the channel off again.
        """
        source = os.environ if environ is None else environ
        env_token = str(source.get(TOKEN_ENV) or "").strip()
        stored = str(stored_token or "").strip()
        wanted = _flag(source.get(ENABLED_ENV))
        token = env_token or stored
        if wanted and not token:
            raise ConfigError(f"{ENABLED_ENV} is set but {TOKEN_ENV} is empty")
        enabled = bool(token) and (wanted if env_token else True)
        return cls(
            enabled=enabled,
            token=token,
            voice_replies=_flag(source.get(VOICE_ENV)),
        )
