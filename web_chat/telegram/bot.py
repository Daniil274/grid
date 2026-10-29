"""A small Telegram Bot API client: long poll, messages, buttons, voice.

Only ``httpx`` is used; there is no webhook and no new dependency. The token is
part of every request URL, so the client takes the two libraries that would
print it out of the logging system (:func:`quiet_http_logging`) and redacts the
token from anything they still emit; callers get :class:`BotError` with
Telegram's own description, and a transport failure is reported by its class
name, never by its URL.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import API_ROOT

logger = logging.getLogger("grid.web_chat.telegram")

#: Telegram's own limit on one message's text.
MAX_TEXT = 4000

#: Above ``CRITICAL``: no level reaches a handler from these loggers.
SILENT = logging.CRITICAL + 10
#: The libraries whose records carry the request URL, token and all.
NOISY = ("httpx", "httpcore")
#: What the token becomes when a record is redacted.
REDACTED = "(bot token)"
#: Every token a client was made with, so one filter redacts them all.
TOKENS: set[str] = set()


class RedactToken(logging.Filter):
    """Cut the bot token out of a record, whatever its level or logger name."""

    def __init__(self, tokens: set[str]) -> None:
        super().__init__()
        self._tokens = tokens

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record is not ours to fix
            return True
        redacted = message
        for token in self._tokens:
            if token:
                redacted = redacted.replace(token, REDACTED)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def quiet_http_logging(token: str) -> None:
    """Keep a URL that carries *token* out of the logs, at every level.

    ``httpx`` logs every request URL at INFO and ``httpcore`` traces
    connections at DEBUG, so a deployment that raised the root level would
    print the token. Both libraries are held above ``CRITICAL`` (their child
    loggers inherit that level) and a filter redacts the token from anything
    that still gets through, so the promise does not depend on the configured
    level.
    """
    TOKENS.add(token)
    for name in NOISY:
        library = logging.getLogger(name)
        library.setLevel(SILENT)
        if not any(isinstance(item, RedactToken) for item in library.filters):
            library.addFilter(RedactToken(TOKENS))


class BotError(RuntimeError):
    """Telegram refused a call, or could not be reached."""


def chunks(text: str, size: int = MAX_TEXT) -> list[str]:
    """Split a long answer into messages Telegram accepts.

    Breaks on a newline or space when one is close enough, so a paragraph is
    not cut in the middle of a word.
    """
    text = text or ""
    if not text.strip():
        return []
    parts: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        cut = cut if cut > 0 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        parts.append(text)
    return parts


class Bot:
    """The calls the channel makes, one client for the process' lifetime."""

    def __init__(self, token: str, *, api_root: str = API_ROOT, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not token:
            raise ValueError("a bot token is required")
        quiet_http_logging(token)
        self._token = token
        self._root = api_root.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._root, timeout=timeout, transport=transport,
            headers={"User-Agent": "grid-web-chat/telegram"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- calls ---------------------------------------------------------------
    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(f"/bot{self._token}/{method}", json=payload)
            body = response.json()
        except httpx.HTTPError as exc:
            # The URL holds the token: report the failure by its class only.
            raise BotError(f"Telegram is unreachable ({type(exc).__name__})") from None
        except ValueError:
            raise BotError("Telegram answered with something that is not JSON") from None
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description") if isinstance(body, dict) else ""
            raise BotError(str(description or "Telegram refused the request"))
        return body.get("result")

    async def get_updates(self, offset: int, *, timeout: int = 25) -> list[dict[str, Any]]:
        result = await self._call("getUpdates", {"offset": int(offset), "timeout": int(timeout),
                                                 "allowed_updates": ["message", "callback_query"]})
        return result if isinstance(result, list) else []

    async def get_me(self) -> dict[str, Any]:
        """The bot's own account: its ``username`` names the t.me link."""
        result = await self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    async def send_message(self, chat_id: int, text: str, *, thread_id: int | None = None,
                           reply_markup: dict | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = await self._call("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    async def edit_message_reply_markup(self, chat_id: int, message_id: int,
                                        reply_markup: dict | None) -> None:
        await self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": int(message_id),
                                                    "reply_markup": reply_markup or {"inline_keyboard": []}})

    async def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        await self._call("answerCallbackQuery", payload)

    async def send_voice(self, chat_id: int, ogg: bytes, *, thread_id: int | None = None,
                         duration: int = 0) -> dict[str, Any]:
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if thread_id:
            data["message_thread_id"] = str(int(thread_id))
        if duration:
            data["duration"] = str(int(duration))
        files = {"voice": ("voice.ogg", ogg, "audio/ogg")}
        try:
            response = await self._client.post(f"/bot{self._token}/sendVoice", data=data, files=files)
            body = response.json()
        except httpx.HTTPError as exc:
            raise BotError(f"Telegram is unreachable ({type(exc).__name__})") from None
        except ValueError:
            raise BotError("Telegram answered with something that is not JSON") from None
        if not isinstance(body, dict) or not body.get("ok"):
            raise BotError(str((body or {}).get("description") or "Telegram refused the voice message"))
        return body.get("result") or {}

    async def download(self, file_id: str) -> bytes:
        """The bytes of a file the user sent: ``getFile`` then the file itself."""
        info = await self._call("getFile", {"file_id": file_id})
        path = str((info or {}).get("file_path") or "")
        if not path:
            raise BotError("Telegram returned no file path")
        try:
            response = await self._client.get(f"/file/bot{self._token}/{path}")
        except httpx.HTTPError as exc:
            raise BotError(f"Telegram is unreachable ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise BotError("Telegram refused to send the file")
        return response.content

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        await self._call("setMyCommands", {"commands": commands})
