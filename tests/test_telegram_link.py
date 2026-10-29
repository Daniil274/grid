"""Linking: codes are single-use and short-lived, unlink forgets, disabled users are refused."""

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from web_chat.telegram.config import Settings
from web_chat.telegram.http import register_telegram_routes
from web_chat.telegram.service import TelegramChannel
from web_chat.telegram.store import TelegramStore, code_hash

NOW = 1_000_000.0


@pytest.fixture
def store(tmp_path):
    result = TelegramStore(tmp_path / "telegram.db")
    yield result
    result.close()


class Bot:
    """Records what the channel would send; no network."""

    def __init__(self):
        self.sent = []
        self.buttons = []
        self.answers = []
        self.voices = []
        self.closed = False

    async def send_message(self, chat_id, text, thread_id=None, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "thread_id": thread_id,
                          "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.buttons.append((chat_id, message_id, reply_markup))

    async def answer_callback_query(self, callback_id, text=""):
        self.answers.append((callback_id, text))

    async def send_voice(self, chat_id, ogg, thread_id=None, duration=0):
        self.voices.append({"chat_id": chat_id, "ogg": ogg, "thread_id": thread_id})

    async def close(self):
        self.closed = True


class Spaces:
    def __init__(self, space=None):
        self.space = space
        self.used = []

    def use(self, user_id):
        self.used.append(user_id)
        space = self.space

        class Lease:
            async def __aenter__(self):
                return space

            async def __aexit__(self, *exc):
                return False

        return Lease()


def channel(store, *, bot=None, allowed=lambda user_id: True, spaces=None, voice_factory=None,
            clock=lambda: NOW):
    return TelegramChannel(
        store, spaces or Spaces(), Settings(enabled=True, token="123:secret"),
        bot=bot or Bot(), allowed=allowed, voice_factory=voice_factory, clock=clock,
    )


def message(text, chat_id=11, thread_id=0, username="alice", chat_type="private", sender_id=None):
    """An update as Telegram sends one: a private chat's id is its user's id."""
    body = {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": chat_id if sender_id is None else sender_id, "username": username},
        "text": text,
    }
    if thread_id:
        body["message_thread_id"] = thread_id
    return {"update_id": 1, "message": body}


# -- the store -------------------------------------------------------------
def test_a_code_links_a_chat_once_and_only_its_hash_is_stored(store):
    made = store.create_code("alice", now=1000)
    assert made["expires_at"] == 1600 and len(made["code"]) == 8
    rows = list(store.db.execute("SELECT code_hash FROM codes"))
    assert [row["code_hash"] for row in rows] == [code_hash(made["code"])]

    assert store.redeem_code(made["code"], 11, 0, now=1000) == "alice"
    assert store.link("alice").chat_id == 11
    assert store.redeem_code(made["code"], 12, 0, now=1000) is None


def test_an_expired_code_links_nobody(store):
    made = store.create_code("alice", now=1000, ttl=600)
    assert store.redeem_code(made["code"], 11, 0, now=1600) is None
    assert store.link("alice") is None


def test_a_new_code_replaces_the_previous_one(store):
    first = store.create_code("alice", now=1000)["code"]
    second = store.create_code("alice", now=1010)["code"]
    assert first != second
    assert store.redeem_code(first, 11, 0, now=1011) is None
    assert store.redeem_code(second.lower(), 11, 0, now=1011) == "alice"


def test_a_private_chat_is_one_link_whatever_the_thread(store):
    store.redeem_code(store.create_code("alice", now=1000)["code"], 11, 7, now=1000)
    assert store.user_for_chat(11, 7) == "alice"
    assert store.user_for_chat(11, 0) == "alice"
    assert store.user_for_chat(12, 0) is None
    store.set_context(11, 7, "alice", "ctx-1", now=1001)
    assert store.context(11, 7) == "ctx-1" and store.context(11, 0) is None


def test_unlink_forgets_the_chat_its_conversation_and_reminders(store):
    store.redeem_code(store.create_code("alice", now=1000)["code"], 11, 0, now=1000)
    store.set_context(11, 0, "alice", "ctx-1", now=1000)
    store.add_reminder("alice", 11, 0, "stretch", due_at=2000, now=1000)
    store.announce_approval("ap-1", "alice", 11, 0, "ctx-1", now=1000)

    assert store.unlink("alice") is True
    assert store.link("alice") is None
    assert store.user_for_chat(11, 0) is None
    assert store.context(11, 0) is None
    assert store.reminders("alice") == [] and store.approvals("alice") == []


def test_relinking_replaces_another_chat(store):
    store.redeem_code(store.create_code("alice", now=1000)["code"], 11, 0, now=1000)
    store.redeem_code(store.create_code("alice", now=1001)["code"], 22, 0, now=1001)
    assert store.link("alice").chat_id == 22
    assert store.user_for_chat(11, 0) is None


def test_relinking_forgets_the_old_chats_context_reminders_and_approvals(store):
    store.redeem_code(store.create_code("alice", now=1000)["code"], 11, 0, now=1000)
    store.set_context(11, 0, "alice", "ctx-old", now=1000)
    store.add_reminder("alice", 11, 0, "secret text", due_at=2000, now=1000)
    store.announce_approval("ap-old", "alice", 11, 0, "ctx-old", now=1000)

    store.redeem_code(store.create_code("alice", now=1001)["code"], 22, 0, now=1001)

    assert store.link("alice").chat_id == 22
    # Nothing of the old chat survives: no conversation, no reminder addressed
    # to it, no button that would still be answered there.
    assert store.user_for_chat(11, 0) is None
    assert store.context(11, 0) is None
    assert store.due_reminders(5000) == []
    assert store.approval("ap-old") is None


def test_a_chat_that_changes_hands_starts_clean(store):
    store.redeem_code(store.create_code("alice", now=1000)["code"], 11, 0, now=1000)
    store.set_context(11, 0, "alice", "ctx-alice", now=1000)
    store.add_reminder("alice", 11, 0, "alice's note", due_at=2000, now=1000)

    store.redeem_code(store.create_code("bob", now=1001)["code"], 11, 0, now=1001)

    assert store.user_for_chat(11, 0) == "bob"
    assert store.context(11, 0) is None
    assert store.reminders("alice") == [] and store.reminders("bob") == []


def test_only_a_private_chat_can_be_linked(store):
    made = store.create_code("alice", now=1000)
    with pytest.raises(ValueError):
        store.redeem_code(made["code"], -1009, 0, tg_user_id=11, now=1000)
    assert store.link("alice") is None
    # The code was not spent by the refused attempt.
    assert store.redeem_code(made["code"], 11, 0, tg_user_id=11, now=1000) == "alice"
    assert store.link("alice").tg_user_id == 11


def test_the_attempt_ledger_counts_per_chat_and_forgets_on_success(store):
    assert store.spend_code_attempt(11, 1000, limit=2, window=600)
    assert store.spend_code_attempt(11, 1000, limit=2, window=600)
    assert not store.spend_code_attempt(11, 1000, limit=2, window=600)
    # Another chat has its own allowance, and the window resets it.
    assert store.spend_code_attempt(22, 1000, limit=2, window=600)
    assert store.spend_code_attempt(11, 1601, limit=2, window=600)


def test_the_poll_cursor_survives_a_reopen(tmp_path):
    first = TelegramStore(tmp_path / "telegram.db")
    assert first.offset() == 0
    first.set_offset(41)
    first.close()
    second = TelegramStore(tmp_path / "telegram.db")
    assert second.offset() == 41
    second.close()


# -- the channel -----------------------------------------------------------
async def test_a_plain_message_from_an_unlinked_chat_asks_for_a_code(store):
    bot = Bot()
    await channel(store, bot=bot).dispatch(message("hello"))
    assert len(bot.sent) == 1
    assert "not linked" in bot.sent[0]["text"]


async def test_a_code_in_the_chat_links_it(store):
    bot = Bot()
    service = channel(store, bot=bot)
    code = service.code_for("alice")["code"]
    await service.dispatch(message(f"/start {code}"))
    assert store.link("alice").chat_id == 11
    assert "Linked" in bot.sent[-1]["text"]


async def test_a_bad_code_links_nobody(store):
    bot = Bot()
    await channel(store, bot=bot).dispatch(message("/start DEADBEEF"))
    assert store.link("alice") is None
    assert "not valid" in bot.sent[-1]["text"]


async def test_a_code_in_a_group_links_nobody_and_is_not_spent(store):
    bot = Bot()
    service = channel(store, bot=bot)
    code = service.code_for("alice")["code"]
    await service.dispatch(message(code, chat_id=-100999, chat_type="supergroup", sender_id=11))
    assert store.link("alice") is None
    assert bot.sent == []  # the channel does not even answer in a group
    assert store.redeem_code(code, 11, 0, tg_user_id=11, now=NOW) == "alice"


async def test_a_message_from_a_sender_other_than_the_chat_is_refused(store):
    bot = Bot()
    service = channel(store, bot=bot)
    code = service.code_for("alice")["code"]
    await service.dispatch(message(code, chat_id=11, sender_id=99))
    assert store.link("alice") is None
    assert bot.sent == []


async def test_a_linked_user_is_not_heard_through_someone_elses_message(store):
    bot = Bot()
    service = channel(store, bot=bot)
    store.redeem_code(service.code_for("alice")["code"], 11, 0, tg_user_id=11, now=NOW)
    await service.dispatch(message("/remind 10m stretch", chat_id=11, sender_id=99))
    assert store.reminders("alice") == []
    assert bot.sent == []


async def test_a_chat_gets_a_limited_number_of_code_attempts(store):
    bot = Bot()
    clock = [NOW]
    service = channel(store, bot=bot, clock=lambda: clock[0])
    for _ in range(5):
        await service.dispatch(message("/start DEADBEEF"))
    assert "not valid" in bot.sent[-1]["text"]  # five tries are allowed

    code = service.code_for("alice")["code"]
    await service.dispatch(message(code))
    assert "Too many" in bot.sent[-1]["text"]
    assert store.link("alice") is None  # the right code does not open a blocked chat

    clock[0] = NOW + 901  # the window passes: a fresh code works again
    await service.dispatch(message(service.code_for("alice")["code"]))
    assert store.link("alice").chat_id == 11


async def test_a_disabled_user_cannot_link_even_with_a_code(store):
    bot = Bot()
    service = channel(store, bot=bot, allowed=lambda user_id: False)
    code = service.code_for("alice")["code"]
    await service.dispatch(message(code))
    assert store.link("alice") is None
    assert "closed" in bot.sent[-1]["text"]


async def test_a_linked_but_disabled_user_is_refused_before_a_turn(store):
    bot = Bot()
    service = channel(store, bot=bot, allowed=lambda user_id: False)
    store.redeem_code(store.create_code("alice", now=NOW)["code"], 11, 0, now=NOW)
    await service.dispatch(message("hello"))
    assert "closed" in bot.sent[-1]["text"]
    assert store.offset() == 0  # the dispatcher does not touch the cursor


async def test_unlink_from_the_chat_forgets_the_link(store):
    bot = Bot()
    service = channel(store, bot=bot)
    store.redeem_code(service.code_for("alice")["code"], 11, 0, now=NOW)
    await service.dispatch(message("/unlink"))
    assert store.link("alice") is None
    assert "no longer linked" in bot.sent[-1]["text"]


def test_state_reports_the_link_and_the_reminders(store):
    service = channel(store)
    store.redeem_code(service.code_for("alice")["code"], 11, 0, now=NOW)
    store.add_reminder("alice", 11, 0, "stretch", due_at=NOW + 60, now=NOW)
    state = service.state("alice")
    assert state["enabled"] is True
    assert state["link"]["chat_id"] == 11
    assert [item["text"] for item in state["reminders"]] == ["stretch"]
    assert state["chats"] == []


def test_a_channel_that_is_off_refuses_codes(store):
    from web_chat.telegram.service import TelegramDisabled

    service = TelegramChannel(store, Spaces(), Settings(enabled=False), bot=Bot())
    with pytest.raises(TelegramDisabled):
        service.code_for("alice")


# -- starting up -----------------------------------------------------------
def test_the_channel_starts_only_with_a_flag_and_a_token():
    from web_chat.telegram import open_telegram
    from web_chat.telegram.config import ConfigError, Settings

    assert Settings.from_env({}).enabled is False
    # A token alone does not start it.
    token_only = Settings.from_env({"GRID_TELEGRAM_BOT_TOKEN": "123:abc"})
    assert token_only.enabled is False and token_only.token == "123:abc"
    assert open_telegram(":memory:", Spaces(), settings=token_only) is None
    # Asking for it without a token is an error, not a half-working bot.
    with pytest.raises(ConfigError):
        Settings.from_env({"GRID_TELEGRAM_ENABLED": "1"})
    ready = Settings.from_env({"GRID_TELEGRAM_ENABLED": "1", "GRID_TELEGRAM_BOT_TOKEN": "123:abc"})
    assert ready.enabled is True and "123:abc" not in repr(ready)


# -- the Bot API client ----------------------------------------------------
async def test_the_bot_client_long_polls_and_reports_refusals():
    import json

    import httpx

    from web_chat.telegram.bot import Bot, BotError

    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 3}]})
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    bot = Bot("123:secret", transport=httpx.MockTransport(handler))
    assert await bot.get_updates(3, timeout=1) == [{"update_id": 3}]
    sent = json.loads(requests[0].content)
    assert sent == {"offset": 3, "timeout": 1, "allowed_updates": ["message", "callback_query"]}
    with pytest.raises(BotError) as error:
        await bot.send_message(11, "hi")
    assert "Unauthorized" in str(error.value)
    # The token is in the URL, never in the error.
    assert "123:secret" not in str(error.value)
    await bot.close()


async def test_no_log_level_prints_the_request_url_with_the_token(caplog):
    import logging

    import httpx

    from web_chat.telegram.bot import Bot

    token = "123456:SECRET-TOKEN-VALUE"

    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": []})

    # The widest a deployment can open: httpx logs request URLs at INFO and
    # httpcore traces connections at DEBUG.
    caplog.set_level(logging.DEBUG)
    bot = Bot(token, transport=httpx.MockTransport(handler))
    await bot.get_updates(0, timeout=1)
    await bot.send_message(11, "hi")
    await bot.close()

    assert token not in caplog.text
    assert [record.name for record in caplog.records
            if record.name.startswith(("httpx", "httpcore"))] == []


def test_the_token_is_redacted_from_an_http_record(caplog):
    import logging

    from web_chat.telegram.bot import REDACTED, quiet_http_logging

    token = "123456:SECRET-TOKEN-VALUE"
    quiet_http_logging(token)
    caplog.set_level(logging.DEBUG)
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1,
        "HTTP Request: POST https://api.telegram.org/bot%s/getUpdates", (token,), None,
    )
    logging.getLogger("httpx").handle(record)

    assert token not in caplog.text
    assert REDACTED in caplog.text


# -- the web routes --------------------------------------------------------
def test_the_web_page_gets_a_code_links_state_and_unlinks(store):
    service = channel(store)
    app = FastAPI()
    api = APIRouter()

    async def current_user():
        return SimpleNamespace(id="alice")

    register_telegram_routes(api, current_user, service)
    app.include_router(api)
    client = TestClient(app)

    made = client.post("/api/telegram/link-code").json()
    assert len(made["code"]) == 8 and made["expires_at"] == NOW + 600
    # A new code replaces the old one.
    newer = client.post("/api/telegram/link-code").json()["code"]
    assert store.redeem_code(made["code"], 11, 0, now=NOW) is None
    assert store.redeem_code(newer, 11, 0, now=NOW) == "alice"

    state = client.get("/api/telegram/state").json()
    assert state["link"]["chat_id"] == 11

    after = client.delete("/api/telegram/link").json()
    assert after["link"] is None and store.link("alice") is None


def test_the_web_routes_say_the_channel_is_off(store):
    service = TelegramChannel(store, Spaces(), Settings(enabled=False), bot=Bot())
    app = FastAPI()
    api = APIRouter()

    async def current_user():
        return SimpleNamespace(id="alice")

    register_telegram_routes(api, current_user, service)
    app.include_router(api)
    client = TestClient(app)

    assert client.post("/api/telegram/link-code").status_code == 503
    assert client.get("/api/telegram/state").json()["enabled"] is False
