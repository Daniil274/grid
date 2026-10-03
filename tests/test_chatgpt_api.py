import urllib.parse

import httpx
import pytest
from fastapi.testclient import TestClient

from schemas.schemas import GridConfig
from tests.test_chatgpt import CLIENT, NOW, OpenAI
from tests.test_web_accounts import accounts, cheap_hashing  # noqa: F401
from tests.test_web_spaces import FakeSpace
from web_chat import chatgpt
from web_chat.chatgpt import ChatGPTLogin
from web_chat.entitlements import Plans
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool
from web_chat.usage import UsageStore
from web_chat.vault import local_vault


class Deployment:
    plans = Plans(lambda: GridConfig())


@pytest.fixture
def parts(tmp_path):
    openai = OpenAI()
    login = ChatGPTLogin("urn:uuid:host", transport=httpx.MockTransport(openai), clock=lambda: NOW)
    vault = local_vault(tmp_path, oauth={"chatgpt": login})
    server = WebChatServer(
        Deployment(), SpacePool(FakeSpace), vault=vault, chatgpt=login, usage=UsageStore(), warm_user=None
    )
    return openai, login, vault, server


def local(server):
    return TestClient(server.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 5000), headers={"Origin": "http://127.0.0.1:8000"})


def start(client, openai):
    url = client.post("/api/chatgpt/connect").json()["url"]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    openai.nonce = query["nonce"]
    return query


def test_a_local_browser_signs_in_and_the_plan_login_is_stored_for_the_user(parts):
    openai, login, vault, server = parts
    client = local(server)
    query = start(client, openai)
    assert query["redirect_uri"] == "http://127.0.0.1:8000/auth/callback"
    done = client.get(
        "/auth/callback",
        params={"code": "c", "state": query["state"], "client_id": CLIENT, "scope": chatgpt.SCOPES},
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == "/#chatgpt=connected"
    plan = client.get("/api/plan").json()
    entry = next(e for e in plan["credentials"] if e["kind"] == "chatgpt")
    assert entry["connected"] and entry["hint"] == "me@example.com" and plan["chatgpt_available"]
    assert "access-1" not in client.get("/api/plan").text


def test_a_server_reached_over_the_network_cannot_start_the_sign_in(parts):
    _, _, _, server = parts
    remote = TestClient(server.app, base_url="https://chat.example.com", client=("203.0.113.9", 5000),
                        headers={"Origin": "https://chat.example.com"})
    response = remote.post("/api/chatgpt/connect")
    assert response.status_code == 400 and "your own computer" in response.json()["detail"]
    # even at 127.0.0.1 in the Host header, a client from elsewhere is refused
    spoof = TestClient(server.app, base_url="http://127.0.0.1:8000", client=("203.0.113.9", 5000),
                       headers={"Origin": "http://127.0.0.1:8000"})
    assert spoof.post("/api/chatgpt/connect").status_code == 400


def test_a_callback_with_an_error_a_forged_state_or_a_refused_plan_stores_nothing(parts):
    openai, login, vault, server = parts
    client = local(server)
    denied = client.get("/auth/callback", params={"error": "access_denied", "error_description": "No."}, follow_redirects=False)
    assert denied.headers["location"] == "/#chatgpt=error%3ANo."
    start(client, openai)
    forged = client.get(
        "/auth/callback", params={"code": "c", "state": "forged", "client_id": CLIENT}, follow_redirects=False
    )
    assert forged.headers["location"].startswith("/#chatgpt=error")
    assert not vault.has("default_user", "chatgpt")


def test_signing_out_revokes_the_login_and_deletes_it(parts):
    openai, login, vault, server = parts
    client = local(server)
    query = start(client, openai)
    client.get("/auth/callback", params={"code": "c", "state": query["state"], "client_id": CLIENT, "scope": chatgpt.SCOPES})
    assert vault.has("default_user", "chatgpt")
    assert client.delete("/api/credentials/chatgpt").status_code == 200
    assert not vault.has("default_user", "chatgpt")
    assert any(r.url.path.endswith("/revoke") for r in openai.requests)


def test_only_plans_that_list_chatgpt_may_sign_in(tmp_path, accounts):
    from tests.test_web_accounts import PASSWORD
    from web_chat.accounts.http import SessionAuth

    config = GridConfig(tiers={"new": {"pool": "default"}, "plus": {"own_credentials": ["chatgpt"]}}, default_tier="new")

    class Tiered:
        plans = Plans(lambda: config)

    openai = OpenAI()
    login = ChatGPTLogin("h", transport=httpx.MockTransport(openai), clock=lambda: NOW)
    server = WebChatServer(Tiered(), SpacePool(FakeSpace), auth=SessionAuth(accounts), vault=local_vault(tmp_path),
                           chatgpt=login, usage=UsageStore(), warm_user=None)
    accounts.create_user("newbie", PASSWORD, tier="new")
    accounts.create_user("subscriber", PASSWORD, tier="plus")

    def sign_in(name):
        client = local(server)
        assert client.post("/api/auth/login", json={"username": name, "password": PASSWORD}).status_code == 200
        return client

    assert sign_in("newbie").post("/api/chatgpt/connect").status_code == 403
    assert sign_in("subscriber").post("/api/chatgpt/connect").status_code == 200
