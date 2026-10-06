"""Client-side limits on one model request (core.bounded_model)."""

import asyncio
from types import SimpleNamespace

import pytest

from core.bounded_model import CHARS_PER_TOKEN, BoundedModel, ModelRequestLimit
from core.factory.failures import is_retriable


def delta(kind: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(type=f"response.{kind}.delta", delta=text)


class Scripted:
    """A model whose stream yields *events*, then waits *then_wait* seconds per event forever."""

    def __init__(self, events, *, then_wait=None, answer=None, answer_after=0.0):
        self.events = list(events)
        self.then_wait = then_wait
        self.answer = answer
        self.answer_after = answer_after
        self.closed = False

    async def get_response(self, *args, **kwargs):
        await asyncio.sleep(self.answer_after)
        return self.answer

    async def stream_response(self, *args, **kwargs):
        try:
            for event in self.events:
                yield event
            while self.then_wait is not None:
                await asyncio.sleep(self.then_wait)
                yield delta("output_text", "x")
        finally:
            self.closed = True


async def drain(model):
    return [event async for event in model.stream_response()]


@pytest.mark.asyncio
async def test_an_answer_that_keeps_streaming_is_cut_at_the_response_timeout():
    inner = Scripted([], then_wait=0.005)  # never pauses long enough for a read timeout
    model = BoundedModel(inner, key="m", timeout=0.1)

    with pytest.raises(ModelRequestLimit, match="within 0.1 s") as cut:
        await drain(model)

    assert inner.closed
    assert not is_retriable(cut.value)  # its message says "timeout", but a retry repeats it


@pytest.mark.asyncio
async def test_a_stalled_answer_is_cut_at_the_response_timeout():
    inner = Scripted([delta("output_text", "a")], then_wait=10)
    model = BoundedModel(inner, key="m", timeout=0.05)

    with pytest.raises(ModelRequestLimit):
        await drain(model)


@pytest.mark.asyncio
async def test_tool_call_arguments_past_max_tokens_cut_the_answer():
    chunk = "y" * 100
    events = [delta("function_call_arguments", chunk)] * 20
    model = BoundedModel(Scripted(events), key="plan", max_output_tokens=10 * 100 // CHARS_PER_TOKEN)

    seen = []
    with pytest.raises(ModelRequestLimit, match="max_tokens"):
        async for event in model.stream_response():
            seen.append(event)

    assert len(seen) == 10  # what was within the cap passed through


@pytest.mark.asyncio
async def test_an_answer_within_its_limits_passes_unchanged():
    events = [SimpleNamespace(type="response.created"), delta("output_text", "hi"), SimpleNamespace(type="response.completed")]
    model = BoundedModel(Scripted(events), key="m", timeout=5, max_output_tokens=100)

    assert await drain(model) == events


@pytest.mark.asyncio
async def test_the_providers_own_timeout_is_not_reported_as_the_limit():
    class Failing(Scripted):
        async def stream_response(self, *args, **kwargs):
            raise TimeoutError("provider")
            yield  # pragma: no cover

    with pytest.raises(TimeoutError, match="provider"):
        await drain(BoundedModel(Failing([]), key="m", timeout=5))


@pytest.mark.asyncio
async def test_a_non_streamed_request_is_bounded_too():
    slow = BoundedModel(Scripted([], answer="late", answer_after=10), key="m", timeout=0.05)
    with pytest.raises(ModelRequestLimit):
        await slow.get_response()

    quick = BoundedModel(Scripted([], answer="ok"), key="m", timeout=5)
    assert await quick.get_response() == "ok"
