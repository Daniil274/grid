"""A model served only over the Responses API is summarized there."""

from types import SimpleNamespace

import pytest

from core.compact import CompactionStatus, CompactMessage
from core.compact.compact_conversation import compact_conversation


class _Stream:
    def __init__(self, events):
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


class _Responses:
    def __init__(self):
        self.request = None

    async def create(self, **kwargs):
        self.request = kwargs
        return _Stream(
            [
                SimpleNamespace(type="response.output_text.delta", delta="Short "),
                SimpleNamespace(type="response.output_text.delta", delta="summary."),
                SimpleNamespace(type="response.completed"),
            ]
        )


class _Chat:
    @property
    def completions(self):
        raise AssertionError("Chat Completions must not be called")


def _messages():
    long = "A long exchange about the project. " * 200
    return [
        CompactMessage(role="system", content="Be terse.", message_id="s"),
        CompactMessage(role="user", content=long, message_id="m1"),
        CompactMessage(role="assistant", content=long, message_id="m2"),
    ]


@pytest.mark.asyncio
async def test_summary_goes_over_responses_api():
    client = SimpleNamespace(responses=_Responses(), chat=_Chat())

    result = await compact_conversation(
        messages=_messages(), llm_client=client, model="muse", responses_api={}
    )

    assert result.status == CompactionStatus.SUCCESS
    assert "Short summary." in result.summary_messages[0].get_text()
    request = client.responses.request
    assert request["stream"] is True
    assert request["instructions"] == "Be terse."
    assert [m["role"] for m in request["input"]] == ["user", "assistant", "user"]
    assert request["max_output_tokens"] > 0


@pytest.mark.asyncio
async def test_plan_options_drop_the_output_cap():
    client = SimpleNamespace(responses=_Responses(), chat=_Chat())

    await compact_conversation(
        messages=_messages(),
        llm_client=client,
        model="gpt",
        responses_api={"store": False, "max_output_tokens": None},
    )

    assert client.responses.request["store"] is False
    assert "max_output_tokens" not in client.responses.request
