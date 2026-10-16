"""Regression coverage for Responses-only summary models (no live credentials)."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from core.compact import CompactMessage
from core.compact.compact_conversation import compact_conversation
from core.factory.models import ModelProvider


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            yield NS(**event)

    async def close(self):
        self.closed = True


async def summarize(events, options=None, **kwargs):
    stream = Stream(events)
    create = AsyncMock(return_value=stream)
    client = NS(responses=NS(create=create))  # No Chat Completions API at all.
    result = await compact_conversation(
        [CompactMessage(role="system", content="Be terse."),
         CompactMessage(role="user", content="Long history " * 1000)],
        client, "summary-model", responses_api={} if options is None else options,
        **kwargs,
    )
    return result, create.call_args.kwargs, stream


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [False, True])
async def test_responses_request_and_summary(plan):
    options = {"store": False, "max_output_tokens": None} if plan else {}
    result, request, stream = await summarize([
        {"type": "response.output_text.delta", "delta": "Short summary"},
        {"type": "response.completed"},
    ], options)
    assert result.success()
    assert "Short summary" in result.summary_messages[0].get_text()
    assert request["instructions"] == "Be terse."
    assert [m["role"] for m in request["input"]] == ["user", "user"]
    assert request["stream"] is True
    assert ("max_output_tokens" in request) is not plan
    assert stream.closed
    assert options == ({"store": False, "max_output_tokens": None} if plan else {})


@pytest.mark.asyncio
@pytest.mark.parametrize("events,error", [
    ([{"type": "response.output_text.delta", "delta": "partial"}], RuntimeError),
    ([{"type": "response.incomplete"}], RuntimeError),
    ([{"type": "response.failed"}], RuntimeError),
    ([{"type": "error"}], RuntimeError),
    ([{"type": "response.completed"}], ValueError),
    ([{"type": "response.output_text.delta", "delta": "x" * 4001}], ValueError),
])
async def test_bad_stream_never_becomes_summary(events, error):
    stream = Stream(events)
    client = NS(responses=NS(create=AsyncMock(return_value=stream)))
    with pytest.raises(error):
        await compact_conversation([CompactMessage(role="user", content="history" * 1000)],
                                   client, "m", responses_api={}, max_output_tokens=1000)
    assert stream.closed


@pytest.mark.parametrize("summary,plan,responses,expected", [
    (None, False, False, None),
    (None, False, True, {}),
    ("summary", True, False, {"store": False, "max_output_tokens": None}),
])
def test_summary_api_resolved_from_selected_model(summary, plan, responses, expected):
    cfg = NS(provider="p", use_responses_api=responses)
    config = NS(get_model=lambda key: cfg, get_provider=lambda key: NS(auth="chatgpt" if plan else "api_key"))
    runtime = NS(resolve_model_key=lambda key: "primary", get_openai_client_for_model=lambda key: (key, key))
    provider = ModelProvider(config, runtime, NS(summary_model=summary))
    assert provider.compact_request("agent") == (summary or "primary", summary or "primary", expected)


def test_unavailable_summary_falls_back_with_matching_api():
    def client(key):
        if key == "missing":
            raise KeyError(key)
        return key, key
    config = NS(get_model=lambda key: NS(provider="p", use_responses_api=True),
                get_provider=lambda key: NS(auth="api_key"))
    runtime = NS(resolve_model_key=lambda key: "primary", get_openai_client_for_model=client)
    provider = ModelProvider(config, runtime, NS(summary_model="missing"))
    assert provider.compact_request("agent") == ("primary", "primary", {})
