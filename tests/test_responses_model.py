"""A streamed response whose completed event lists no output still runs its tool calls.

The SDK's run loop, the OpenAI client and its SSE parsing are real; only the
HTTP transport is replaced by recorded streams.
"""

import json

import httpx
from agents import Agent, Runner, function_tool
from openai import AsyncOpenAI
from openai.types.responses import ResponseCompletedEvent

from core.responses_model import StreamedOutputResponsesModel, with_streamed_output
from tests.test_chatgpt_requests import COMPLETED, sse

FUNCTION_CALL = {
    "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup",
    "arguments": '{"text": "grid"}', "status": "completed",
}
MESSAGE = {
    "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
    "content": [{"type": "output_text", "text": "Found it.", "annotations": []}],
}


def item_done(item, index=0):
    return {"type": "response.output_item.done", "sequence_number": 0, "output_index": index, "item": item}


def completed(*output):
    return {**COMPLETED, "response": {**COMPLETED["response"], "output": list(output)}}


def model_over(*streams):
    """A model whose requests are answered by *streams*, one per request; the requests it made."""
    replies, seen = list(streams), []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=sse(*replies.pop(0)), headers={"content-type": "text/event-stream"})

    client = AsyncOpenAI(
        api_key="test", base_url="https://llm.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return StreamedOutputResponsesModel(model="m", openai_client=client), seen


async def run_streamed(model, tool):
    result = Runner.run_streamed(Agent(name="worker", model=model, tools=[tool]), "Look it up", max_turns=3)
    async for _ in result.stream_events():
        pass
    return result


async def test_tool_calls_missing_from_the_completed_event_are_run():
    looked_up = []
    lookup = function_tool(lambda text: looked_up.append(text) or "a grid", name_override="lookup")
    model, seen = model_over(
        [item_done(FUNCTION_CALL), completed()],
        [item_done(MESSAGE), completed()],
    )

    result = await run_streamed(model, lookup)

    assert looked_up == ["grid"]
    assert result.final_output == "Found it."
    # the second request carries the call and its result
    sent = {item.get("type") for item in seen[1]["input"]}
    assert {"function_call", "function_call_output"} <= sent


async def test_a_completed_event_with_its_output_is_passed_on_as_sent():
    lookup = function_tool(lambda text: "a grid", name_override="lookup")
    model, _ = model_over(
        [item_done(FUNCTION_CALL), completed(FUNCTION_CALL)],
        [item_done(MESSAGE), completed(MESSAGE)],
    )
    assert (await run_streamed(model, lookup)).final_output == "Found it."

    event = ResponseCompletedEvent.model_validate(completed(MESSAGE))
    assert with_streamed_output(event, {0: object()}) is event


def test_streamed_items_keep_their_output_order():
    event = ResponseCompletedEvent.model_validate(completed())
    filled = with_streamed_output(event, {1: "second", 0: "first"})
    assert filled.response.output == ["first", "second"]
    assert event.response.output == []  # the original event is not changed
