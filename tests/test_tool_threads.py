"""Plain-function tools run in a worker thread, off the server's event loop."""

import asyncio
import json
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
