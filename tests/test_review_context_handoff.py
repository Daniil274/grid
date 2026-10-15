"""Actual wrapped sub-agent sessions must match execution metadata, without footer parsing."""
from types import SimpleNamespace

import pytest

from tests.test_tool_error_recovery import sub_agent_tool, tool_ctx, _no_events
from core.factory.tools import _agent_tool_context_id


@pytest.mark.asyncio
async def test_new_context_handoff_is_isolated_and_resumable(tmp_path, monkeypatch):
    sessions = []

    def run_streamed(**kwargs):
        sessions.append(kwargs["session"].session_id)
        return SimpleNamespace(stream_events=lambda: _no_events(), final_output="report", new_items=[])

    factory, tool = sub_agent_tool(tmp_path, monkeypatch, run_streamed)
    executions = []
    monkeypatch.setattr(factory.context_manager, "add_execution", lambda execution, **_: executions.append(execution))
    # Simulate footer-free pending contract even on the clean HEAD.
    original = tool.on_invoke_tool

    async def no_footer(ctx, args):
        result = await original(ctx, args)
        return result.split("\n\nContext ID:", 1)[0]

    tool.on_invoke_tool = no_footer
    wrapped = factory._wrap_agent_tool(tool, "test_agent")
    await wrapped.on_invoke_tool(tool_ctx("alice"), '{"input":"Start"}')
    first_id = executions[-1].context_id
    assert first_id in sessions[-1]
    assert "alice" in sessions[-1]
    await wrapped.on_invoke_tool(tool_ctx("alice"), '{"input":"Continue ' + first_id + '"}')
    assert sessions[-1] == sessions[0]
    await wrapped.on_invoke_tool(tool_ctx("bob"), '{"input":"Continue ' + first_id + '"}')
    assert sessions[-1] != sessions[0]
    assert _agent_tool_context_id.get() is None


@pytest.mark.asyncio
async def test_handoff_reset_on_error(tmp_path, monkeypatch):
    factory, tool = sub_agent_tool(tmp_path, monkeypatch, lambda **kwargs: None)

    async def fail(ctx, args):
        assert _agent_tool_context_id.get() is not None
        raise RuntimeError("failed")

    tool.on_invoke_tool = fail
    wrapped = factory._wrap_agent_tool(tool, "test_agent")
    with pytest.raises(RuntimeError, match="failed"):
        await wrapped.on_invoke_tool(tool_ctx(), '{"input":"Start"}')
    assert _agent_tool_context_id.get() is None
