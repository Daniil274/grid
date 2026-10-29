"""Reminders: reading times, the tick loop on injected clocks, and the commands."""

from datetime import UTC, datetime

import pytest

from web_chat.telegram.config import Settings
from web_chat.telegram.reminders import (
    DAY,
    ReminderError,
    Reminders,
    describe,
    parse_reminder,
)
from web_chat.telegram.service import TelegramChannel
from web_chat.telegram.store import TelegramStore

#: 2026-10-24 12:00 UTC.
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC).timestamp()


class Bot:
    def __init__(self):
        self.sent = []
        self.answers = []

    async def send_message(self, chat_id, text, thread_id=None, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "thread_id": thread_id})
        return {"message_id": len(self.sent)}

    async def answer_callback_query(self, callback_id, text=""):
        self.answers.append((callback_id, text))


class Spaces:
    def use(self, _user_id):
        class Lease:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False

        return Lease()


@pytest.fixture
def store():
    result = TelegramStore(":memory:")
    yield result
    result.close()


def clock(value=NOW):
    return lambda: value


# -- reading what follows /remind -----------------------------------------
def test_a_relative_duration_is_seconds_minutes_hours_or_days():
    assert parse_reminder("10m stretch", NOW).due_at == NOW + 600
    assert parse_reminder("2h30m call Anna", NOW).due_at == NOW + 9000
    assert parse_reminder("1d report", NOW).due_at == NOW + DAY
    assert parse_reminder("in 15m tea", NOW).due_at == NOW + 900


def test_a_clock_time_is_the_next_one_in_utc():
    assert parse_reminder("13:30 stand-up", NOW).due_at == NOW + 5400
    # Already past today: tomorrow.
    assert parse_reminder("09:00 stand-up", NOW).due_at == NOW + 21 * 3600


def test_daily_and_every_repeat():
    daily = parse_reminder("daily 09:00 plan", NOW)
    assert daily.repeat_seconds == DAY and daily.due_at == NOW + 21 * 3600
    every = parse_reminder("every 90m water", NOW)
    assert every.repeat_seconds == 5400 and every.due_at == NOW + 5400


@pytest.mark.parametrize("argument", ["", "10m", "later today", "99:99 x", "daily plan", "every 10s x"])
def test_an_unreadable_reminder_says_what_to_write(argument):
    with pytest.raises(ReminderError):
        parser = parse_reminder
        parser(argument, NOW)


def test_describe_names_a_repeat():
    spec = parse_reminder("every 15m water", NOW)
    assert "Every 15 minute(s)" in describe(spec.due_at, spec.repeat_seconds)
    assert "2026-10-24" in describe(spec.due_at)


# -- the tick loop ---------------------------------------------------------
async def test_a_due_reminder_is_sent_and_forgotten(store):
    bot = Bot()
    loop = Reminders(store, bot, clock=clock())
    store.add_reminder("alice", 11, 0, "stretch", due_at=NOW, now=NOW - 60)

    assert await loop.tick() == 1
    assert bot.sent == [{"chat_id": 11, "text": "⏰ Reminder: stretch", "thread_id": None}]
    assert store.reminders("alice") == []
    assert await loop.tick() == 0


async def test_a_reminder_in_the_future_waits(store):
    bot = Bot()
    loop = Reminders(store, bot, clock=clock())
    store.add_reminder("alice", 11, 0, "later", due_at=NOW + 60, now=NOW)
    assert await loop.tick() == 0
    assert bot.sent == []


async def test_a_repeating_reminder_moves_to_its_next_time(store):
    bot = Bot()
    loop = Reminders(store, bot, clock=clock())
    store.add_reminder("alice", 11, 0, "water", due_at=NOW, repeat_seconds=900, now=NOW - 900)

    assert await loop.tick() == 1
    (left,) = store.reminders("alice")
    assert left.due_at == NOW + 900
    assert await loop.tick(NOW + 900) == 1
    assert len(bot.sent) == 2


async def test_a_missed_repeat_does_not_flood_the_chat(store):
    bot = Bot()
    loop = Reminders(store, bot, clock=clock())
    store.add_reminder("alice", 11, 0, "water", due_at=NOW - 5000, repeat_seconds=900, now=NOW - 5000)

    assert await loop.tick() == 1
    (left,) = store.reminders("alice")
    assert left.due_at > NOW


# -- the commands ----------------------------------------------------------
def link(store):
    store.redeem_code(store.create_code("alice", now=NOW)["code"], 11, 0, now=NOW)


def channel(store, bot):
    return TelegramChannel(store, Spaces(), Settings(enabled=True, token="1:x"), bot=bot, clock=clock())


async def command(store, text):
    bot = Bot()
    service = channel(store, bot)
    link(store)
    await service.dispatch({"update_id": 1, "message": {
        "chat": {"id": 11, "type": "private"}, "from": {"id": 11, "username": "alice"},
        "text": text}})
    return bot, store


async def test_remind_stores_and_confirms(store):
    bot, _ = await command(store, "/remind 10m stretch")
    (reminder,) = store.reminders("alice")
    assert reminder.text == "stretch" and reminder.due_at == NOW + 600
    assert "Reminder set" in bot.sent[-1]["text"]


async def test_reminders_lists_and_cancel_drops_one(store):
    bot, _ = await command(store, "/remind 10m stretch")
    (reminder,) = store.reminders("alice")

    await channel(store, bot).dispatch({"update_id": 2, "message": {
        "chat": {"id": 11, "type": "private"}, "from": {"id": 11, "username": "alice"},
        "text": "/reminders"}})
    assert reminder.id in bot.sent[-1]["text"] and "stretch" in bot.sent[-1]["text"]

    await channel(store, bot).dispatch({"update_id": 3, "message": {
        "chat": {"id": 11, "type": "private"}, "from": {"id": 11, "username": "alice"},
        "text": f"/cancel {reminder.id}"}})
    assert store.reminders("alice") == []
    assert bot.sent[-1]["text"] == "Cancelled."


async def test_cancel_all_and_cancel_with_nothing_to_cancel(store):
    await command(store, "/remind 10m one")
    bot, _ = await command(store, "/remind 20m two")
    await channel(store, bot).dispatch({"update_id": 4, "message": {
        "chat": {"id": 11, "type": "private"}, "from": {"id": 11, "username": "alice"},
        "text": "/cancel all"}})
    assert store.reminders("alice") == []
    assert "Cancelled 2" in bot.sent[-1]["text"]

    await channel(store, bot).dispatch({"update_id": 5, "message": {
        "chat": {"id": 11, "type": "private"}, "from": {"id": 11, "username": "alice"},
        "text": "/cancel"}})
    assert "Which one" in bot.sent[-1]["text"]


async def test_a_bad_reminder_is_explained_not_stored(store):
    bot, _ = await command(store, "/remind tomorrow stretch")
    assert store.reminders("alice") == []
    assert "10m" in bot.sent[-1]["text"]
