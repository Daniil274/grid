"""The Telegram channel as a whole: linking, messages, turns, buttons, reminders.

Everything the channel may do is here and nothing else: it reads an update,
finds the linked user, and acts in that user's space through the same seams
the web chat uses (a chat turn, an action review, the speech service). A chat
that is not linked can do one thing only - spend a single-use link code.

The channel is off unless ``GRID_TELEGRAM_ENABLED=1`` and a token are in the
environment. A user the server no longer admits is refused even if linked, as
a planned restart refuses to resume a disabled account's turn.

The channel speaks to one person in a private chat, where the chat id is that
person's Telegram id: the store records both (``links.tg_user_id``) and every
message and every button is checked against them, so a group, a forwarded
message or a third party can neither act as the user nor answer for them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import audio
from .bot import Bot, BotError, chunks
from .config import Settings
from .confirm import Approvals, parse_callback
from .reminders import ReminderError, Reminders, describe
from web_chat.errors import error_text
from .runner import TurnRunner
from .store import TelegramStore

logger = logging.getLogger("grid.web_chat.telegram")

#: A link code: eight hex digits, as ``store.create_code`` makes them.
CODE = re.compile(r"^[0-9A-Fa-f]{8}$")

HELP = """I am the GRID assistant on Telegram.

Just write a message: it runs as a turn in your GRID conversation.
Voice notes work too, when speech recognition is set up.

/remind 10m stretch - in ten minutes
/remind 18:30 stand-up - the next 18:30 UTC
/remind daily 09:00 plan - every day at 09:00 UTC
/remind every 90m water - every ninety minutes
/reminders - what is waiting
/cancel <id> - drop one, /cancel all - drop everything
/unlink - disconnect this chat from GRID
"""


class TelegramDisabled(RuntimeError):
    """The channel is off: no token, or not started."""


class TelegramChannel:
    def __init__(
        self,
        store: TelegramStore,
        spaces: Any,
        settings: Settings,
        *,
        bot: Any,
        runner: TurnRunner | None = None,
        approvals: Approvals | None = None,
        reminders: Reminders | None = None,
        allowed: Callable[[str], bool] = lambda user_id: True,
        voice_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = lambda: datetime.now(UTC).timestamp(),
    ) -> None:
        self.store = store
        self.spaces = spaces
        self.settings = settings
        self.bot = bot
        self.runner = runner or TurnRunner(spaces)
        self.approvals = approvals or Approvals(store, bot)
        self.reminders = reminders or Reminders(store, bot)
        self.allowed = allowed
        self._voice_factory = voice_factory
        self._voice_cached: Any = None
        self._clock = clock
        self._poller: asyncio.Task | None = None
        self._started = False
        self._username: str | None = None

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self.reminders.start()
        self._poller = asyncio.create_task(self._poll_forever(), name="telegram-poll")

    async def close(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
            with suppress(asyncio.CancelledError):
                await self._poller
            self._poller = None
        await self.reminders.close()
        close = getattr(self.bot, "close", None)
        if close is not None:
            with suppress(Exception):
                await close()
        self.store.close()

    # -- what the web page asks ---------------------------------------------
    def code_for(self, user_id: str) -> dict[str, Any]:
        """A new single-use code for this user; the previous one stops working."""
        if not self.settings.enabled:
            raise TelegramDisabled("The Telegram channel is off")
        return self.store.create_code(user_id, now=self._clock(), ttl=self.settings.link_ttl)

    async def bot_username(self) -> str:
        """The bot's @username, as ``getMe`` names it; "" when Telegram is not reachable.

        Asked once per channel: the username of a bot does not change, and the
        page needs it to build ``https://t.me/<bot>?start=<code>``.
        """
        if self._username is None:
            bot = self.bot
            get_me = getattr(bot, "get_me", None)
            if get_me is None:  # a bot double without the call: no link, a code still
                self._username = ""
            else:
                try:
                    found = await get_me()
                except Exception:  # noqa: BLE001 - the code is still worth showing
                    logger.warning("Telegram did not say which bot this is")
                    return ""
                self._username = str((found or {}).get("username") or "")
        return self._username

    async def link_code(self, user_id: str) -> dict[str, Any]:
        """A code and the t.me link that spends it, for the web page's button."""
        made = self.code_for(user_id)
        username = await self.bot_username()
        return {
            **made,
            "bot_username": username,
            "url": f"https://t.me/{username}?start={made['code']}" if username else "",
        }

    def state(self, user_id: str) -> dict[str, Any]:
        link = self.store.link(user_id)
        return {
            "enabled": bool(self.settings.enabled),
            "link": None if link is None else {
                "chat_id": link.chat_id,
                "username": link.username,
                "linked_at": link.linked_at,
            },
            "chats": [
                {"chat_id": row["chat_id"], "thread_id": row["thread_id"],
                 "context_id": row["context_id"], "updated_at": row["updated_at"]}
                for row in self.store.chats(user_id)
            ],
            "reminders": [
                {"id": item.id, "text": item.text, "due_at": item.due_at,
                 "repeat_seconds": item.repeat_seconds}
                for item in self.store.reminders(user_id)
            ],
        }

    def unlink(self, user_id: str) -> bool:
        return self.store.unlink(user_id)

    # -- long poll -----------------------------------------------------------
    async def _poll_forever(self) -> None:
        while True:
            try:
                updates = await self.bot.get_updates(
                    self.store.offset(), timeout=self.settings.poll_timeout
                )
            except BotError:
                logger.warning("Telegram long poll failed; retrying")
                await asyncio.sleep(5)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the poller must survive
                logger.warning("Telegram long poll failed; retrying")
                await asyncio.sleep(5)
                continue
            for update in updates or []:
                if not isinstance(update, dict) or "update_id" not in update:
                    continue
                self.store.set_offset(int(update["update_id"]) + 1)
                try:
                    await self.dispatch(update)
                except Exception:  # noqa: BLE001 - one bad update must not stop the poll
                    logger.warning("A Telegram update could not be handled")
            if not updates:
                await asyncio.sleep(1)

    async def dispatch(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("callback_query"), dict):
            await self._callback(update["callback_query"])
        elif isinstance(update.get("message"), dict):
            await self._message(update["message"])

    # -- messages ------------------------------------------------------------
    async def _message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        chat_id = int(chat_id)
        # The channel speaks to one person in a private chat, where the chat id
        # is that person's Telegram id. In a group nobody could be told apart,
        # so nothing there - not a message, not a code - is acted on.
        if str(chat.get("type") or "") != "private":
            logger.info("A Telegram message from a chat that is not private was ignored")
            return
        sender = message.get("from") or {}
        sender_id = sender.get("id")
        if not isinstance(sender_id, int) or sender_id != chat_id:
            logger.info("A Telegram message whose sender is not the chat's owner was ignored")
            return
        thread_id = int(message.get("message_thread_id") or 0)
        text = str(message.get("text") or message.get("caption") or "").strip()
        username = str(sender.get("username") or sender.get("first_name") or "")[:64]

        user_id = self.store.user_for_chat(chat_id, thread_id)
        if user_id is None:
            await self._link_attempt(chat_id, thread_id, text, sender_id, username)
            return
        if not self._admitted(user_id):
            await self._say(chat_id, thread_id, "This server is closed for your account.")
            return
        if text.startswith("/"):
            await self._command(user_id, chat_id, thread_id, text)
            return
        voice = message.get("voice") or message.get("audio") or {}
        if isinstance(voice, dict) and voice.get("file_id"):
            await self._voice_note(user_id, chat_id, thread_id, voice, caption=text)
            return
        if message.get("video_note"):
            await self._say(chat_id, thread_id, "Video notes are not supported; send text or a voice note.")
            return
        if not text:
            await self._say(chat_id, thread_id, "I work with text and voice notes.")
            return
        await self._turn(user_id, chat_id, thread_id, text)

    async def _link_attempt(self, chat_id: int, thread_id: int, text: str, tg_user_id: int,
                            username: str) -> None:
        code = text
        if text.lower().startswith("/start"):
            code = text[len("/start"):].strip()
        elif text.startswith("/") or not CODE.fullmatch(text.strip()):
            code = ""
        if not code:
            await self._say(
                chat_id, thread_id,
                "This chat is not linked to GRID.\n"
                "Open GRID, ask for a Telegram link code and send it here.",
            )
            return
        if not self.store.spend_code_attempt(
            chat_id, self._clock(),
            limit=self.settings.code_attempts, window=self.settings.code_attempt_window,
        ):
            logger.warning("A Telegram chat used up its link code attempts")
            await self._say(chat_id, thread_id, "Too many codes were tried here. Ask GRID for a new one later.")
            return
        user_id = self.store.redeem_code(code, chat_id, thread_id, now=self._clock(),
                                         username=username, tg_user_id=tg_user_id)
        if user_id is None:
            await self._say(chat_id, thread_id, "That code is not valid any more. Ask GRID for a new one.")
            return
        if not self._admitted(user_id):
            self.store.unlink(user_id)
            await self._say(chat_id, thread_id, "This server is closed for your account.")
            return
        await self._say(chat_id, thread_id, "Linked. Write a message to start.\n\n" + HELP)

    def _admitted(self, user_id: str) -> bool:
        try:
            return bool(self.allowed(user_id))
        except Exception:  # noqa: BLE001 - an unreadable account is not an admission
            return False

    async def _command(self, user_id: str, chat_id: int, thread_id: int, text: str) -> None:
        name, _, argument = text.partition(" ")
        name = name.split("@", 1)[0].lower()
        if name in {"/start", "/help"}:
            await self._say(chat_id, thread_id, HELP)
        elif name == "/unlink":
            self.unlink(user_id)
            await self._say(chat_id, thread_id, "This chat is no longer linked to GRID.")
        elif name == "/remind":
            try:
                reminder = self.reminders.add(user_id, chat_id, thread_id, argument, now=self._clock())
            except ReminderError as exc:
                await self._say(chat_id, thread_id, error_text(exc))
                return
            except Exception:  # noqa: BLE001 - a bad reminder is a message, not a crash
                logger.warning("A Telegram reminder could not be stored")
                await self._say(chat_id, thread_id, "That reminder could not be stored.")
                return
            when = describe(reminder.due_at, reminder.repeat_seconds)
            await self._say(chat_id, thread_id, f"Reminder set: {when}\n{reminder.text}")
        elif name == "/reminders":
            items = self.store.reminders(user_id)
            if not items:
                await self._say(chat_id, thread_id, "No reminders.")
                return
            lines = [
                f"{item.id} · {describe(item.due_at, item.repeat_seconds)} · {item.text}"
                for item in items
            ]
            await self._say(chat_id, thread_id, "Reminders:\n" + "\n".join(lines))
        elif name == "/cancel":
            target = argument.strip()
            if target.lower() in {"all", "*"}:
                removed = self.store.cancel_all_reminders(user_id)
                await self._say(chat_id, thread_id, f"Cancelled {removed} reminder(s).")
            elif target and self.store.cancel_reminder(user_id, target):
                await self._say(chat_id, thread_id, "Cancelled.")
            else:
                await self._say(chat_id, thread_id, "Which one? /cancel <id>, or /cancel all.")
        else:
            await self._say(chat_id, thread_id, "Unknown command.\n\n" + HELP)

    # -- a turn --------------------------------------------------------------
    async def _turn(self, user_id: str, chat_id: int, thread_id: int, text: str, *, audio: Any = None) -> None:
        context_id = self.store.context(chat_id, thread_id) or uuid.uuid4().hex
        self.store.set_context(chat_id, thread_id, user_id, context_id, now=self._clock())

        async def watch(space: Any, active_context: str) -> None:
            await self.approvals.publish(space, user_id, chat_id, thread_id, active_context)

        try:
            kwargs = {"audio": audio} if audio is not None else {}
            outcome = await self.runner.run(user_id, context_id, text, on_watch=watch, **kwargs)
        except Exception:  # noqa: BLE001 - the user gets a refusal, not silence
            logger.warning("A Telegram turn failed to run")
            await self._say(chat_id, thread_id, "The turn could not be started. Try again.")
            return
        finally:
            self.approvals.forget_context(context_id)

        if outcome.outcome == "busy":
            await self._say(chat_id, thread_id, "I am still working on your previous message.")
            return
        if outcome.outcome != "answered":
            reason = outcome.reason or "The turn ended without an answer."
            await self._say(chat_id, thread_id, reason)
            return
        answer = outcome.answer.strip() or "(The agent finished without text.)"
        spoken = self.settings.voice_replies and await self._voice_reply(chat_id, thread_id, answer)
        if not spoken:
            await self._say(chat_id, thread_id, answer)

    def _voice_service(self) -> Any:
        if self._voice_cached is None and self._voice_factory is not None:
            try:
                self._voice_cached = self._voice_factory()
            except Exception:  # noqa: BLE001 - voice is optional
                logger.warning("Telegram voice is unavailable")
        return self._voice_cached

    async def _voice_reply(self, chat_id: int, thread_id: int, text: str) -> bool:
        """A voice note of the answer; False means the caller sends text instead."""
        service = self._voice_service()
        if service is None or not audio.available():
            return False
        try:
            wav = await service.synthesize(text[:4000])
            ogg = await audio.to_ogg(wav)
            await self.bot.send_voice(chat_id, ogg, thread_id=thread_id)
            return True
        except Exception:  # noqa: BLE001 - text is always a good fallback
            logger.info("A Telegram voice reply was not sent; sending text")
            return False

    async def _voice_note(self, user_id: str, chat_id: int, thread_id: int, voice: dict[str, Any], *, caption: str = "") -> None:
        try:
            from core.media import MAX_AUDIO_BYTES, sniff
            from web_chat.audio_ingress import AudioAttachment
            if int(voice.get("file_size") or 0) > MAX_AUDIO_BYTES:
                raise ValueError("Audio message exceeds 20 MiB")
            data = await self.bot.download(str(voice["file_id"]))
            attachment = AudioAttachment(data, sniff(data[:64]) or "")
        except BotError:
            await self._say(chat_id, thread_id, "That voice note could not be downloaded.")
            return
        except ValueError as exc:
            await self._say(chat_id, thread_id, error_text(exc))
            return
        await self._turn(user_id, chat_id, thread_id, caption, audio=attachment)

    # -- buttons -------------------------------------------------------------
    async def _callback(self, query: dict[str, Any]) -> None:
        callback_id = str(query.get("id") or "")
        parsed = parse_callback(str(query.get("data") or ""))
        if parsed is None:
            await self._answer_button(callback_id, "")
            return
        approve, approval_id = parsed
        record = self.store.approval(approval_id)
        if record is None:
            await self._answer_button(callback_id, "This request is no longer waiting.")
            return
        # A button is only a button in the chat it was sent to, pressed by the
        # person the chat belongs to. Telegram copies inline keyboards with a
        # forwarded message, so the id it carries is not a capability: the
        # record's chat, the linked Telegram identity and the message itself
        # are checked before the answer counts.
        if not self._button_belongs_here(query, record):
            logger.warning("A Telegram approval button was pressed outside the chat it was sent to")
            await self._answer_button(callback_id, "This button does not belong to this chat.")
            return
        if not self._admitted(record.user_id):
            await self._answer_button(callback_id, "This server is closed for your account.")
            return
        resolved = False
        try:
            async with self.spaces.use(record.user_id) as space:
                resolved = await self.approvals.answer(space, approval_id, approve=approve)
        except Exception:  # noqa: BLE001 - answer the button, never leave it spinning
            logger.warning("A Telegram approval could not be answered")
        await self._answer_button(
            callback_id,
            ("Approved" if approve else "Declined") if resolved else "It is no longer waiting.",
        )

    def _button_belongs_here(self, query: dict[str, Any], record: Any) -> bool:
        """Whether this callback comes from the chat and the person it was sent to."""
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("type") or "") != "private":
            return False
        if chat.get("id") != record.chat_id:
            return False
        if record.message_id is not None and message.get("message_id") != record.message_id:
            return False
        sender = query.get("from") or {}
        sender_id = sender.get("id")
        link = self.store.link(record.user_id)
        if link is None or not isinstance(sender_id, int):
            return False
        return sender_id == link.tg_user_id == record.chat_id

    async def _answer_button(self, callback_id: str, text: str) -> None:
        if not callback_id:
            return
        with suppress(Exception):
            await self.bot.answer_callback_query(callback_id, text)

    # -- sending -------------------------------------------------------------
    async def _say(self, chat_id: int, thread_id: int, text: str) -> None:
        for part in chunks(text):
            try:
                await self.bot.send_message(chat_id, part, thread_id=thread_id or None)
            except BotError:
                logger.warning("A Telegram message could not be sent")
                return


def open_telegram(
    path: Path | str,
    spaces: Any,
    *,
    settings: Settings | None = None,
    allowed: Callable[[str], bool] = lambda user_id: True,
    voice_factory: Callable[[], Any] | None = None,
    bot: Any | None = None,
    clock: Callable[[], float] | None = None,
) -> TelegramChannel | None:
    """The channel this environment asks for, or None when it is off.

    A missing token means off; ``GRID_TELEGRAM_ENABLED=1`` without a token
    raises, so a misconfigured server does not start a bot with no way in.
    """
    settings = settings or Settings.from_env()
    if not settings.enabled:
        logger.info("Telegram channel is off (no %s or not enabled)", "GRID_TELEGRAM_BOT_TOKEN")
        return None
    store = TelegramStore(path)
    kwargs: dict[str, Any] = {"voice_factory": voice_factory, "allowed": allowed}
    if clock is not None:
        kwargs["clock"] = clock
    return TelegramChannel(store, spaces, settings, bot=bot or Bot(settings.token, api_root=settings.api_root), **kwargs)
