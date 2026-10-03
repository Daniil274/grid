"""OpenAI Responses model that keeps a streamed response's output items.

The Agents SDK takes a streamed response's output from its final
``response.completed`` event alone (``agents/run.py``, SDK 0.7.0). A server
that does not store the response (``store: false``; a ChatGPT plan always
works so) may send that event with an empty ``output``: the items - messages,
reasoning, tool calls - came only in ``response.output_item.done`` events.
The SDK then sees a step with no tool calls and no text, ends the run with an
empty answer, and the tool calls the stream showed never run.

This subclass fills an empty ``output`` of ``response.completed`` with the
items the stream delivered, in their ``output_index`` order. A completed event
that has its output is passed on untouched, so other servers see no change.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict

from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseOutputItemDoneEvent,
    ResponseStreamEvent,
)

from agents import OpenAIResponsesModel


def with_streamed_output(
    event: ResponseCompletedEvent, items: Dict[int, Any]
) -> ResponseCompletedEvent:
    """*event* with *items* as its output when the server sent it none."""
    if event.response.output or not items:
        return event
    output = [items[index] for index in sorted(items)]
    return event.model_copy(update={"response": event.response.model_copy(update={"output": output})})


class StreamedOutputResponsesModel(OpenAIResponsesModel):
    """An OpenAIResponsesModel whose final streamed event carries every output item."""

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[ResponseStreamEvent]:
        items: Dict[int, Any] = {}
        async for event in super().stream_response(*args, **kwargs):
            if isinstance(event, ResponseOutputItemDoneEvent):
                items[event.output_index] = event.item
            elif isinstance(event, ResponseCompletedEvent):
                event = with_streamed_output(event, items)
            yield event
