"""Reminders: one-off and repeating, delivered to the linked chat.

A tick loop like the daily digest's: every few seconds the store is asked
what is due, each due reminder is sent to its own chat, and a repeating one
moves to its next time. Time comes from an injectable clock, so the loop is
tested without waiting.

Times are read in UTC:

    /remind 10m stretch            in ten minutes
    /remind 2h call Anna           in two hours
    /remind 18:30 stand-up         the next 18:30
    /remind daily 09:00 plan       every day at 09:00
    /remind every 90m water        every ninety minutes
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import REMINDER_TICK

logger = logging.getLogger("grid.web_chat.telegram")

DAY = 86400.0
UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": DAY}
_DURATION = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


class ReminderError(ValueError):
    """The user's reminder cannot be read; the message says what to write."""


@dataclass(frozen=True)
class ReminderSpec:
    text: str
    due_at: float
    repeat_seconds: float = 0.0


def _duration(value: str) -> float | None:
    """``90m``, ``2h30m``, ``1d`` -> seconds; None when it is not a duration."""
    cleaned = value.strip()
    if not cleaned:
        return None
    found = _DURATION.findall(cleaned)
    if not found or _DURATION.sub("", cleaned).strip():
        return None
    return float(sum(int(number) * UNIT_SECONDS[unit.lower()] for number, unit in found))


def _next_clock(value: str, now: float) -> float | None:
    """The next moment UTC at ``HH:MM``: today if it is still ahead, else tomorrow."""
    match = _CLOCK.match(value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    moment = datetime.fromtimestamp(now, UTC)
    target = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()


def _split_head(argument: str, count: int) -> tuple[str, str]:
    """The first *count* whitespace-separated words and the rest."""
    parts = argument.strip().split(None, count)
    if len(parts) <= count:
        return argument.strip(), ""
    return " ".join(parts[:count]), " ".join(parts[count:])


def parse_reminder(argument: str, now: float) -> ReminderSpec:
    """Read what follows ``/remind``; :class:`ReminderError` says what is wrong."""
    argument = " ".join(argument.strip().split())
    if not argument:
        raise ReminderError("Write when and what: /remind 10m stretch")
    lowered = argument.lower()
    repeat = 0.0

    if lowered.startswith("daily "):
        head, rest = _split_head(argument[len("daily "):], 1)
        due = _next_clock(head, now)
        if due is None:
            raise ReminderError("A daily reminder needs a time: /remind daily 09:00 plan")
        repeat, text = DAY, rest
    elif lowered.startswith("every "):
        head, rest = _split_head(argument[len("every "):], 1)
        seconds = _duration(head)
        if seconds is None or seconds < 60:
            raise ReminderError("An interval looks like 15m, 2h or 1d: /remind every 15m water")
        due, repeat, text = now + seconds, seconds, rest
    else:
        head, rest = _split_head(argument, 1)
        if head.lower().startswith("in ") or head.lower() == "in":
            # "in 10m text" is two words before the text; read it again.
            head, rest = _split_head(argument, 2)
            head = head[3:]
        seconds = _duration(head)
        if seconds is not None:
            if seconds < 1:
                raise ReminderError("A reminder must be at least a second ahead.")
            due, text = now + seconds, rest
        else:
            due = _next_clock(head, now)
            if due is None:
                raise ReminderError(
                    "Read the time as 10m, 2h, 18:30 or daily 09:00: /remind 10m stretch"
                )
            text = rest

    text = text.strip()
    if not text:
        raise ReminderError("What should the reminder say? /remind 10m stretch")
    if len(text) > 500:
        raise ReminderError("The reminder is longer than 500 characters; shorten it.")
    return ReminderSpec(text, due, repeat)


def describe(due_at: float, repeat_seconds: float = 0.0) -> str:
    """A short confirmation of when the reminder will come."""
    when = datetime.fromtimestamp(due_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
    if repeat_seconds > 0:
        every = repeat_seconds
        if every % DAY == 0:
            return f"Every {every / DAY:g} day(s), next {when}"
        if every % 3600 == 0:
            return f"Every {every / 3600:g} hour(s), next {when}"
        return f"Every {every / 60:g} minute(s), next {when}"
    return when


class Reminders:
    def __init__(self, store: Any, bot: Any, *, clock: Callable[[], float] = time.time,
                 tick_seconds: float = REMINDER_TICK) -> None:
        self.store = store
        self.bot = bot
        self._clock = clock
        self._tick_seconds = tick_seconds
        self.task: asyncio.Task | None = None

    def add(self, user_id: str, chat_id: int, thread_id: int, argument: str,
            now: float | None = None) -> Any:
        now = self._clock() if now is None else now
        spec = parse_reminder(argument, now)
        return self.store.add_reminder(user_id, chat_id, thread_id, spec.text,
                                       spec.due_at, spec.repeat_seconds, now=now)

    async def tick(self, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        sent = 0
        for reminder in self.store.due_reminders(now):
            try:
                await self.bot.send_message(
                    reminder.chat_id, f"⏰ Reminder: {reminder.text}",
                    thread_id=reminder.thread_id or None,
                )
            except Exception:  # noqa: BLE001 - another reminder may still go out
                logger.warning("A Telegram reminder could not be sent; it stays due")
                continue
            next_due = None
            if reminder.repeat_seconds > 0:
                next_due = reminder.due_at + reminder.repeat_seconds
                while next_due <= now:
                    next_due += reminder.repeat_seconds
            self.store.finish_reminder(reminder.id, next_due)
            sent += 1
        return sent

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="telegram-reminders")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - upkeep must survive one bad tick
                logger.warning("Telegram reminder tick failed")
            await asyncio.sleep(self._tick_seconds)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
