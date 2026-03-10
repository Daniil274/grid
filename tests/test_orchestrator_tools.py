import pytest
from unittest.mock import AsyncMock, Mock

from tools.orchestrator_tools import ORCHESTRATOR_TOOLS


@pytest.mark.asyncio
async def test_orchestrate_requires_factory_in_context():
    ctx = Mock()
    ctx.context = None
    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(ctx, input='{"task": "do something"}')
    assert "нет доступа к AgentFactory" in out


@pytest.mark.asyncio
async def test_orchestrate_happy_path_with_mocks():
    # Build a fake AgentFactory with the minimal surface used by the tool
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-123")
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
    factory.get_active_context_id = Mock(return_value="ctx-123")
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




