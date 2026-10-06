"""A model on a ChatGPT plan makes the request OpenAI's plan usage accepts."""

import json
from types import SimpleNamespace

import httpx
import pytest

from core.bounded_model import BoundedModel
from agents.models.interface import ModelTracing

from core import model_access
from core.config import Config
from core.factory.models import ModelProvider
from core.model_access import ModelAccess
from web_chat.entitlements import UserCredentials, Plans
from schemas.schemas import GridConfig

CONFIG = """
providers:
  plan:
    name: plan
    base_url: https://api.openai.com/v1
    auth: chatgpt
    price_source: openai
  keyed:
    name: keyed
    base_url: https://llm.test/v1
    api_key: sk-test
models:
  gpt:
    name: gpt-5.4
    provider: plan
    max_tokens: 4000
    context_window: 100000
  other:
    name: other-model
    provider: keyed
    max_tokens: 4000
agents:
  main:
    name: main
    model: gpt
    prompt: x
"""


def sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


COMPLETED = {
    "type": "response.completed",
    "sequence_number": 1,
    "response": {
        "id": "resp_1", "object": "response", "created_at": 1.0, "model": "gpt-5.4", "status": "completed",
        "output": [], "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        "usage": {
            "input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0},
        },
    },
}


class Login:
    def __init__(self):
        self.used = 0

    def has(self, user_id, kind):
        return kind == "chatgpt"

    async def credential(self, user_id, kind):
        from core.credentials import Credential

        self.used += 1
        return Credential("plan-token", "chatgpt", charged=False, subscription=True)


@pytest.mark.asyncio
async def test_a_plan_model_is_signed_with_the_users_token_and_asks_for_what_the_plan_allows(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    config = Config(str(path))
    seen, spent = [], []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=sse(COMPLETED), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(model_access.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))
    plans = Plans(lambda: GridConfig())
    credentials = UserCredentials("me", lambda: plans.resolve(admin=True), plans, Login())
    from core.pricing import PriceBook

    book = PriceBook.from_models_dev({"openai": {"models": {"gpt-5.4": {"cost": {"input": 2.5, "output": 15}}}}})
    access = ModelAccess(credentials, on_spend=spent.append, prices=book)
    support = SimpleNamespace(access=access)
    models = ModelProvider(config, support, None)

    model, model_config = models.sdk_model("gpt")
    settings = models.settings(model_config)
    assert settings.max_tokens is None and settings.store is False
    # The request carries no cap, so max_tokens is kept on this side.
    assert isinstance(model, BoundedModel) and model.max_output_tokens == 4000

    events = [
        event
        async for event in model.stream_response(
            "Be brief.", "hi", settings, [], None, [], ModelTracing.DISABLED, previous_response_id=None,
            conversation_id=None, prompt=None,
        )
    ]
    assert events
    request = seen[0]
    body = json.loads(request.content)
    assert request.url.path == "/v1/responses"
    assert request.headers["authorization"] == "Bearer plan-token"
    assert body["store"] is False and body["stream"] is True
    # an unstored reasoning item comes back with what it takes to send it again
    assert body["include"] == ["reasoning.encrypted_content"]
    assert "max_output_tokens" not in body and "temperature" not in body and "previous_response_id" not in body
    assert body["instructions"] == "Be brief."
    assert all(item.get("role") != "system" for item in body["input"])
    # a subscription call: priced as the API would price it, but not charged
    (event,) = spent
    assert (event.source, event.charged, event.subscription, event.cost.basis) == ("chatgpt", False, True, "subscription")
    assert event.cost.micro == 1000 * 2.5 + 200 * 15


def test_a_keyed_provider_is_unchanged_and_a_plan_provider_refuses_the_environments_key(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    config = Config(str(path))
    models = ModelProvider(config, SimpleNamespace(access=ModelAccess()), None)
    assert models.settings(config.get_model("other")).max_tokens == 4000
    assert models.settings(config.get_model("other")).store is None
    assert models.settings(config.get_model("other")).response_include is None
    from core.credentials import CredentialError

    with pytest.raises(CredentialError, match="ChatGPT"):
        models.sdk_model("gpt")  # unmanaged: no signed-in user, and never an API key for it


def test_a_response_timeout_bounds_a_keyed_model_and_none_leaves_it_bare(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    config = Config(str(path))
    models = ModelProvider(config, SimpleNamespace(access=ModelAccess()), None)

    bare, _ = models.sdk_model("other")
    assert not isinstance(bare, BoundedModel)

    config.get_model("other").response_timeout = 600
    bounded, _ = models.sdk_model("other")
    assert isinstance(bounded, BoundedModel)
    assert (bounded.timeout, bounded.max_output_tokens) == (600, None)  # the provider caps the output
