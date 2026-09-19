import asyncio
from types import SimpleNamespace

import pytest

from core.background_agents import BackgroundAgentSupervisor
from grid_mcp import create_server


class FakeCatalog:
    def model_keys(self):
        return ["worker-model"]

    def tool_names(self):
        return ["file_read", "file_write"]

    def require_model(self, model):
        if model not in self.model_keys():
            raise ValueError("unknown model")

    def require_tools(self, tools):
        selected = list(tools)
        unknown = set(selected) - set(self.tool_names())
        if unknown:
            raise ValueError("unknown tools")
        return selected

    def system_info(self, *, constraints):
        return {
            "workflow": {"launch_actions": 2},
            "models": [{"key": "worker-model"}],
            "tools": [{"name": name} for name in self.tool_names()],
            "constraints": constraints,
        }


class FakeFactory:
    def __init__(self):
        self.started = asyncio.Event()
        self.cleaned_up = False

    async def create_dynamic_agent(self, *, name, instructions, model_key, tool_names):
        del name, instructions, model_key
        return SimpleNamespace(
            tools=[SimpleNamespace(name=tool_name) for tool_name in tool_names]
        )

    async def run_agent_object_simple(self, worker, task, *, context_id):
        del worker, context_id
        self.started.set()
        if task == "never finish":
            await asyncio.Event().wait()
        if task == "too slow":
            await asyncio.sleep(2)
        return f"done: {task}"

    async def cleanup(self):
        self.cleaned_up = True


def make_supervisor(**kwargs):
    factory = FakeFactory()
    supervisor = BackgroundAgentSupervisor(
        factory=factory,
        catalog=FakeCatalog(),
        default_timeout_seconds=5,
        max_timeout_seconds=10,
        **kwargs,
    )
    return supervisor, factory


@pytest.mark.asyncio
async def test_two_action_contract_and_background_completion():
    supervisor, _factory = make_supervisor()

    system = supervisor.get_system_info()
    assert system["workflow"]["launch_actions"] == 2

    started = await supervisor.start_agent(
        model="worker-model",
        task="inspect repository",
        tools=["file_read"],
    )
    assert started["status"] in {"queued", "running"}

    waited = await supervisor.wait_agents([started["agent_id"]], timeout_seconds=1)
    assert waited["reason"] == "agent_event"
    assert waited["agents"][0]["status"] == "completed"
    assert waited["agents"][0]["result"] == "done: inspect repository"

    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_agent_deadline_and_interrupt_are_terminal_events():
    supervisor, factory = make_supervisor()

    slow = await supervisor.start_agent(
        model="worker-model",
        task="too slow",
        tools=[],
        timeout_seconds=1,
    )
    timed_out = await supervisor.wait_agents([slow["agent_id"]], timeout_seconds=2)
    assert timed_out["agents"][0]["status"] == "timed_out"

    blocked = await supervisor.start_agent(
        model="worker-model",
        task="never finish",
        tools=[],
    )
    await asyncio.wait_for(factory.started.wait(), timeout=1)
    interrupted = await supervisor.interrupt_agent(
        blocked["agent_id"], reason="Codex changed the plan"
    )
    assert interrupted["status"] == "interrupted"
    assert interrupted["error"] == "Codex changed the plan"

    await supervisor.shutdown()
    assert factory.cleaned_up is True


@pytest.mark.asyncio
async def test_mcp_exposes_only_the_minimal_lifecycle_surface():
    supervisor, _factory = make_supervisor()
    server = create_server(supervisor)

    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {
        "grid_get_system",
        "grid_start_agent",
        "grid_get_agents",
        "grid_wait_agents",
        "grid_interrupt_agent",
    }

    start_tool = next(tool for tool in tools if tool.name == "grid_start_agent")
    assert set(start_tool.inputSchema["required"]) == {"model", "task", "tools"}

    await supervisor.shutdown()
