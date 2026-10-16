"""Plain-function tools run in a worker thread, off the server's event loop."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from agents.tool import FunctionTool

from core.managers.project_tools_loader import ProjectToolsLoader
from core.tool_threads import OFF_LOOP, function_tool
from tools import AVAILABLE_TOOLS

ROOT = Path(__file__).resolve().parent.parent


async def test_a_slow_plain_tool_leaves_the_event_loop_free():
    @function_tool
    def slow(seconds: float) -> str:
        """Wait, then answer."""
        time.sleep(seconds)
        return "done"

    ticks = 0

    async def other_users():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(other_users())
    try:
        assert await slow.on_invoke_tool(None, json.dumps({"seconds": 0.3})) == "done"
    finally:
        ticker.cancel()

    assert ticks >= 10
    assert slow.params_json_schema["properties"]["seconds"]["type"] == "number"
    assert slow.description == "Wait, then answer."


def _shipped_tools():
    yield from AVAILABLE_TOOLS.items()
    for tools_dir in sorted(ROOT.glob("examples/*/tools")):
        loader = ProjectToolsLoader(str(tools_dir.parent), "tools")
        for name, tool in loader.load_project_tools().items():
            yield f"{tools_dir.parent.name}:{name}", tool


@pytest.mark.parametrize("name,tool", list(_shipped_tools()), ids=lambda value: value if isinstance(value, str) else "")
def test_every_shipped_tool_runs_off_the_event_loop(name, tool):
    if isinstance(tool, FunctionTool):
        assert getattr(tool, OFF_LOOP, False), f"{name} was made before core.tool_threads.install()"


async def test_one_users_tools_do_not_queue_another_users(monkeypatch):
    from types import SimpleNamespace

    from core import tool_threads
    from utils.path_utils import reset_current_factory, set_current_factory

    monkeypatch.setattr(tool_threads, "SLOTS_PER_USER", 2)
    release = __import__("threading").Event()
    running = {"busy": 0, "most": 0}

    @function_tool
    def hold() -> str:
        """Hold a thread until released."""
        running["busy"] += 1
        running["most"] = max(running["most"], running["busy"])
        release.wait(5)
        running["busy"] -= 1
        return "held"

    @function_tool
    def quick() -> str:
        """Answer at once."""
        return "quick"

    def as_user(container: str):
        set_current_factory(SimpleNamespace(container_id=container))

    async def call(tool, container):
        as_user(container)
        try:
            return await tool.on_invoke_tool(None, "{}")
        finally:
            reset_current_factory()

    busy = [asyncio.create_task(call(hold, "busy-user")) for _ in range(5)]
    await asyncio.sleep(0.2)
    # The busy user holds only its two slots; the other user gets a thread at once.
    assert running["most"] == 2
    assert await asyncio.wait_for(call(quick, "other-user"), 2) == "quick"
    release.set()
    assert await asyncio.gather(*busy) == ["held"] * 5
    assert running["most"] == 2


async def test_tools_do_not_take_the_servers_own_threads():
    from core import tool_threads

    @function_tool
    def where() -> str:
        """Name the thread."""
        return __import__("threading").current_thread().name

    assert (await where.on_invoke_tool(None, "{}")).startswith("grid-tool")
    assert tool_threads.THREADS >= tool_threads.SLOTS_PER_USER


async def test_a_cancelled_caller_holds_the_slot_until_the_worker_finishes(monkeypatch):
    """The thread cannot be stopped: cancel the await, not the capacity it uses."""
    from types import SimpleNamespace

    from core import tool_threads
    from utils.path_utils import reset_current_factory, set_current_factory

    monkeypatch.setattr(tool_threads, "SLOTS_PER_USER", 1)
    started = threading.Event()
    release = threading.Event()
    active = {"now": 0, "most": 0}

    def slow() -> str:
        started.set()
        active["now"] += 1
        active["most"] = max(active["most"], active["now"])
        release.wait(5)
        active["now"] -= 1
        return "slow"

    def quick() -> str:
        active["now"] += 1
        active["most"] = max(active["most"], active["now"])
        active["now"] -= 1
        return "quick"

    set_current_factory(SimpleNamespace(container_id="one-user"))
    try:
        cancelled = asyncio.create_task(tool_threads.run_in_tool_thread(slow))
        assert await asyncio.to_thread(started.wait, 5)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        # The worker still runs; the one slot must still be taken.
        second = asyncio.create_task(tool_threads.run_in_tool_thread(quick))
        await asyncio.sleep(0.1)
        assert not second.done(), "the slot was released while its worker still ran"

        release.set()
        assert await asyncio.wait_for(second, 5) == "quick"
        assert active["most"] == 1
    finally:
        release.set()
        reset_current_factory()
