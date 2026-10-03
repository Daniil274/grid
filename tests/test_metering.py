import json

import httpx
import pytest

from core.metering import MeteredCall, MeteredTransport, extract_usage


def _client(handler, calls):
    inner = httpx.MockTransport(handler)
    return httpx.AsyncClient(
        transport=MeteredTransport(inner, "openrouter", calls.append), base_url="https://p.test/v1"
    )


def test_extract_takes_the_last_real_usage_object():
    body = b'{"usage": null, "text": "\\"usage\\": {\\"x\\": 1}", "usage": {"prompt_tokens": 3, "cost": 0.5}, "z": 1}'
    assert extract_usage(body) == {"prompt_tokens": 3, "cost": 0.5}
    assert extract_usage(b'data: {"usage": null}\n\n') is None
    assert extract_usage(b"x", b'{"usage": {"a": 1}}') == {"a": 1}


@pytest.mark.asyncio
async def test_json_response_is_passed_on_and_reported():
    payload = {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7, "cost": 0.0123}}
    calls: list[MeteredCall] = []

    async with _client(lambda request: httpx.Response(200, json=payload), calls) as client:
        response = await client.post("/chat/completions", json={"model": "m-1", "messages": []})
    assert response.json() == payload
    assert calls == [MeteredCall("openrouter", "m-1", payload["usage"])]


@pytest.mark.asyncio
async def test_sse_stream_is_forwarded_untouched_and_usage_read_from_its_end():
    events = [
        'data: {"choices": [{"delta": {"content": "hi"}}], "usage": null}\n\n',
        'data: {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2, "cost": 0.001}}\n\n',
        "data: [DONE]\n\n",
    ]
    calls: list[MeteredCall] = []

    async def body():
        for event in events:
            yield event.encode()

    def handler(request):
        return httpx.Response(200, stream=httpx.ByteStream(b"".join(e.encode() for e in events)))

    async with _client(handler, calls) as client:
        async with client.stream("POST", "/responses", json={"model": "m"}) as response:
            text = "".join([part async for part in response.aiter_text()])
    assert text == "".join(events)
    assert calls[0].usage["cost"] == 0.001 and calls[0].model == "m"


@pytest.mark.asyncio
async def test_failures_other_paths_and_missing_usage_are_not_reported():
    calls: list[MeteredCall] = []
    async with _client(lambda request: httpx.Response(429, json={"usage": {"a": 1}}), calls) as client:
        await client.post("/chat/completions", json={"model": "m"})
    async with _client(lambda request: httpx.Response(200, json={"usage": {"a": 1}}), calls) as client:
        await client.get("/models")
        await client.post("/files", json={})
    async with _client(lambda request: httpx.Response(200, json={"ok": True}), calls) as client:
        await client.post("/chat/completions", json={"model": "m"})
    assert calls == []


@pytest.mark.asyncio
async def test_a_failing_sink_never_breaks_the_call():
    def boom(call):
        raise RuntimeError("sink down")

    inner = httpx.MockTransport(lambda request: httpx.Response(200, json={"usage": {"prompt_tokens": 1}}))
    async with httpx.AsyncClient(transport=MeteredTransport(inner, "p", boom), base_url="https://p.test") as client:
        response = await client.post("/chat/completions", json={"model": "m"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_usage_far_from_the_end_of_a_large_body_is_found_at_its_start():
    payload = json.dumps({"usage": {"prompt_tokens": 2}, "pad": "x" * 300_000}).encode()
    calls: list[MeteredCall] = []
    async with _client(lambda request: httpx.Response(200, content=payload), calls) as client:
        await client.post("/chat/completions", json={"model": "m"})
    assert calls[0].usage == {"prompt_tokens": 2}
