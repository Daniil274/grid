"""Offline tests for the cross-system tools of a user's key agents.

No providers, Docker or MCP are started; the SDK runner is replaced.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from agents import RunContextWrapper

from core.action_policy import ActionRunState
from core.factory.run_context import GridRunContext
from core.system_access import SystemAccessBroker, SystemAccessDenied
from schemas import AgentConfig
from utils.path_utils import factory_path_context, get_current_factory


class FakeConfig:
    def __init__(self, agents, default_agent):
        self.config = SimpleNamespace(agents=agents)
        self._default_agent = default_agent

    def get_default_agent(self):
        return self._default_agent

    def get_agent_timeout(self, key):
        return 30

    def get_max_turns(self, key):
        return 5


class FakeFactory:
    def __init__(self, config):
        self.config = config
        self.container_id = "user-container"
        self.confine_tools = True
        self.action_gate = None
        self.stream_error = None
        self.wait = False
        self.built = []
        self._system_access_broker = None
        self._system_access_key = None

    def bind_system_access(self, broker, key):
        self._system_access_broker = broker
        self._system_access_key = key

    def _wrap_tool_with_policy(self, tool, name, kind):
        return tool

    async def create_agent(self, key):
        self.built.append(key)
        return SimpleNamespace(name=key)

    def _run_config(self, key, observer=None):
        return None

    async def _consume_stream(self, result, **kwargs):
        assert get_current_factory() is self
        if self.stream_error:
            raise self.stream_error
        if self.wait:
            await asyncio.Event().wait()


def _broker(configs, factories, visible):
    return SystemAccessBroker(
        keys=lambda: visible,
        config=configs.__getitem__,
        factory=factories.__getitem__,
        agents=lambda key: configs[key].config.agents,
        default_agent=lambda key: configs[key].get_default_agent(),
    )


@pytest.fixture
def runtime(monkeypatch):
    main_config = FakeConfig(
        {"coordinator": AgentConfig(name="Coordinator", model="m")}, "coordinator")
    knowledge_config = FakeConfig(
        {"reader": AgentConfig(name="Reader", model="m")}, "reader")
    # A system whose key agent opted out of cross-system calls.
    private_config = FakeConfig(
        {"solo": AgentConfig(name="Solo", model="m", key_agent=False)}, "solo")
    configs = {"main": main_config, "knowledge": knowledge_config, "private": private_config}
    factories = {key: FakeFactory(config) for key, config in configs.items()}
    visible = ["main", "knowledge", "private"]
    broker = _broker(configs, factories, visible)
    for key, factory in factories.items():
        factory.bind_system_access(broker, key)
    calls = []
    result = SimpleNamespace(final_output="Answer", cancel=Mock())

    def run_streamed(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(
        "core.system_access.get_runner", lambda: SimpleNamespace(run_streamed=run_streamed))
    parent = GridRunContext(
        factory=factories["main"], agent_id="coordinator", user_id="alice",
        container_id="user-container", context_id="caller-history",
        action_state=ActionRunState(task="Original user instruction"),
    )
    return SimpleNamespace(
        broker=broker, configs=configs, factories=factories, calls=calls,
        parent=parent, result=result, visible=visible,
    )


async def test_key_agent_sees_the_users_other_systems(runtime):
    listed = json.loads(json.dumps({"systems": runtime.broker.systems("main")}))
    assert [item["system"] for item in listed["systems"]] == ["knowledge"]
    # The opted-out system is never offered, not even listed.
    assert all(item["system"] != "private" for item in listed["systems"])


async def test_allowed_call_runs_the_targets_key_agent(runtime):
    with factory_path_context(runtime.parent.factory):
        answer = json.loads(
            await runtime.broker.delegate("main", "knowledge", "Find facts", runtime.parent))
        assert get_current_factory() is runtime.parent.factory
    assert answer["status"] == "completed"
    assert answer["system"] == "knowledge" and answer["agent"] == "reader"
    call = runtime.calls[0]
    child = call["context"]
    assert child.factory is runtime.factories["knowledge"]
    assert child.agent_id == "reader" and child.user_id == "alice"
    assert call["session"] is None and call["input"] == "Find facts"
    assert child.action_state.root is runtime.parent.action_state
    assert child.system_access_active is True


@pytest.mark.parametrize("target", ["main", "missing", "private"])
async def test_unavailable_targets_are_denied_before_any_build(runtime, target):
    with pytest.raises(SystemAccessDenied):
        await runtime.broker.delegate("main", target, "task", runtime.parent)
    assert not runtime.calls
    assert not any(factory.built for factory in runtime.factories.values())


async def test_only_the_key_agent_may_call(runtime):
    runtime.parent.agent_id = "helper"
    with pytest.raises(SystemAccessDenied, match="key agent"):
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
    assert not runtime.calls


async def test_tools_only_for_the_key_agent(runtime):
    tools = {tool.name: tool for tool in runtime.broker.tools(runtime.factories["main"], "main")}
    assert set(tools) == {"systems_list", "system_delegate"}
    assert set(tools["system_delegate"].params_json_schema["properties"]) == {"system", "task"}
    # A system whose key agent opted out gets no tools at all.
    assert runtime.broker.tools(runtime.factories["private"], "private") == []
    ctx = RunContextWrapper(context=runtime.parent)
    listed = json.loads(await tools["systems_list"].on_invoke_tool(ctx, "{}"))
    assert [item["system"] for item in listed["systems"]] == ["knowledge"]
    # A non-key agent borrowing the tool is blocked at call time.
    runtime.parent.agent_id = "helper"
    blocked = json.loads(await tools["system_delegate"].on_invoke_tool(
        ctx, json.dumps({"system": "knowledge", "task": "task"})))
    assert blocked["status"] == "blocked"
    assert not runtime.calls


async def test_wrong_registry_cannot_use_caller_tool(runtime):
    runtime.parent.factory._system_access_broker = object()
    with pytest.raises(SystemAccessDenied, match="registry"):
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
    assert not runtime.calls


async def test_hidden_system_is_not_loaded_even_for_the_key_agent(runtime):
    runtime.visible.remove("knowledge")
    assert [item["system"] for item in runtime.broker.systems("main")] == []
    with pytest.raises(SystemAccessDenied):
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
    assert not runtime.calls


async def test_nested_calls_fail_closed(runtime):
    runtime.parent.system_access_active = True
    with pytest.raises(SystemAccessDenied, match="re-delegation"):
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
    runtime.parent.system_access_active = False
    runtime.parent.action_depth = 1
    with pytest.raises(SystemAccessDenied, match="top-level"):
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
    assert not runtime.calls


@pytest.mark.parametrize("task", ["", "   ", "x" * 16001])
async def test_task_limits(runtime, task):
    with pytest.raises(SystemAccessDenied):
        await runtime.broker.delegate("main", "knowledge", task, runtime.parent)
    assert not runtime.calls


async def test_policy_cannot_replace_missing_trusted_task(runtime):
    runtime.factories["knowledge"].action_gate = object()
    runtime.parent.action_state = None
    with pytest.raises(SystemAccessDenied, match="trusted task"):
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)


async def test_isolation_mismatch_denied(runtime):
    runtime.factories["knowledge"].container_id = "another-user"
    with pytest.raises(SystemAccessDenied, match="isolation"):
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
    assert not runtime.calls


async def test_truncation(runtime):
    runtime.result.final_output = "x" * 20000
    output = json.loads(
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent))
    assert len(output["result"]) == 16000 and output["truncated"]


async def test_errors_hide_secrets_and_do_not_retry(runtime):
    runtime.factories["knowledge"].stream_error = RuntimeError("secret-token-provider-url")
    output = await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
    assert json.loads(output)["status"] == "failed"
    assert "secret-token" not in output
    assert len(runtime.calls) == 1
    runtime.result.cancel.assert_called_once()


async def test_timeout_cancels_target(runtime, monkeypatch):
    runtime.factories["knowledge"].wait = True
    monkeypatch.setattr("core.system_access.TIMEOUT_SECONDS", 0.01)
    output = json.loads(
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent))
    assert output["status"] == "failed" and "timed out" in output["reason"]
    runtime.result.cancel.assert_called_once()


async def test_cancellation_propagates_and_restores_factory(runtime):
    runtime.factories["knowledge"].stream_error = asyncio.CancelledError()
    with factory_path_context(runtime.parent.factory):
        with pytest.raises(asyncio.CancelledError):
            await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
        assert get_current_factory() is runtime.parent.factory
    runtime.result.cancel.assert_called_once()


async def test_stop_control_is_shared_and_already_stopped_caller_is_denied(runtime):
    from core.interruption import RunControl

    control = RunControl()
    runtime.parent.run_control = control
    await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
    assert runtime.calls[0]["context"].run_control is control
    control.request_stop()
    with pytest.raises(SystemAccessDenied, match="stopped"):
        await runtime.broker.delegate("main", "knowledge", "task", runtime.parent)
    assert len(runtime.calls) == 1


async def test_real_factory_assembly_gives_tools_only_to_the_key_agent(runtime):
    from core.agent_factory import AgentFactory

    config = runtime.configs["main"]
    config.project_tools_loader = None
    config.config.agents["coordinator"].name = "Same display name"
    config.config.agents["helper"] = AgentConfig(name="Same display name", model="m")
    factory = AgentFactory.__new__(AgentFactory)
    factory.config = config
    factory._tool_cache = {}
    factory._agent_cache = {}
    factory._system_access_broker = None
    factory._system_access_key = None
    factory.action_gate = None
    factory.bind_system_access(runtime.broker, "main")
    key = await factory._get_agent_tools(config.config.agents["coordinator"], "coordinator")
    other = await factory._get_agent_tools(config.config.agents["helper"], "helper")
    assert {tool.name for tool in key} == {"systems_list", "system_delegate"}
    assert other == []
    factory.bind_system_access(None, "main")
    assert await factory._get_agent_tools(config.config.agents["coordinator"], "coordinator") == []


async def test_registry_binds_its_own_broker_and_lists_user_systems(tmp_path):
    from core.config import Config
    from web_chat.systems import SystemRegistry

    common = {
        "providers": {"p": {"name": "p", "base_url": "http://localhost", "api_key": "test"}},
        "models": {"m": {"name": "m", "provider": "p"}},
    }
    knowledge = {**common, "settings": {"default_agent": "reader"}, "agents": {
        "reader": {"name": "Reader", "model": "m", "description": "Reads"},
    }}
    root = {**common, "settings": {"default_agent": "coordinator"}, "agents": {
        "coordinator": {"name": "Coordinator", "model": "m"},
    }, "routing": {"model": "m", "default_system": "main", "systems": {
        "main": {"config": "main.yaml"},
        "knowledge": {"config": "knowledge.yaml"},
        "admin_only": {"config": "knowledge.yaml", "admins_only": True},
    }}}
    (tmp_path / "knowledge.yaml").write_text(yaml.safe_dump(knowledge))
    root_path = tmp_path / "main.yaml"
    root_path.write_text(yaml.safe_dump(root))
    catalog = Config(str(root_path))
    registry = SystemRegistry(base_config=catalog, catalog=catalog, build_factory=FakeFactory)
    other = SystemRegistry(base_config=catalog, catalog=catalog, build_factory=FakeFactory)
    first = registry.factory("main")
    second = other.factory("main")
    assert first._system_access_broker is registry._system_access
    assert first._system_access_broker is not second._system_access_broker
    # The admin's registry lists the admins-only system; a user's does not.
    assert {item["system"] for item in registry._system_access.systems("main")} == {
        "knowledge", "admin_only"}
    user_registry = SystemRegistry(
        base_config=catalog, catalog=catalog, build_factory=FakeFactory, admin=False)
    assert {item["system"] for item in user_registry._system_access.systems("main")} == {
        "knowledge"}
    with pytest.raises(SystemAccessDenied):
        await user_registry._system_access.delegate(
            "main", "admin_only", "task",
            GridRunContext(factory=user_registry.factory("main"), agent_id="coordinator"))
    assert set(user_registry.built_factories()) == {"main"}


async def test_catalog_config_to_tool_to_target_execution(tmp_path, runtime):
    from core.config import Config
    from web_chat.systems import SystemRegistry

    common = {
        "providers": {"p": {"name": "p", "base_url": "http://localhost", "api_key": "test"}},
        "models": {"m": {"name": "m", "provider": "p"}},
    }
    knowledge = {**common, "settings": {"default_agent": "reader"}, "agents": {
        "reader": {"name": "Reader", "model": "m", "accept": True},
    }}
    root = {**common, "settings": {"default_agent": "coordinator"}, "agents": {
        "coordinator": {"name": "Coordinator", "model": "m"},
    }, "routing": {"model": "m", "default_system": "main", "systems": {
        "main": {"config": "main.yaml"},
        "knowledge": {"config": "knowledge.yaml"},
    }}}
    (tmp_path / "knowledge.yaml").write_text(yaml.safe_dump(knowledge))
    root_path = tmp_path / "main.yaml"
    root_path.write_text(yaml.safe_dump(root))
    catalog = Config(str(root_path))
    registry = SystemRegistry(base_config=catalog, catalog=catalog, build_factory=FakeFactory)
    source_factory = registry.factory("main")
    parent = GridRunContext(factory=source_factory, agent_id="coordinator", user_id="alice")
    tools = {tool.name: tool for tool in registry._system_access.tools(source_factory, "main")}
    output = json.loads(await tools["system_delegate"].on_invoke_tool(
        RunContextWrapper(context=parent),
        json.dumps({"system": "knowledge", "task": "Find facts"}),
    ))
    assert output["status"] == "completed"
    assert output["system"] == "knowledge" and output["agent"] == "reader"
    assert registry.factory("knowledge").built == ["reader"]
