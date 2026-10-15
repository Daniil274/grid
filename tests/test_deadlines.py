"""Run deadlines stand still while a call waits for the user's approval.

The case behind it: an orchestrated executor asked for permission to run a
command, nobody had answered yet, and "Agent executor-d26721 stopped after
300 s" - then its retry asked again and died the same way.
"""
import asyncio
import json

import pytest

from core.deadlines import Deadline, waiting_for_person
from tests.test_policy_filters import answered_by, gate_with, invoke
from utils.tool_effects import write


async def test_the_time_a_person_takes_does_not_count():
    async with Deadline(0.2):
        with waiting_for_person():
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.1)  # the 0.2 s it had left are still there


async def test_without_a_person_the_deadline_still_fires():
    with pytest.raises(TimeoutError):
        async with Deadline(0.1):
            await asyncio.sleep(0.5)
    with pytest.raises(TimeoutError):
        async with Deadline(0.2):
            with waiting_for_person():
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)  # the clock runs again after the answer


async def test_every_enclosing_deadline_pauses_also_from_a_child_task():
    async def sub_agent():
        async with Deadline(0.15):
            with waiting_for_person():
                await asyncio.sleep(0.4)
            return "done"

    async with Deadline(0.2):  # the turn
        assert await asyncio.create_task(sub_agent()) == "done"


async def test_parallel_waits_keep_the_turn_paused_until_the_last_answer():
    async def waits(seconds):
        with waiting_for_person():
            await asyncio.sleep(seconds)

    async with Deadline(0.15):
        await asyncio.gather(asyncio.create_task(waits(0.2)), asyncio.create_task(waits(0.4)))


async def test_no_limit_is_no_limit():
    async with Deadline(0) as deadline:
        with waiting_for_person():
            await asyncio.sleep(0.01)
    assert not deadline.expired()


async def test_a_sub_agent_waiting_for_approval_outlives_its_timeout():
    gate, _, state, ctx = gate_with()
    state.interactive, state.context_id = True, "ctx-1"

    async def sub_agent():
        # The agent's own timeout is shorter than the user takes to answer.
        async with Deadline(0.3):
            return await invoke(gate, ctx, write("filepath"), {"filepath": ".env"})

    run = asyncio.create_task(sub_agent())
    await asyncio.sleep(0.6)
    assert not run.done()
    assert await answered_by(gate, state, approve=True, context_id="ctx-1")
    assert (await asyncio.wait_for(run, 2)).startswith("ran:")


async def test_an_unanswered_request_still_expires_on_its_own_ttl():
    gate, _, state, ctx = gate_with(review_ttl_seconds=10)
    gate.config = gate.config.model_copy(update={"review_ttl_seconds": 0.2})
    state.interactive, state.context_id = True, "ctx-1"
    async with Deadline(5):
        blocked = json.loads(await invoke(gate, ctx, write("filepath"), {"filepath": ".env"}))
    assert blocked["rule"] == "review_timeout"
