import json

import pytest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from core.run_stream import RunOutcome
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
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT-1"))

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
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(
        ctx,
        input='{"task": "goal", "executor_tools": ["filesystem","git","terminal"]}'
    )
    assert "DRAFT" in out


@pytest.mark.asyncio
async def test_orchestrate_runs_the_executor_on_the_tools_first_model():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.return_value = SimpleNamespace(
        models=["worker-model"]
    )
    factory.resolve_model_key = Mock(side_effect=lambda key: key or "coordinator-model")
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(ctx, input='{"task": "goal"}')

    assert "DRAFT" in out
    factory.create_dynamic_agent.assert_awaited_once()
    assert factory.create_dynamic_agent.await_args.kwargs["model_key"] == "worker-model"


def test_orchestrate_does_not_let_the_caller_pick_the_model():
    schema = ORCHESTRATOR_TOOLS["orchestrate"].params_json_schema
    assert "model_key" not in schema["properties"]
    assert schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_orchestrate_ignores_a_model_key_from_the_caller():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.return_value = SimpleNamespace(
        models=["worker-model"]
    )
    factory.resolve_model_key = Mock(side_effect=lambda key: key or "coordinator-model")
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(
        ctx,
        input='{"task": "goal", "model_key": "expensive-model"}',
    )

    factory.resolve_model_key.assert_called_once_with("worker-model")
    assert factory.create_dynamic_agent.await_args.kwargs["model_key"] == "worker-model"
    assert '"model_key": "worker-model"' in out


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
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

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
    run_kwargs = factory.run_agent_object.await_args.kwargs
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
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123", context_id=None)

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = await tool.on_invoke_tool(
        ctx,
        input='{"task": "goal", "context_id": "reuse parent session"}',
    )

    assert "DRAFT" in out
    assert '"context_id": "ctx-11223344"' in out
    run_kwargs = factory.run_agent_object.await_args.kwargs
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
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

    ctx = Mock()
    ctx.context = SimpleNamespace(
        factory=factory, user_id="user-1", context_id="ctx-12345678",
        pipeline_id=None, stream_observer=caller,
        action_state=ActionRunState(task="Study the current changes"), action_depth=0,
    )
    ctx.tool_call_id = "call-7"

    out = await ORCHESTRATOR_TOOLS["orchestrate"].on_invoke_tool(ctx, input='{"task": "goal"}')

    assert "DRAFT" in out
    observer = factory.run_agent_object.await_args.kwargs["stream_observer"]
    (block,) = recorder.snapshot()
    assert block["kind"] == "agent"
    assert observer._parent_id == block["id"]
    assert block["status"] == "done"  # settled even before the caller sees the output

    # The executor acts under the user's task; the text the coordinator wrote
    # for it is its purpose, not its authority.
    kwargs = factory.run_agent_object.await_args.kwargs
    assert kwargs["action_state"].parent is ctx.context.action_state
    assert kwargs["action_state"].task == "Study the current changes"
    assert kwargs["action_state"].delegation == {"tool": "orchestrate", "request": "goal"}
    assert kwargs["action_depth"] == 1
    # Its init_tools are judged under the same state, not refused as taskless.
    created = factory.create_dynamic_agent.await_args.kwargs
    assert created["action_state"] is kwargs["action_state"]


@pytest.mark.asyncio
async def test_orchestrate_reports_tools_the_executor_did_not_get():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.side_effect = Exception("Not found")
    factory.resolve_model_key = Mock(return_value="gpt-4")
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    fake_agent._grid_missing_tools = ["codegraf_explore"]
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123", run_control="stop-handle")

    tool = ORCHESTRATOR_TOOLS["orchestrate"]
    out = json.loads(
        await tool.on_invoke_tool(
            ctx,
            input='{"task": "goal", "executor_tools": ["file_read", "codegraf_explore"]}',
        )
    )

    assert out["executor_tools"] == ["file_read"]
    assert out["missing_tools"] == ["codegraf_explore"]
    assert "task" not in out
    # The executor acts for the same user and is reached by the user's Stop.
    kwargs = factory.run_agent_object.await_args.kwargs
    assert kwargs["user_id"] == "user-123"
    assert kwargs["run_control"] == "stop-handle"


@pytest.mark.asyncio
async def test_orchestrate_does_not_give_the_executor_the_callers_models():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.side_effect = Exception("Not found")
    factory.config.get_agent.return_value = Mock(model_keys=Mock(return_value=["gpt-6.1-sol", "glm-latest"]))
    factory.resolve_model_key = Mock(return_value="glm-flash")
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123", agent_id="coordinator")

    await ORCHESTRATOR_TOOLS["orchestrate"].on_invoke_tool(ctx, input='{"task": "goal"}')

    assert factory.create_dynamic_agent.await_args.kwargs["fallback_model_keys"] == []


@pytest.mark.asyncio
async def test_orchestrate_falls_back_only_to_the_tools_other_models():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.return_value = SimpleNamespace(
        models=["worker-model", "spare-a", "spare-b"]
    )
    factory.resolve_model_key = Mock(side_effect=lambda key: key)
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123", agent_id="coordinator")

    await ORCHESTRATOR_TOOLS["orchestrate"].on_invoke_tool(ctx, input='{"task": "goal"}')

    assert factory.create_dynamic_agent.await_args.kwargs["fallback_model_keys"] == ["spare-a", "spare-b"]


@pytest.mark.asyncio
async def test_orchestrate_reports_the_model_the_executor_runs_on():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.return_value = SimpleNamespace(
        models=["worker-model", "spare"]
    )
    factory.resolve_model_key = Mock(side_effect=lambda key: key)
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    # worker-model had no login, so the factory built the executor on the spare.
    fake_agent._grid_model_key = "spare"
    fake_agent._grid_missing_tools = []
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object = AsyncMock(return_value=RunOutcome("DRAFT"))

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")

    out = json.loads(await ORCHESTRATOR_TOOLS["orchestrate"].on_invoke_tool(ctx, input='{"task": "goal"}'))

    assert out["model_key"] == "spare"


@pytest.mark.asyncio
async def test_orchestrate_refuses_a_model_the_config_does_not_have():
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.return_value = SimpleNamespace(models=["worker-model", "typo-model"])

    def get_model(key):
        if key == "worker-model":
            return SimpleNamespace()
        raise Exception("unknown model")

    factory.config.get_model.side_effect = get_model
    factory.create_dynamic_agent = AsyncMock()

    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")

    out = await ORCHESTRATOR_TOOLS["orchestrate"].on_invoke_tool(ctx, input='{"task": "goal"}')

    assert "typo-model" in out
    factory.create_dynamic_agent.assert_not_awaited()


def test_tool_config_takes_the_orchestrate_models_as_a_list_or_one_key():
    from schemas.schemas import ToolConfig

    assert ToolConfig(type="function", models=["a", "b"]).models == ["a", "b"]
    assert ToolConfig(type="function", models="a").models == "a"
    assert ToolConfig(type="function").models is None


def _tiered_factory(**config):
    factory = Mock()
    factory.get_active_context_id = Mock(return_value="ctx-12345678")
    factory.config = Mock()
    factory.config.get_tool.return_value = SimpleNamespace(
        models=["worker-model"], tiers={"fast": ["cheap-a", "cheap-b"], "strong": ["big"]}
    )
    factory.config.get_agent_timeout = Mock(return_value=config.get("timeout", 900))
    factory.config.get_max_turns = Mock(return_value=config.get("turns", 300))
    factory.resolve_model_key = Mock(side_effect=lambda key: key)
    fake_agent = Mock()
    fake_agent.name = "fake_agent"
    fake_agent._grid_missing_tools = []
    fake_agent._grid_model_key = None
    factory.create_dynamic_agent = AsyncMock(return_value=fake_agent)
    factory.run_agent_object = AsyncMock(
        return_value=RunOutcome("DRAFT", None, model_calls=4, input_tokens=5000, output_tokens=300)
    )
    return factory


async def _orchestrate(factory, **arguments):
    ctx = Mock()
    ctx.context = Mock(factory=factory, user_id="user-123")
    out = await ORCHESTRATOR_TOOLS["orchestrate"].on_invoke_tool(
        ctx, input=json.dumps({"task": "goal", **arguments})
    )
    return out


@pytest.mark.asyncio
async def test_orchestrate_runs_the_executor_on_the_chosen_tiers_models():
    factory = _tiered_factory()
    out = json.loads(await _orchestrate(factory, tier="fast"))

    kwargs = factory.create_dynamic_agent.await_args.kwargs
    assert (kwargs["model_key"], kwargs["fallback_model_keys"]) == ("cheap-a", ["cheap-b"])
    assert out["tier"] == "fast"


@pytest.mark.asyncio
async def test_orchestrate_names_the_tiers_when_one_does_not_exist():
    factory = _tiered_factory()
    out = await _orchestrate(factory, tier="turbo")

    assert "unknown tier 'turbo'; this system's tiers: fast, strong" in out
    factory.create_dynamic_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrate_time_limit_is_never_looser_than_the_systems():
    factory = _tiered_factory(timeout=600, turns=50)
    await _orchestrate(factory, timeout_seconds=1200)
    assert factory.run_agent_object.await_args.kwargs["timeout"] == 600

    await _orchestrate(factory, timeout_seconds=120)
    kwargs = factory.run_agent_object.await_args.kwargs
    assert kwargs["timeout"] == 120
    # Turns are the system's alone: the caller sets none.
    assert "max_turns" not in kwargs

    await _orchestrate(factory)
    assert factory.run_agent_object.await_args.kwargs["timeout"] is None  # the system's own


@pytest.mark.asyncio
async def test_orchestrate_reports_what_the_executor_took():
    factory = _tiered_factory()
    factory.run_agent_object.return_value = RunOutcome(
        "[Agent executor reached its turn limit]", "max_turns", 7, 9000, 400
    )
    out = json.loads(await _orchestrate(factory))

    assert out["status"] == "max_turns"
    assert (out["model_calls"], out["tokens"]) == (7, {"input": 9000, "output": 400})
    assert isinstance(out["seconds"], float)
    assert "cost_usd" not in out  # unmetered: nothing known about cost


@pytest.mark.asyncio
async def test_orchestrate_reports_the_cost_a_metered_client_saw():
    from core import model_access
    from core.pricing import Cost, TokenUsage

    factory = _tiered_factory()

    async def metered_run(*args, **kwargs):
        for paid, subscription in ((12_500, False), (2_500, True)):
            event = model_access.SpendEvent(
                "p", "m", TokenUsage(), Cost(paid, "computed"), "env", True, subscription
            )
            for tally in model_access._tallies.get():
                tally.add(event)
        return RunOutcome("DRAFT")

    factory.run_agent_object = AsyncMock(side_effect=metered_run)
    out = json.loads(await _orchestrate(factory))

    assert out["cost_usd"] == 0.015
    assert out["cost_note"].startswith("1 of 2 calls ran on a subscription")


@pytest.mark.asyncio
async def test_the_executor_is_not_told_any_budget():
    factory = _tiered_factory(timeout=600, turns=50)
    await _orchestrate(factory, timeout_seconds=120)
    instructions = factory.create_dynamic_agent.await_args.kwargs["instructions"] or ""
    assert "budget" not in instructions.lower() and "turn" not in instructions.lower()


@pytest.mark.asyncio
async def test_a_chosen_model_runs_first_then_the_tiers_models():
    factory = _tiered_factory()
    factory.config.get_tool.return_value.model_choice = True
    await _orchestrate(factory, tier="fast", model="big")

    kwargs = factory.create_dynamic_agent.await_args.kwargs
    assert (kwargs["model_key"], kwargs["fallback_model_keys"]) == ("big", ["cheap-a", "cheap-b"])

    await _orchestrate(factory, model="cheap-b")
    kwargs = factory.create_dynamic_agent.await_args.kwargs
    assert (kwargs["model_key"], kwargs["fallback_model_keys"]) == ("cheap-b", ["worker-model"])


@pytest.mark.asyncio
async def test_a_model_is_chosen_only_where_and_among_what_the_system_offers():
    factory = _tiered_factory()
    out = await _orchestrate(factory, model="big")
    assert "offers no choice of model; pick a tier" in out
    factory.create_dynamic_agent.assert_not_awaited()

    factory.config.get_tool.return_value.model_choice = True
    out = await _orchestrate(factory, model="gpt-anything")
    assert "not one of this system's executor models: worker-model, cheap-a, cheap-b, big" in out
    factory.create_dynamic_agent.assert_not_awaited()


def test_the_caller_sees_the_executor_models_only_when_it_may_choose(tmp_path, monkeypatch):
    from core.config.config import Config

    monkeypatch.setenv("TEST_KEY", "k")
    config_path = tmp_path / "config.yaml"
    body = """
settings: {{default_agent: lead}}
providers:
  p: {{name: p, base_url: "https://example.com/v1", api_key_env: TEST_KEY}}
models:
  quick: {{name: quick-1, provider: p, description: "Quick and cheap", context_window: 128000}}
  deep: {{name: deep-1, provider: p, context_window: 400000, capabilities: [vision]}}
tools:
  orchestrate:
    type: function
    models: [quick]
    tiers: {{strong: [deep, quick]}}
    model_choice: {choice}
agents:
  lead: {{name: Lead, model: quick, custom_prompt: Lead., tools: [orchestrate]}}
"""
    config_path.write_text(body.format(choice="true"), encoding="utf-8")
    config = Config(str(config_path))
    sections = {section.key: section.content for section in config.build_agent_prompt_sections("lead")}
    assert sections["executor_models"].strip().splitlines()[1:] == [
        "- without tier or model: quick",
        "- tier strong: deep -> quick",
        "- `quick`: Quick and cheap · context 128k",
        "- `deep`: deep-1 · context 400k · vision",
    ]

    config_path.write_text(body.format(choice="false"), encoding="utf-8")
    sections = {section.key for section in Config(str(config_path)).build_agent_prompt_sections("lead")}
    assert "executor_models" not in sections
