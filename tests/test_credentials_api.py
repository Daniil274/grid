import httpx
import pytest

from schemas.schemas import GridConfig
from tests.test_web_accounts import PASSWORD, SAME_SITE, accounts, cheap_hashing, signed_in  # noqa: F401
from tests.test_web_spaces import FakeSpace
from web_chat import credentials_api
from web_chat.accounts.http import SessionAuth
from web_chat.entitlements import Plans
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool
from web_chat.usage import UsageStore
from web_chat.vault import Cipher, Vault

CONFIG = GridConfig(
    pools={"starter": {"K": "K2"}},
    tiers={
        "byok": {"own_credentials": ["openrouter", "openai", "chatgpt"], "limits": {"usd_per_day": 3}},
        "new": {"pool": "starter"},
    },
    default_tier="new",
)


class Deployment:
    plans = Plans(lambda: CONFIG)


@pytest.fixture
def parts(accounts, monkeypatch):
    verified = []

    async def verify(kind, secret):
        verified.append((kind, secret))
        if secret.startswith("bad"):
            raise credentials_api.KeyRejected("OpenRouter did not accept this key.")
        if secret.startswith("down"):
            raise ConnectionError("Could not reach OpenRouter to check the key: ConnectError.")

    monkeypatch.setattr(credentials_api, "verify_key", verify)
    vault = Vault(accounts.store, Cipher([Cipher.new_key()]))
    usage = UsageStore()
    server = WebChatServer(
        Deployment(), SpacePool(FakeSpace), auth=SessionAuth(accounts), warm_user=None, vault=vault, usage=usage
    )
    return server, vault, usage, verified


def test_the_plan_lists_what_the_user_may_connect_what_they_spent_and_never_a_secret(parts, accounts):
    server, vault, usage, _ = parts
    user = accounts.create_user("bob", PASSWORD, tier="byok")
    usage.record(event_id="e1", user_id=user.id, model="m", tokens_in=1, tokens_out=1, cost_micro=2_500_000)
    client = signed_in(server, "bob")
    client.put("/api/credentials/openrouter", json={"secret": "sk-or-v1-abcdefghijkl"})
    plan = client.get("/api/plan").json()
    assert plan["tier"] == "byok" and plan["limits"]["usd_per_day"] == 3
    assert plan["spent"]["day_usd"] == 2.5
    by_kind = {entry["kind"]: entry for entry in plan["credentials"]}
    assert set(by_kind) == {"openrouter", "openai", "chatgpt"}
    assert by_kind["openrouter"]["connected"] and by_kind["openrouter"]["hint"] == "…ijkl"
    assert not by_kind["openai"]["connected"]
    assert "abcdefghijkl" not in client.get("/api/plan").text


def test_a_key_is_checked_with_its_provider_before_it_is_kept(parts, accounts):
    server, vault, usage, verified = parts
    user = accounts.create_user("bob", PASSWORD, tier="byok")
    client = signed_in(server, "bob")
    assert client.put("/api/credentials/openrouter", json={"secret": "bad-key-123456"}).status_code == 400
    assert client.put("/api/credentials/openrouter", json={"secret": "down-key-123456"}).status_code == 502
    assert not vault.has(user.id, "openrouter")
    ok = client.put("/api/credentials/openrouter", json={"secret": "  good-key-123456  "})
    assert ok.status_code == 200 and vault.has(user.id, "openrouter")
    assert verified[-1] == ("openrouter", "good-key-123456")  # trimmed


def test_a_plan_that_does_not_take_own_keys_refuses_them(parts, accounts):
    server, vault, *_ = parts
    accounts.create_user("newbie", PASSWORD, tier="new")
    client = signed_in(server, "newbie")
    assert client.put("/api/credentials/openrouter", json={"secret": "good-key-123456"}).status_code == 403
    assert client.get("/api/plan").json()["credentials"] == []
    accounts.create_user("bob", PASSWORD, tier="byok")
    other = signed_in(server, "bob")
    assert other.put("/api/credentials/anthropic", json={"secret": "good-key-123456"}).status_code == 403


def test_a_key_can_be_removed_and_checks_are_rate_limited(parts, accounts):
    server, vault, *_ = parts
    user = accounts.create_user("bob", PASSWORD, tier="byok")
    client = signed_in(server, "bob")
    client.put("/api/credentials/openai", json={"secret": "good-key-123456"})
    assert client.delete("/api/credentials/openai").status_code == 200
    assert client.delete("/api/credentials/openai").status_code == 404
    assert not vault.has(user.id, "openai")
    codes = [client.put("/api/credentials/openai", json={"secret": "bad-key-123456"}).status_code for _ in range(12)]
    assert codes[-1] == 429


def test_without_a_secrets_key_the_server_says_so(accounts):
    vault = Vault(accounts.store, None)
    server = WebChatServer(Deployment(), SpacePool(FakeSpace), auth=SessionAuth(accounts), warm_user=None, vault=vault)
    accounts.create_user("bob", PASSWORD, tier="byok")
    client = signed_in(server, "bob")
    assert client.get("/api/plan").json()["vault_enabled"] is False
    assert client.put("/api/credentials/openai", json={"secret": "good-key-123456"}).status_code == 503


def test_another_users_session_cannot_reach_a_credential(parts, accounts):
    server, vault, *_ = parts
    owner = accounts.create_user("bob", PASSWORD, tier="byok")
    accounts.create_user("eve", PASSWORD, tier="byok")
    signed_in(server, "bob").put("/api/credentials/openai", json={"secret": "good-key-123456"})
    assert signed_in(server, "eve").delete("/api/credentials/openai").status_code == 404
    assert vault.has(owner.id, "openai")


@pytest.mark.asyncio
async def test_verification_goes_to_the_presets_address_and_follows_no_redirect():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200 if request.headers["authorization"] == "Bearer good" else 401)

    transport = httpx.MockTransport(handler)
    await credentials_api.verify_key("openrouter", "good", transport)
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/key"
    with pytest.raises(credentials_api.KeyRejected):
        await credentials_api.verify_key("openai", "nope", transport)
    redirecting = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://evil.test/"}))
    with pytest.raises(ConnectionError):
        await credentials_api.verify_key("openai", "secret", redirecting)
    unreachable = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    with pytest.raises(ConnectionError, match="Could not reach"):
        await credentials_api.verify_key("openai", "secret", unreachable)
