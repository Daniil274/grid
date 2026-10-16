"""Tools the host runs before the model is asked are visible in the web trace, failures included."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent_factory import AgentFactory
from web_chat.observer import WebStreamObserver
from web_chat.trace import TraceRecorder

pytestmark = pytest.mark.asyncio


def make_factory(tool_results):
    factory = object.__new__(AgentFactory)
    factory.action_gate = None
    factory._stream_observer = None  # the factory's own observer must not be needed
    tools = []
    for name, result in tool_results.items():
        invoke = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
        tools.append(SimpleNamespace(name=name, on_invoke_tool=invoke))
    return factory, tools


def make_observer():
    recorder = TraceRecorder(lambda _: None)
    return recorder, WebStreamObserver(recorder, emit_token=lambda _: None)


def run_context(observer):
    return SimpleNamespace(stream_observer=observer)


async def test_auto_run_rows_show_success_and_failure_in_the_turn_observer():
    recorder, observer = make_observer()
    factory, tools = make_factory({
        "init_codegraph": "❌ CodeGraph is not available (exit 1): boom",
        "beads_init": "✅ Beads initialized.",
    })
    config = SimpleNamespace(auto_run_tools=[{"name": "init_codegraph"}, {"name": "beads_init"}])
    info, complete = await factory._execute_auto_run_tools("a", config, tools, ".", run_context(observer))
    rows = {row["tool"]: row for row in recorder.snapshot()}
    assert rows["init_codegraph"]["status"] == "error"
    assert "not available" in rows["init_codegraph"]["body"]
    assert rows["beads_init"]["status"] == "done"
    assert "Beads initialized" in info and complete


async def test_auto_run_exception_and_missing_tool_are_shown():
    recorder, observer = make_observer()
    factory, tools = make_factory({"beads_init": RuntimeError("no bd")})
    config = SimpleNamespace(auto_run_tools=[{"name": "beads_init"}, {"name": "ghost"}])
    await factory._execute_auto_run_tools("a", config, tools, ".", run_context(observer))
    rows = recorder.snapshot()
    assert [row["status"] for row in rows] == ["error", "error"]
    assert "RuntimeError: no bd" in rows[0]["body"]
    assert "ghost" in rows[1]["body"]


async def test_policy_block_of_auto_run_tool_is_an_error_row_and_counted():
    recorder, observer = make_observer()
    blocked = json.dumps({"status": "blocked", "rule": "run_stopped"})
    factory, tools = make_factory({"init_codegraph": blocked})
    config = SimpleNamespace(auto_run_tools=[{"name": "init_codegraph"}])
    info, complete = await factory._execute_auto_run_tools("a", config, tools, ".", run_context(observer))
    assert info == "" and not complete
    assert recorder.snapshot()[0]["status"] == "error"
    assert observer.policy_blocks == 1


async def test_init_tools_failures_reach_the_observer():
    recorder, observer = make_observer()
    factory, tools = make_factory({"beads_show": RuntimeError("down")})
    factory.config = SimpleNamespace(get_working_directory=lambda: ".")
    await factory._execute_init_tools(
        [{"name": "beads_show", "parameters": {}}, {"name": "nope"}], tools, run_context(observer)
    )
    assert [row["status"] for row in recorder.snapshot()] == ["error", "error"]
