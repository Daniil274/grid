"""Limits on one model request that the provider itself does not enforce.

A streamed answer is never cut by the client's own timeout while the provider
keeps sending: that timeout is the longest pause between two chunks, not the
length of the answer. And a ChatGPT plan takes no output cap at all. A model
that runs away in one answer - a tool call's arguments growing without end,
say - then holds its run for as long as it likes, and nothing of it shows,
since tool-call arguments are not rendered while they stream.

``BoundedModel`` puts both limits on the client side: the whole request,
first byte to last event, within ``timeout`` seconds, and the generated
output within ``max_output_tokens``, estimated from the streamed deltas.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

from agents.models.interface import Model

logger = logging.getLogger("grid.models.bounded")

#: Stream events whose ``delta`` is output the model generates: the answer,
#: a tool call's arguments and reasoning made visible.
GENERATED_DELTA_EVENTS = frozenset(
    {
        "response.output_text.delta",
        "response.refusal.delta",
        "response.function_call_arguments.delta",
        "response.reasoning_text.delta",
        "response.reasoning_summary_text.delta",
    }
)

#: Characters per token of the output estimate. Code and English run near
#: four, other languages lower, so the estimate counts fewer tokens than the
#: model spent and a request is cut a little past its cap, never before it.
CHARS_PER_TOKEN = 4


class ModelRequestLimit(RuntimeError):
    """A model request was abandoned for passing a limit of its own; not transient."""


class BoundedModel(Model):
    """*inner* with a wall-clock limit and an output cap on each request."""

    def __init__(
        self,
        inner: Model,
        *,
        key: str,
        timeout: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
    ) -> None:
        self.inner = inner
        self.key = key
        self.timeout = timeout if timeout and timeout > 0 else None
        self.max_output_tokens = max_output_tokens if max_output_tokens and max_output_tokens > 0 else None

    def _too_slow(self) -> ModelRequestLimit:
        logger.warning("Model '%s' did not finish a request within %s s; abandoned", self.key, self.timeout)
        return ModelRequestLimit(
            f"model '{self.key}' did not finish its answer within {self.timeout:g} s (response_timeout)"
        )

    def _too_long(self, chars: int) -> ModelRequestLimit:
        logger.warning(
            "Model '%s' generated ~%d tokens in one answer, past its max_tokens %d; abandoned",
            self.key, chars // CHARS_PER_TOKEN, self.max_output_tokens,
        )
        return ModelRequestLimit(
            f"model '{self.key}' generated more than its max_tokens ({self.max_output_tokens}) "
            "in one answer"
        )

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        if self.timeout is None:
            return await self.inner.get_response(*args, **kwargs)
        try:
            async with asyncio.timeout(self.timeout) as deadline:
                return await self.inner.get_response(*args, **kwargs)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise self._too_slow() from None

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        if self.timeout is None and self.max_output_tokens is None:
            async for event in self.inner.stream_response(*args, **kwargs):
                yield event
            return
        loop = asyncio.get_running_loop()
        ends_at = loop.time() + self.timeout if self.timeout is not None else None
        chars = 0
        stream = self.inner.stream_response(*args, **kwargs)
        try:
            while True:
                # The wait for each event, not the caller's work between them,
                # is what the deadline bounds: a timeout cannot span a yield.
                try:
                    if ends_at is None:
                        event = await anext(stream)
                    else:
                        remaining = ends_at - loop.time()
                        if remaining <= 0:
                            raise self._too_slow()
                        # The timer says whether it fired: the clock alone can
                        # read just short of the deadline when it does (Windows
                        # ticks are ~15 ms, and asyncio fires within one).
                        deadline = asyncio.timeout(remaining)
                        try:
                            async with deadline:
                                event = await anext(stream)
                        except TimeoutError:
                            if not deadline.expired():
                                raise  # the provider's own timeout, not this limit
                            raise self._too_slow() from None
                except StopAsyncIteration:
                    return
                if self.max_output_tokens is not None and getattr(event, "type", None) in GENERATED_DELTA_EVENTS:
                    chars += len(getattr(event, "delta", "") or "")
                    if chars > self.max_output_tokens * CHARS_PER_TOKEN:
                        raise self._too_long(chars)
                yield event
        finally:
            await stream.aclose()
