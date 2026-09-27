import json

import pytest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from tools.orchestrator_tools import ORCHESTRATOR_TOOLS


@pytest.mark.asyncio
async def test_orchestrate_requires_factory_in_context():
    ctx = Mock()
    ctx.context = None
    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(ctx, input='{"task": "do something"}')
    assert "no access to AgentFactory" in out


@pytest.mark.asyncio
async def test_orchestrate_happy_path_with_mocks():
    # Build a fake AgentFactory with the minimal surface used by the tool
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.side_effect = Exception("Not found")
    factory.resolve_model_key = Mock(return_value="gpt-4")

    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    # executor run -> returns a draft; reviewer -> approve; judges -> accept majority
    factory.run_agent_object_simple = AsyncMock(
        side_effect=[
            "DRAFT-1",
            '{"decision":"approve","feedback":""}',
            '{"vote":"accept","reason":"ok"}',
            '{"vote":"accept","reason":"ok"}',
            '{"vote":"reject","reason":"minor"}',
        ]
    )

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(ctx, input='{"task": "goal"}')
    assert "DRAFT-1" in out


@pytest.mark.asyncio
async def test_orchestrate_accepts_executor_tools_as_json_string():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.side_effect = Exception("Not found")
    factory.resolve_model_key = Mock(return_value="gpt-4")
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object_simple = AsyncMock(
        side_effect=[
            "DRAFT",
            '{"decision":"approve","feedback":""}',
            '{"vote":"accept","reason":"ok"}',
        ]
    )

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(
        ctx,
        input='{"task": "goal", "executor_tools": ["filesystem","git","terminal"], "model_key": "None"}'
    )
    assert "DRAFT" in out


@pytest.mark.asyncio
async def test_orchestrate_uses_tool_default_model_when_model_key_omitted():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.return_value = SimpleNamespace(
        env_vars={"DEFAULT_MODEL": "worker-model"}
    )
    factory.resolve_model_key = Mock(side_effect=lambda key: key or "coordinator-model")
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object_simple = AsyncMock(return_value="DRAFT")

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(ctx, input='{"task": "goal"}')

    assert "DRAFT" in out
    factory.create_dynamic_agent.assert_awaited_once()
    assert factory.create_dynamic_agent.await_args.kwargs["model_key"] == "worker-model"


@pytest.mark.asyncio
async def test_orchestrate_falls_back_to_tool_default_for_unknown_model_key():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.return_value = SimpleNamespace(
        env_vars={"DEFAULT_MODEL": "worker-model"}
    )

    def get_model(key):
        if key == "worker-model":
            return SimpleNamespace()
        raise Exception("unknown model")

    factory.config.get_model.side_effect = get_model
    factory.config.get_agent.side_effect = Exception("unknown agent")
    factory.resolve_model_key = Mock(side_effect=lambda key: key or "coordinator-model")
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object_simple = AsyncMock(return_value="DRAFT")

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(
        ctx,
        input='{"task": "goal", "model_key": "stale-model"}',
    )

    assert "DRAFT" in out
    factory.create_dynamic_agent.assert_awaited_once()
    assert factory.create_dynamic_agent.await_args.kwargs["model_key"] == "worker-model"


@pytest.mark.asyncio
async def test_orchestrate_sanitizes_noisy_context_id():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value=None)
    factory.context_manager = Mock()
    factory.context_manager.start_new_context = Mock(return_value="ctx-newcontext")
    factory.config = Mock()
    factory.config.get_tool.side_effect = Exception("Not found")
    factory.resolve_model_key = Mock(return_value="gpt-4")
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object_simple = AsyncMock(return_value="DRAFT")

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123", context_id=None)

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    noisy_id = "Context ID: ctx-abcdef12 (reuse from previous step)"
    out = await tool.on_invoke_tool(
        ctx,
        input=f'{{"task": "goal", "context_id": {json.dumps(noisy_id)}}}',
    )

    assert "DRAFT" in out
    assert '"context_id": "ctx-abcdef12"' in out
    run_kwargs = factory.run_agent_object_simple.await_args.kwargs
    assert run_kwargs["context_id"] == "ctx-abcdef12"
    factory.context_manager.start_new_context.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrate_ignores_invalid_context_id_and_reuses_active():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-11223344")
    factory.config = Mock()
    factory.config.get_tool.side_effect = Exception("Not found")
    factory.resolve_model_key = Mock(return_value="gpt-4")
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object_simple = AsyncMock(return_value="DRAFT")

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123", context_id=None)

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(
        ctx,
        input='{"task": "goal", "context_id": "reuse parent session"}',
    )

    assert "DRAFT" in out
    assert '"context_id": "ctx-11223344"' in out
    run_kwargs = factory.run_agent_object_simple.await_args.kwargs
    assert run_kwargs["context_id"] == "ctx-11223344"






@pytest.mark.asyncio
async def test_orchestrate_executor_reports_into_the_callers_trace_under_its_task():
    from core.action_policy import ActionRunState
    from web_chat.observer import WebStreamObserver
    from web_chat.trace import TraceRecorder

    events = []
    recorder = TraceRecorder(events.append)
    caller = WebStreamObserver(recorder, emit_token=lambda _text: None)

    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.side_effect = Exception("Not found")
    factory.resolve_model_key = Mock(return_value="gpt-4")
    fake_agent = Mock()
    fake_agent.name = "executor-abc123"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object_simple = AsyncMock(return_value="DRAFT")

    ctx = Mock()
    ctx.context = SimpleNamespace(
        factory=factory, user_id="user-1", context_id="ctx-12345678",
        pipeline_id=None, stream_observer=caller,
        action_state=ActionRunState(task="Study the current changes"), action_depth=0,
    )
    ctx.tool_call_id = "call-7"

    out = await ORCHESTRATOR_TOOLS["orchestrate"].on_invoke_tool(ctx, input='{"task": "goal"}')

    assert "DRAFT" in out
    observer = factory.run_agent_object_simple.await_args.kwargs["stream_observer"]
    (block,) = recorder.snapshot()
    assert block["kind"] == "agent"
    assert observer._parent_id == block["id"]
    assert block["status"] == "done"  # settled even before the caller sees the output

    # The executor acts under the user's task; the text the coordinator wrote
    # for it is its purpose, not its authority.
    kwargs = factory.run_agent_object_simple.await_args.kwargs
    assert kwargs["action_state"].parent is ctx.context.action_state
    assert kwargs["action_state"].task == "Study the current changes"
    assert kwargs["action_state"].delegation == {"tool": "orchestrate", "request": "goal"}
    assert kwargs["action_depth"] == 1
    # Its init_tools are judged under the same state, not refused as taskless.
    created = factory.create_dynamic_agent.await_args.kwargs
    assert created["action_state"] is kwargs["action_state"]
