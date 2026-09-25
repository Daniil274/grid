"""The web runtime owns its background work: it is replaced, logged and cancelled."""

import asyncio
from unittest.mock import AsyncMock

from web_chat.runtime import WebChatRuntime


def bare_runtime():
    runtime = object.__new__(WebChatRuntime)
    runtime._background = set()
    runtime._warmup = None
    runtime.registry = AsyncMock()
    return runtime


async def test_a_new_warmup_replaces_the_running_one_and_close_cancels_it():
    runtime = bare_runtime()
    started = asyncio.Event()

    async def warm_forever():
        started.set()
        await asyncio.Event().wait()

    runtime.warm_default_agent = warm_forever
    runtime.schedule_warmup()
    first = runtime._warmup
    await started.wait()
    runtime.schedule_warmup()
    await asyncio.sleep(0)

    assert first.cancelled()
    await runtime.close()
    assert runtime._warmup.cancelled()
    assert not runtime._background
    runtime.registry.close.assert_awaited_once()


async def test_a_failed_warmup_is_logged_not_lost(caplog):
    runtime = bare_runtime()

    async def fail():
        raise RuntimeError("model unavailable")

    runtime.warm_default_agent = fail
    runtime.schedule_warmup()
    await asyncio.gather(runtime._warmup, return_exceptions=True)
    await asyncio.sleep(0)

    assert "warm-default-agent failed" in caplog.text
    assert not runtime._background
