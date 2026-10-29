"""The Telegram channel: GRID in a linked chat, off until a bot token says on.

    GRID_TELEGRAM_ENABLED=1
    GRID_TELEGRAM_BOT_TOKEN=...

or, without root and without a restart, the bot token an admin sets in the web
interface: it is sealed in the server's vault (``GRID_SECRETS_KEY``) and the
channel starts on it at once.

Text messages and voice notes run as ordinary turns of the user's own
conversation; calls waiting for approval arrive as inline buttons; reminders
come back on their own. Everything is reached through the seams the web chat
already has, so the channel adds a way in, not a second engine.

Modules:

- ``config``    - settings, the environment first, the vault after it, off by default;
- ``secrets``   - the bot token sealed in the vault, hinted, never printed;
- ``manager``   - owns the channel: starts and stops it at runtime;
- ``store``     - one SQLite file: links, single-use codes, chats, approvals, reminders;
- ``bot``       - long poll and the Bot API calls, through httpx;
- ``runner``    - one message as one chat turn (web_chat.lazy.runner's way);
- ``confirm``   - ``space.awaiting_approval`` -> buttons -> ``answer_action_review``;
- ``audio``     - Ogg/Opus <-> WAV PCM16 mono through ffmpeg;
- ``reminders`` - a tick loop, one-off and repeating reminders;
- ``http``      - ``/api/telegram/*`` and ``/api/admin/telegram``, and the ``/telegram`` page.

``docs/telegram.md`` is the step-by-step way to turn it on.
"""

from __future__ import annotations

from .config import ConfigError, Settings
from .manager import TelegramManager
from .service import TelegramChannel, TelegramDisabled, open_telegram
from .store import TelegramStore

__all__ = [
    "ConfigError",
    "Settings",
    "TelegramChannel",
    "TelegramDisabled",
    "TelegramManager",
    "TelegramStore",
    "open_telegram",
]
