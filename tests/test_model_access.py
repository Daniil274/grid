import json
from types import SimpleNamespace

import httpx
import pytest

from core import model_access
from core.credentials import Credential, CredentialError, EnvCredentials
from core.model_access import MANAGED_KEY, ModelAccess, SpendEvent
from core.pricing import COMPUTED, REPORTED, SUBSCRIPTION, Price, PriceBook
from utils.exceptions import AgentError

PROVIDER = SimpleNamespace(
    name="p", base_url="https://llm.test/v1", api_key=None, api_key_env="P_KEY", default_headers={}, price_source=None
)


def _config(models=None):
    return SimpleNamespace(
        get_provider=lambda key: PROVIDER,
        get_proxy_for_provider=lambda key: None,
        get_api_key=lambda key: None,
        config=SimpleNamespace(models=models or {}),
    )


class FixedSource:
    def __init__(self, credential=None):
        self.credential_ = credential

    def available(self, provider):
        return self.credential_ is not None

    async def credential(self, provider):
        if self.credential_ is None:
            raise CredentialError("none")
        return self.credential_


def _serve(monkeypatch, usage, seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [], "usage": usage})

    monkeypatch.setattr(model_access.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))


def test_unmanaged_access_keeps_the_environment_key():
    config = _config()
    config.get_api_key = lambda key: "env-secret"
    assert ModelAccess().api_key(config, "p") == "env-secret"
    config.get_api_key = lambda key: None
    with pytest.raises(AgentError, match="API key not found"):
        ModelAccess().api_key(config, "p")


def test_managed_access_holds_no_secret_and_refuses_a_provider_without_credential():
    access = ModelAccess(FixedSource(Credential("s")))
    assert access.api_key(_config(), "p") == MANAGED_KEY
    with pytest.raises(CredentialError, match="No credential"):
        ModelAccess(FixedSource(None)).api_key(_config(), "p")


@pytest.mark.asyncio
async def test_requests_are_signed_per_call_and_their_cost_reported(monkeypatch):
    seen, spent = [], []
    _serve(monkeypatch, {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.0042}, seen)
    source = FixedSource(Credential("pool-secret", "pool:friends"))
    access = ModelAccess(source, on_spend=spent.append)
    client = access.client(_config(), api_key=MANAGED_KEY, base_url=PROVIDER.base_url, provider_key="p")

    await client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    source.credential_ = Credential("rotated", "own", charged=False)  # applies to the next call at once
    await client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])

    assert [r.headers["authorization"] for r in seen] == ["Bearer pool-secret", "Bearer rotated"]
    first, second = spent
    assert isinstance(first, SpendEvent)
    assert (first.cost.micro, first.cost.basis, first.source, first.charged) == (4200, REPORTED, "pool:friends", True)
    assert (second.source, second.charged) == ("own", False)
    assert json.loads(seen[0].content)["model"] == "m"


@pytest.mark.asyncio
async def test_unreported_cost_is_computed_from_the_model_price_then_the_book(monkeypatch):
    seen, spent = [], []
    _serve(monkeypatch, {"prompt_tokens": 1_000_000, "completion_tokens": 0}, seen)
    declared = SimpleNamespace(provider="p", name="m", price={"input": 3, "output": 9})
    book = PriceBook.from_models_dev({"x": {"api": PROVIDER.base_url, "models": {"n": {"cost": {"input": 1, "output": 1}}}}})
    access = ModelAccess(EnvCredentials({"P_KEY": "k"}), on_spend=spent.append, prices=book)

    for model, models in (("m", {"m": declared}), ("n", {})):
        client = access.client(_config(models), api_key=MANAGED_KEY, base_url=PROVIDER.base_url, provider_key="p")
        await client.chat.completions.create(model=model, messages=[])
    assert [(e.cost.micro, e.cost.basis) for e in spent] == [(3_000_000, COMPUTED), (1_000_000, COMPUTED)]


@pytest.mark.asyncio
async def test_an_unpriced_model_is_charged_the_fallback_and_a_subscription_is_labelled(monkeypatch):
    seen, spent = [], []
    _serve(monkeypatch, {"prompt_tokens": 1_000_000, "completion_tokens": 0}, seen)
    fallback = Price.from_mapping({"input": 5, "output": 5})
    source = FixedSource(Credential("t", "chatgpt", charged=False, subscription=True))
    access = ModelAccess(source, on_spend=spent.append, unknown_price=fallback)
    client = access.client(_config(), api_key=MANAGED_KEY, base_url=PROVIDER.base_url, provider_key="p")
    await client.chat.completions.create(model="unpriced", messages=[])
    assert (spent[0].cost.micro, spent[0].cost.basis, spent[0].charged) == (5_000_000, SUBSCRIPTION, False)


async def test_a_spend_tally_counts_the_calls_of_its_scope_and_of_the_tasks_it_starts(monkeypatch):
    import asyncio

    from core.model_access import tally_spend

    seen, spent = [], []
    _serve(monkeypatch, {"prompt_tokens": 1_000_000, "completion_tokens": 0}, seen)
    declared = SimpleNamespace(provider="p", name="m", price={"input": 2, "output": 2})
    access = ModelAccess(EnvCredentials({"P_KEY": "k"}), on_spend=spent.append)
    client = access.client(_config({"m": declared}), api_key=MANAGED_KEY, base_url=PROVIDER.base_url, provider_key="p")

    async def call():
        await client.chat.completions.create(model="m", messages=[])

    await call()  # outside any scope
    with tally_spend() as outer:
        await call()
        with tally_spend() as inner:
            await asyncio.gather(call(), call())  # tasks started inside the scope
    assert (outer.calls, outer.micro) == (3, 6_000_000)
    assert (inner.calls, inner.micro) == (2, 4_000_000)
    assert len(spent) == 4  # the sink still sees every call
