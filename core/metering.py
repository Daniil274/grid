"""Seeing what every model call used, wherever it goes.

The Agents SDK reduces a response's ``usage`` to token counts and drops the rest,
among them the ``cost`` OpenRouter reports; compaction and other direct calls
never reach the SDK's accounting at all. The one place all of them pass is the
HTTP client, so :class:`MeteredTransport` wraps its transport and reads ``usage``
off the bytes that go by - a whole JSON body or the last events of an SSE stream
- without altering them or holding the response back.

A call that fails, or whose body carries no usage (a stream cut short), is not
reported: there is nothing known to bill.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Mapping, Optional

import httpx

logger = logging.getLogger(__name__)

#: Paths whose responses carry ``usage``.
METERED_SUFFIXES = ("/chat/completions", "/responses", "/completions", "/embeddings")
#: Bytes kept from the start and the end of a body; ``usage`` sits in one of them.
HEAD_BYTES = 16 * 1024
TAIL_BYTES = 64 * 1024

_USAGE_OBJECT = re.compile(rb'"usage"\s*:\s*\{')


@dataclass(frozen=True)
class MeteredCall:
    """One model call that finished, as the provider reported it."""

    provider: str
    model: str
    usage: Mapping[str, Any]
    #: Who paid for the call: the credential's ``source``, and whether it was the operator.
    source: str = "env"
    charged: bool = True
    subscription: bool = False


def extract_usage(*chunks: bytes) -> Optional[dict]:
    """The last ``"usage": {...}`` object in the first of *chunks* that has one.

    A quoted ``"usage"`` inside generated text is escaped in JSON, so only real
    keys match. Earlier SSE events carry ``"usage": null``, which does not match.
    """
    decoder = json.JSONDecoder()
    for chunk in chunks:
        matches = list(_USAGE_OBJECT.finditer(chunk))
        for match in reversed(matches):
            try:
                value, _ = decoder.raw_decode(chunk.decode("utf-8", errors="ignore"), _char_offset(chunk, match.end() - 1))
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _char_offset(data: bytes, byte_offset: int) -> int:
    """The index in ``data.decode(errors="ignore")`` of the byte at *byte_offset*."""
    return len(data[:byte_offset].decode("utf-8", errors="ignore"))


class _Tee(httpx.AsyncByteStream):
    """The response body, passed through while its ends are remembered."""

    def __init__(self, inner: httpx.AsyncByteStream, finish: Callable[[bytes, bytes], None]) -> None:
        self._inner = inner
        self._finish = finish
        self._head = bytearray()
        self._tail = bytearray()
        self._done = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            if len(self._head) < HEAD_BYTES:
                self._head += chunk[: HEAD_BYTES - len(self._head)]
            self._tail += chunk
            if len(self._tail) > 2 * TAIL_BYTES:
                del self._tail[:-TAIL_BYTES]
            yield chunk
        self._report()

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._report()

    def _report(self) -> None:
        if self._done:
            return
        self._done = True
        self._finish(bytes(self._head), bytes(self._tail[-TAIL_BYTES:]))


class MeteredTransport(httpx.AsyncBaseTransport):
    """Reports the usage of every model call that passes through *inner*."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, provider: str, on_call: Callable[[MeteredCall], None]
    ) -> None:
        self._inner = inner
        self._provider = provider
        self._on_call = on_call

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.method != "POST" or not request.url.path.endswith(METERED_SUFFIXES) or response.status_code >= 400:
            return response
        model = _requested_model(request)
        credential = request.extensions.get("grid_credential")
        paid = {
            "source": getattr(credential, "source", "env"),
            "charged": getattr(credential, "charged", True),
            "subscription": getattr(credential, "subscription", False),
        }

        def finish(head: bytes, tail: bytes) -> None:
            usage = extract_usage(tail, head)
            if usage is None:
                return
            try:
                self._on_call(MeteredCall(self._provider, model, usage, **paid))
            except Exception:  # noqa: BLE001 - accounting must never break a call
                logger.exception("Recording the usage of a model call failed")

        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_Tee(response.stream, finish),  # type: ignore[arg-type]
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def _requested_model(request: httpx.Request) -> str:
    try:
        body = json.loads(request.content)
    except (ValueError, httpx.RequestNotRead):
        return ""
    model = body.get("model") if isinstance(body, dict) else None
    return model if isinstance(model, str) else ""
