"""Provider-specific cache fields must not leak to unsupported API surfaces."""
from types import SimpleNamespace as NS
import json

import httpx
import pytest
from openai import AsyncOpenAI

from core.factory.models import ModelProvider


def configured(url="https://openrouter.ai/api/v1", auth="api_key", **changes):
    config = NS(get_provider=lambda key: NS(base_url=url, auth=auth))
    model = NS(name="anthropic/claude", provider="provider-alias", prompt_cache=None,
               use_responses_api=False, reasoning={"enabled": False}, modalities=["text"])
    for key, value in changes.items():
        setattr(model, key, value)
    return ModelProvider(config, None, None), model


@pytest.mark.parametrize("url,changes,enabled", [
    ("https://openrouter.ai/api/v1", {}, True),
    ("https://openrouter.ai/api/v1", {"prompt_cache": False}, False),
    ("https://openrouter.ai/api/v1", {"name": "other"}, False),
    ("https://openrouter.ai/api/v1", {"name": "other", "prompt_cache": True}, True),
    ("https://api.openai.com/v1", {"prompt_cache": True}, False),
    ("https://openrouter.ai.evil/v1", {"prompt_cache": True}, False),
    ("http://localhost:8000/v1", {"prompt_cache": True}, False),
    ("https://openrouter.ai/api/v1", {"use_responses_api": True, "prompt_cache": True}, False),
])
def test_cache_capability(url, changes, enabled):
    provider, model = configured(url, **changes)
    body = provider.settings(model).extra_body
    assert ("cache_control" in body) is enabled
    assert body["reasoning"] == {"enabled": False}
    assert body["modalities"] == ["text"]


def test_unknown_config_and_plan_never_send_cache_control():
    provider, model = configured(auth="chatgpt", prompt_cache=True)
    assert "cache_control" not in provider.settings(model).extra_body
    assert "cache_control" not in ModelProvider(None, None, None).settings(model).extra_body


@pytest.mark.asyncio
@pytest.mark.parametrize("url,enabled", [("https://openrouter.ai/api/v1", True), ("https://api.openai.com/v1", False)])
async def test_openai_transport_serializes_only_supported_cache_field(url, enabled):
    provider, model = configured(url, prompt_cache=True)
    payloads = []

    async def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "r", "object": "chat.completion", "created": 1,
                                        "model": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with AsyncOpenAI(api_key="test", base_url=url, http_client=http) as client:
            await client.chat.completions.create(model=model.name, messages=[{"role": "user", "content": "hello"}],
                                                 extra_body=provider.settings(model).extra_body)
    assert ("cache_control" in payloads[0]) is enabled
    assert payloads[0]["reasoning"] == {"enabled": False}
