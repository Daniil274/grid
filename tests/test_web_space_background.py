"""A space owns its background work: it is replaced, logged and cancelled."""

import asyncio
from unittest.mock import AsyncMock

from web_chat.space import UserSpace
from web_chat.turns import TurnBoard


def bare_space():
    space = object.__new__(UserSpace)
    space._background = set()
    space._warmup = None
    space.registry = AsyncMock()
    space.turns = TurnBoard()
    return space


async def test_a_new_warmup_replaces_the_running_one_and_close_cancels_it():
    space = bare_space()
    started = asyncio.Event()

    async def warm_forever():
        started.set()
        await asyncio.Event().wait()

    space.warm_default_agent = warm_forever
    space.schedule_warmup()
    first = space._warmup
    await started.wait()
    space.schedule_warmup()
    await asyncio.sleep(0)

    assert first.cancelled()
    await space.close()
    assert space._warmup.cancelled()
    assert not space._background
    space.registry.close.assert_awaited_once()


async def test_a_failed_warmup_is_logged_not_lost(caplog):
    space = bare_space()

    async def fail():
        raise RuntimeError("model unavailable")

    space.warm_default_agent = fail
    space.schedule_warmup()
    await asyncio.gather(space._warmup, return_exceptions=True)
    await asyncio.sleep(0)

    assert "warm-default-agent failed" in caplog.text
    assert not space._background
