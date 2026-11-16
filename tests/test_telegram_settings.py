"""The bot token from the web interface: sealed, hinted, and never handed back.

What these tests hold the server to:

- an admin's token is sealed in the vault (the stored bytes are not the token),
  and neither the admin's API nor the user's ever answers with it - only a hint;
- a non-admin cannot set or remove one;
- the channel starts on a stored token and stops when it is removed, without a
  restart of the server;
- GRID_TELEGRAM_BOT_TOKEN in the environment still wins;
- the link code comes with a t.me link that spends it, and unlinking forgets the
  chat;
- the routes exist when the channel is off, and say so.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from web_chat.identity import admins_only
from web_chat.telegram.bot import BotError
from web_chat.telegram.config import ENABLED_ENV, TOKEN_ENV, ConfigError, Settings
from web_chat.telegram.http import (
    register_telegram_admin_routes,
    register_telegram_page,
    register_telegram_routes,
)
from web_chat.telegram.manager import TelegramManager
from web_chat.telegram.secrets import KIND, OWNER
from web_chat.telegram.service import TelegramDisabled
from web_chat.vault import Cipher, FileCredentialStore, Vault

NOW = 1_000_000.0
TOKEN = "123456789:AAH_test-token_value-0123456789"
OTHER = "987654321:AAF_other-token_value-9876543210"
ENV_TOKEN = "555555555:AAE_from-the-environment-0123456789"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Neither the environment nor a stray token decides what a test sees."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(ENABLED_ENV, raising=False)


@pytest.fixture
def store(tmp_path):
    return FileCredentialStore(tmp_path / "credentials.json")


@pytest.fixture
def vault(store):
    return Vault(store, Cipher([Cipher.new_key()]))


class FakeBot:
    """A bot double: who it is, and what it was asked; no network."""

    def __init__(self, token, *, username="grid_test_bot", refuse=False):
        self.token = token
        self.username = username
        self.refuse = refuse
        self.closed = False

    async def get_me(self):
        if self.refuse:
            raise BotError("Unauthorized")
        return {"id": 1, "username": self.username}

    async def get_updates(self, offset, timeout=25):
        return []

    async def close(self):
        self.closed = True


class Bots:
    """The bot factory a manager is given: one :class:`FakeBot` per token."""

    def __init__(self, **options):
        self.options = options
        self.tokens = []
        self.bots = []

    def __call__(self, token):
        bot = FakeBot(token, **self.options)
        self.tokens.append(token)
        self.bots.append(bot)
        return bot

    @property
    def last(self):
        return self.bots[-1]


class Spaces:
    """A space pool double: the channel never runs a turn in these tests."""

    def use(self, user_id):
        raise AssertionError("no turn is run here")

    def __init__(self):
        self.used = []


def manager(tmp_path, *, vault=None, bots=None):
    return TelegramManager(
        tmp_path / "telegram.db",
        Spaces(),
        vault=vault,
        bot_factory=bots or Bots(),
        clock=lambda: NOW,
    )


def as_user(user_id="alice", *, admin=False):
    user = SimpleNamespace(id=user_id, username=user_id, role="admin" if admin else "user", is_admin=admin)

    async def current_user():
        return user

    return current_user


def client_for(telegram, *, admin=False):
    """A page whose one user is *telegram*'s; the admin routes behind the role check."""
    current_user = as_user(admin=admin)
    app = FastAPI()
    api = APIRouter()
    register_telegram_routes(api, current_user, telegram)
    register_telegram_admin_routes(api, admins_only(current_user), telegram)
    register_telegram_page(app, current_user, telegram)
    app.include_router(api)
    return TestClient(app, headers={"Origin": "http://testserver"})


# -- the token at rest -----------------------------------------------------
async def test_the_token_is_stored_sealed_and_the_api_only_hints_at_it(tmp_path, vault, store):
    telegram = manager(tmp_path, vault=vault)
    telegram.secrets.store(TOKEN)

    row = store.credential(OWNER, KIND)
    assert row is not None
    assert TOKEN.encode() not in row.ciphertext
    assert row.hint == "…" + TOKEN[-5:]

    status = await telegram.status()
    assert status["source"] == "vault"
    assert status["hint"] == "…" + TOKEN[-5:]
    assert TOKEN not in json.dumps(status)


async def test_a_server_without_a_vault_cannot_store_a_token(tmp_path):
    telegram = manager(tmp_path, vault=None)
    with pytest.raises(TelegramDisabled):
        await telegram.apply_token(TOKEN)


async def test_the_token_is_never_written_to_a_log(tmp_path, vault, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    telegram = manager(tmp_path, vault=vault)
    await telegram.apply_token(TOKEN)
    assert TOKEN not in caplog.text
    await telegram.close()


# -- who may set it --------------------------------------------------------
def test_a_non_admin_cannot_set_or_forget_the_token(tmp_path, vault):
    telegram = manager(tmp_path, vault=vault)
    client = client_for(telegram, admin=False)

    assert client.get("/api/admin/telegram").status_code == 403
    assert client.put("/api/admin/telegram", json={"token": TOKEN}).status_code == 403
    assert client.delete("/api/admin/telegram").status_code == 403
    assert telegram.secrets.token() == ""


def test_a_body_that_is_not_a_token_is_refused_without_repeating_it(tmp_path, vault):
    telegram = manager(tmp_path, vault=vault)
    client = client_for(telegram, admin=True)

    shaped = client.put("/api/admin/telegram", json={"token": "definitely-not-a-token"})
    assert shaped.status_code == 400
    assert "definitely-not-a-token" not in shaped.text

    assert client.put("/api/admin/telegram", json={"token": 42}).status_code == 400
    assert client.put("/api/admin/telegram", json={"nope": TOKEN}).status_code == 400
    assert telegram.secrets.token() == ""


# -- starting and stopping at runtime ---------------------------------------
async def test_the_channel_starts_and_stops_without_a_restart(tmp_path, vault):
    bots = Bots()
    telegram = manager(tmp_path, vault=vault, bots=bots)

    await telegram.start()
    assert telegram.channel is None and not telegram.enabled

    status = await telegram.apply_token(TOKEN)
    assert status["enabled"] is True and status["source"] == "vault"
    assert telegram.channel is not None
    assert telegram.channel.settings.token == TOKEN
    assert bots.tokens[-1] == TOKEN

    status = await telegram.forget_token()
    assert status["enabled"] is False and status["source"] is None
    assert telegram.channel is None
    assert bots.last.closed is True
    await telegram.close()


async def test_a_refused_token_is_not_stored(tmp_path, vault):
    telegram = manager(tmp_path, vault=vault, bots=Bots(refuse=True))
    with pytest.raises(TelegramDisabled):
        await telegram.apply_token(TOKEN)
    assert telegram.secrets.token() == ""
    assert telegram.channel is None
    await telegram.close()


# -- the environment keeps priority ----------------------------------------
async def test_the_environment_token_wins_over_the_stored_one(tmp_path, vault, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, ENV_TOKEN)
    monkeypatch.setenv(ENABLED_ENV, "1")
    telegram = manager(tmp_path, vault=vault)
    telegram.secrets.store(TOKEN)

    await telegram.start()
    assert telegram.channel is not None
    assert telegram.channel.settings.token == ENV_TOKEN
    status = await telegram.status()
    assert status["source"] == "env" and status["from_env"] is True
    assert status["hint"] == "…" + ENV_TOKEN[-5:]

    # Removing the stored token does not touch what the environment says.
    await telegram.forget_token()
    assert telegram.channel is not None and telegram.channel.settings.token == ENV_TOKEN
    assert telegram.secrets.token() == ""
    await telegram.close()


async def test_an_environment_token_alone_keeps_the_channel_off(tmp_path, vault, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, ENV_TOKEN)
    telegram = manager(tmp_path, vault=vault)
    await telegram.start()
    assert telegram.channel is None
    assert (await telegram.status())["source"] == "env"


def test_the_environment_asking_for_a_channel_without_a_token_is_an_error(monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, "1")
    with pytest.raises(ConfigError):
        Settings.from_sources("")
    assert Settings.from_sources("", {ENABLED_ENV: "1", TOKEN_ENV: "123:abc"}).token == "123:abc"


# -- the user's link -------------------------------------------------------
async def test_the_link_code_comes_with_a_t_me_link(tmp_path, vault):
    telegram = manager(tmp_path, vault=vault)
    await telegram.start()
    await telegram.apply_token(TOKEN)
    client = client_for(telegram)

    made = client.post("/api/telegram/link-code").json()
    assert len(made["code"]) == 8
    assert made["bot_username"] == "grid_test_bot"
    assert made["url"] == f"https://t.me/grid_test_bot?start={made['code']}"
    assert TOKEN not in json.dumps(made)

    assert telegram.channel.store.redeem_code(made["code"], 11, 0, now=NOW) == "alice"
    state = client.get("/api/telegram/state").json()
    assert state["enabled"] is True and state["link"]["chat_id"] == 11

    after = client.delete("/api/telegram/link").json()
    assert after["link"] is None
    assert telegram.channel.store.link("alice") is None
    await telegram.close()


async def test_the_admin_page_sets_the_token_it_sees_the_channel_on(tmp_path, vault):
    bots = Bots()
    telegram = manager(tmp_path, vault=vault, bots=bots)
    client = client_for(telegram, admin=True)

    before = client.get("/api/admin/telegram").json()
    assert before["enabled"] is False and before["can_store"] is True

    saved = client.put("/api/admin/telegram", json={"token": TOKEN})
    assert saved.status_code == 200
    body = saved.json()
    assert body["enabled"] is True and body["source"] == "vault"
    assert body["hint"] == "…" + TOKEN[-5:]
    assert TOKEN not in saved.text

    # The user's own card knows the channel is on now.
    assert client.post("/api/telegram/link-code").status_code == 200

    removed = client.delete("/api/admin/telegram")
    assert removed.status_code == 200
    assert removed.json()["enabled"] is False
    assert client.post("/api/telegram/link-code").status_code == 503
    await telegram.close()


async def test_admin_status_never_names_the_environment_token(tmp_path, vault, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, ENV_TOKEN)
    monkeypatch.setenv(ENABLED_ENV, "1")
    telegram = manager(tmp_path, vault=vault)
    await telegram.start()
    client = client_for(telegram, admin=True)

    response = client.get("/api/admin/telegram")
    assert response.status_code == 200
    assert response.json()["source"] == "env"
    assert ENV_TOKEN not in response.text
    await telegram.close()


# -- the routes are always there -------------------------------------------
async def test_the_routes_say_the_channel_is_off(tmp_path, vault):
    telegram = manager(tmp_path, vault=vault)
    await telegram.start()
    client = client_for(telegram)

    state = client.get("/api/telegram/state")
    assert state.status_code == 200
    assert state.json()["enabled"] is False and state.json()["available"] is True
    assert client.post("/api/telegram/link-code").status_code == 503
    assert client.delete("/api/telegram/link").status_code == 200
    await telegram.close()


def test_a_server_that_keeps_no_channel_says_so():
    client = client_for(None, admin=True)

    state = client.get("/api/telegram/state")
    assert state.status_code == 200
    assert state.json()["enabled"] is False and state.json()["available"] is False
    assert client.get("/api/admin/telegram").json()["can_store"] is False


def test_the_telegram_page_is_served():
    client = client_for(None)

    page = client.get("/telegram")
    assert page.status_code == 200
    assert "Telegram" in page.text
    assert "/static/js/settings/telegram-page.js" in page.text


def test_a_cross_site_request_cannot_set_the_token(tmp_path):
    """The admin routes get the origin check every admin API has, from the server."""
    from tests.test_web_ui_routes import _DummyDeployment, _DummySpace
    from web_chat.server import WebChatServer
    from web_chat.spaces import SpacePool

    server = WebChatServer(
        _DummyDeployment(),
        SpacePool(lambda user_id: _DummySpace()),
        telegram_path=tmp_path / "telegram.db",
        warm_user=None,
        trusted_hosts=("testserver",),
    )
    with TestClient(server.app) as client:  # no Origin, no Sec-Fetch-Site
        refused = client.put(
            "/api/admin/telegram", json={"token": TOKEN}, headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert refused.status_code == 403
        assert TOKEN not in refused.text

    with TestClient(server.app, headers={"Origin": "http://testserver"}) as client:
        assert client.get("/api/admin/telegram").status_code == 200
