import json

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
        "byok": {
            "own_credentials": ["kimi", "kimi-code", "openrouter", "openai", "chatgpt"],
            "limits": {"usd_per_day": 3},
        },
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
    assert set(by_kind) == {"kimi", "kimi-code", "openrouter", "openai", "chatgpt"}
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
    await credentials_api.verify_key("kimi", "good", transport)
    assert str(seen[-1].url) == "https://api.moonshot.ai/v1/models"
    with pytest.raises(credentials_api.KeyRejected):
        await credentials_api.verify_key("openai", "nope", transport)
    redirecting = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "https://evil.test/"}))
    with pytest.raises(ConnectionError):
        await credentials_api.verify_key("openai", "secret", redirecting)
    unreachable = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    with pytest.raises(ConnectionError, match="Could not reach"):
        await credentials_api.verify_key("openai", "secret", unreachable)


@pytest.mark.asyncio
async def test_a_kimi_code_key_is_checked_by_running_the_model_it_will_be_asked_for():
    """A model list is not a working account: the check sends the same request
    the systems will send, with the short model id Kimi Code expects."""
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "k3.1-pro"}]})
        return httpx.Response(200, json={"id": "chatcmpl-1", "object": "chat.completion"})

    await credentials_api.verify_key("kimi-code", "good", httpx.MockTransport(handler))
    assert [str(request.url) for request in seen] == [
        "https://api.kimi.ai/coding/v1/models",
        "https://api.kimi.ai/coding/v1/chat/completions",
    ]
    assert json.loads(seen[1].content)["model"] == "k3.1-pro"


@pytest.mark.asyncio
async def test_a_key_whose_account_cannot_pay_is_not_stored_as_connected(parts, accounts, monkeypatch):
    """The panel must not say "connected" for an account with no balance: the
    provider's own refusal comes back typed, and nothing is kept."""
    server, vault, _usage, _verified = parts

    async def verify(kind, secret):
        raise credentials_api.KeyUnusable(
            "Kimi Code (Kimi Plus) accepted the key, but the account cannot run k3.1-pro: "
            "Your account is suspended due to insufficient balance",
            code="quota_exceeded",
        )

    monkeypatch.setattr(credentials_api, "verify_key", verify)
    user = accounts.create_user("bob", PASSWORD, tier="byok")
    client = signed_in(server, "bob")
    response = client.put("/api/credentials/kimi-code", json={"secret": "good-key-123456"})

    assert response.status_code == 402
    assert response.json()["code"] == "quota_exceeded"
    assert "insufficient balance" in response.json()["detail"]
    assert not vault.has(user.id, "kimi-code")


@pytest.mark.asyncio
async def test_a_spent_account_and_a_missing_model_are_told_apart():
    """The provider's body decides the code: the panel says what to fix."""
    quota = httpx.MockTransport(
        lambda request: httpx.Response(
            429,
            json={"error": {
                "type": "exceeded_current_quota_error",
                "message": "Your account is suspended due to insufficient balance, please recharge",
            }},
        )
    )
    with pytest.raises(credentials_api.KeyUnusable) as spent:
        await credentials_api.verify_key("kimi-code", "good", quota)
    assert spent.value.code == "quota_exceeded"

    def no_such_model(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"error": {"type": "model_not_found", "message": "no such model"}})

    with pytest.raises(credentials_api.KeyUnusable) as missing:
        await credentials_api.verify_key("kimi-code", "good", httpx.MockTransport(no_such_model))
    assert missing.value.code == "model_unavailable"


@pytest.mark.asyncio
async def test_a_plan_without_the_model_is_not_a_rejected_key():
    """Kimi Code answers 403 for a plan that lacks the model; the key is fine."""
    def planless(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(403, json={"error": {
            "type": "permission_error",
            "message": "Your account doesn't have access to the model",
        }})

    with pytest.raises(credentials_api.KeyUnusable) as refused:
        await credentials_api.verify_key("kimi-code", "good", httpx.MockTransport(planless))
    assert refused.value.code == "plan_lacks_model"
