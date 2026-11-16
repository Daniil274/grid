"""A sub-agent's required handoff: typed, fail-closed, carrying its identity.

A caller hands an executor the calls it must make before its model starts
(read the task in the tracker, claim it). These tests hold the contract
together: the result of that preparation is typed, a step that did not finish
stops the model instead of being silently dropped, the executor's init_tools
act for the caller's user in its container, and a refused call is reported -
never retried or worked around.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent_factory import AgentFactory
from core.factory.auto_run import (
    PreparationFailed,
    PreparationResult,
    failure_reason,
)
from core.factory.preparation import BLOCKED, DONE, FAILED, Step, StepOutcome
from core.factory.run_context import GridRunContext
from tools.orchestrator_tools import ORCHESTRATOR_TOOLS


def outcome(name, status=FAILED, detail="no issue found matching x-1"):
    return StepOutcome(Step(index=0, name=name, spec={"name": name}), status, detail=detail)


def prepared(text="", outcomes=(), phase="init_tools"):
    return PreparationResult(text, list(outcomes), phase=phase, required=True)


# --------------------------------------------------------------------------
# The typed result
# --------------------------------------------------------------------------


def test_a_preparation_is_ok_only_when_every_step_finished():
    assert prepared("ctx", [outcome("beads_show", DONE, "")]).ok
    assert prepared("", []).ok  # nothing required, nothing failed
    for status in (FAILED, BLOCKED):
        result = prepared("", [outcome("beads_show", status)])
        assert not result.ok
        assert [failure.name for failure in result.failures] == ["beads_show"]
        assert "beads_show" in result.failure_report("init_tools")


def test_a_textual_result_is_read_through_the_structured_contract_first():
    # The unified structured answer the tracker tools will return.
    assert failure_reason({"success": False, "error": "no beads database"}) == "no beads database"
    assert failure_reason('{"ok": false, "error": "denied"}') == "denied"
    assert failure_reason({"status": "failed", "reason": "timeout"}) == "timeout"
    assert failure_reason({"error": "Nothing was created."}) == "Nothing was created."
    # A success is a success, whatever it contains.
    assert failure_reason({"success": True, "output": "ok"}) is None
    assert failure_reason({"ids": ["x-1"], "waves": []}) is None
    assert failure_reason("3 open issues") is None
    # The legacy text contract of the beads tools until the unified one lands.
    assert failure_reason("❌ Error showing bead x-1: not found") == (
        "Error showing bead x-1: not found"
    )


# --------------------------------------------------------------------------
# The dynamic (orchestrate/background) path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, [], ["a task"], 0, True])
def test_non_mapping_tool_results_do_not_raise(value):
    assert failure_reason(value) is None


def dynamic_factory(monkeypatch, init_result):
    """A factory that builds a dynamic agent without a real config or model."""
    factory = object.__new__(AgentFactory)
    factory.container_id = "grid-container"
    factory._stream_observer = None
    factory.config = SimpleNamespace(
        get_working_directory=lambda: ".",
        config=SimpleNamespace(settings=SimpleNamespace(allowed_models=[])),
        is_mcp_enabled=lambda: False,
    )
    factory.resolve_model_key = lambda key: key or "gpt-4"
    factory.get_active_context_id = lambda: "ctx-conversation"
    factory.models = SimpleNamespace(
        is_allowed=lambda key: True,
        sdk_model=lambda key: (object(), SimpleNamespace()),
        settings=lambda cfg: None,
        input_modalities=lambda key: ["text"],
    )
    factory._resolve_tools_for_names = AsyncMock(return_value=([], [], []))
    factory._with_public_message_tool = lambda tools: tools
    factory._build_dynamic_agent_instructions = lambda instructions, names: instructions
    factory._execute_init_tools = AsyncMock(return_value=init_result)
    monkeypatch.setattr("core.agent_factory.Agent", lambda **kwargs: SimpleNamespace(**kwargs))
    return factory


async def test_init_tools_inherit_the_callers_user_container_and_observer(monkeypatch):
    """An executor's tracker calls must act for the caller, not for nobody."""
    factory = dynamic_factory(monkeypatch, prepared("ctx", [outcome("beads_show", DONE, "")]))
    observer = SimpleNamespace(kind="the caller's view")
    state = SimpleNamespace(task="the user's trusted task")
    control = SimpleNamespace()
    parent = GridRunContext(
        factory=factory,
        context_id="ctx-conversation",
        user_id="user-1",
        session="the caller's session",
        container_id="grid-container",
        pipeline_id="pipe-1",
        action_state=state,
        action_depth=2,
        stream_observer=observer,
        run_control=control,
        metadata={"trace": "t-1"},
        system_access_active=True,
    )

    await factory.create_dynamic_agent(
        name="executor",
        instructions="do it",
        init_tools=[{"name": "beads_show", "parameters": {"bead_id": "x-1"}}],
        action_state=state,
        run_context=parent,
    )

    ctx = factory._execute_init_tools.await_args.args[2]
    assert ctx.user_id == "user-1"
    assert ctx.container_id == "grid-container"
    assert ctx.stream_observer is observer
    assert ctx.action_state is state
    assert ctx.run_control is control
    assert ctx.session == "the caller's session"
    assert ctx.pipeline_id == "pipe-1"
    assert ctx.metadata == {"trace": "t-1"}
    assert ctx.action_depth == 3
    assert ctx.context_id == "ctx-conversation"


async def test_a_failed_required_init_tool_stops_the_dynamic_agent(monkeypatch):
    bad = prepared("", [outcome("beads_update", FAILED, "no issue found matching x-1")])
    factory = dynamic_factory(monkeypatch, bad)
    built = []
    monkeypatch.setattr(
        "core.agent_factory.Agent",
        lambda **kwargs: built.append(kwargs) or SimpleNamespace(**kwargs),
    )

    with pytest.raises(PreparationFailed) as error:
        await factory.create_dynamic_agent(
            name="executor",
            instructions="do it",
            init_tools=[{"name": "beads_update", "parameters": {"bead_id": "x-1", "claim": True}}],
        )

    message = str(error.value)
    assert "beads_update" in message and "no issue found matching x-1" in message
    assert error.value.result.ok is False
    assert built == []  # the model was never given an agent to start


async def test_a_policy_block_is_a_failed_preparation_and_is_not_retried(monkeypatch):
    blocked = prepared("", [outcome("beads_update", BLOCKED, "denied by policy: run_stopped")])
    factory = dynamic_factory(monkeypatch, blocked)

    with pytest.raises(PreparationFailed) as error:
        await factory.create_dynamic_agent(
            name="executor",
            instructions="do it",
            init_tools=[{"name": "beads_update"}],
        )

    assert "denied by policy" in str(error.value)
    # The batch ran once: a refusal is not retried automatically.
    assert factory._execute_init_tools.await_count == 1


# --------------------------------------------------------------------------
# The configured Agent tool path
# --------------------------------------------------------------------------


def agent_tool_factory(init_result):
    factory = object.__new__(AgentFactory)
    factory.container_id = None
    factory._stream_observer = None
    factory.config = SimpleNamespace(get_working_directory=lambda: ".")
    factory._resolve_tools_for_names = AsyncMock(return_value=([], [], []))
    factory._execute_init_tools = AsyncMock(return_value=init_result)
    factory._auto_run_preamble = AsyncMock(return_value="")
    factory.config.get_agent = lambda agent_key: SimpleNamespace(auto_run_tools=None)
    factory._get_agent_session = lambda agent_key, key: SimpleNamespace()
    factory._strip_legacy_context_footers = AsyncMock(return_value=None)
    return factory


def agent_tool_context(factory):
    parent = SimpleNamespace(
        factory=factory,
        context_id="ctx-conversation",
        user_id="user-1",
        pipeline_id=None,
        step_id=None,
        action_state=None,
        action_depth=0,
        system_access_active=False,
        stream_observer=None,
        run_control=None,
    )
    return SimpleNamespace(context=parent, tool_call_id="call-1")


async def test_the_agent_tool_does_not_start_the_model_when_init_tools_fail():
    bad = prepared("", [outcome("beads_show", FAILED, "no beads database found")])
    factory = agent_tool_factory(bad)
    sub_agent = SimpleNamespace(name="worker", tools=[], instructions="x")
    tool = factory._create_context_aware_agent_tool(
        "worker", sub_agent, "call_worker", "hand a task to worker"
    )
    ctx = agent_tool_context(factory)
    runner = SimpleNamespace(run_streamed=AsyncMock())
    predictor = factory.__dict__.get("_consume_stream")

    out = await tool.on_invoke_tool(
        ctx,
        json.dumps(
            {
                "input": "do the task",
                "init_tools": json.dumps([{"name": "beads_show", "parameters": {"bead_id": "x-1"}}]),
            }
        ),
    )

    assert out.startswith("❌ Agent 'worker' did not start")
    assert "beads_show" in out and "no beads database found" in out
    assert "do the task" not in out  # it was not run
    assert runner.run_streamed.await_count == 0
    assert predictor is None
    # Its required calls were made once, under the caller's user.
    run_ctx = factory._execute_init_tools.await_args.args[2]
    assert run_ctx.user_id == "user-1"


async def test_the_agent_tool_rejects_init_tools_it_cannot_read():
    factory = agent_tool_factory(prepared("", []))
    sub_agent = SimpleNamespace(name="worker", tools=[], instructions="x")
    tool = factory._create_context_aware_agent_tool(
        "worker", sub_agent, "call_worker", "hand a task to worker"
    )

    out = await tool.on_invoke_tool(
        agent_tool_context(factory),
        json.dumps({"input": "do it", "init_tools": "not json"}),
    )

    assert out.startswith("❌ Agent 'worker' did not start")
    assert "not a valid JSON list" in out
    factory._execute_init_tools.assert_not_awaited()


# --------------------------------------------------------------------------
# The orchestrate tool reports the refusal to its caller
# --------------------------------------------------------------------------


def orchestrate_factory(dynamic):
    factory = SimpleNamespace(
        get_active_context_id=lambda: "ctx-conversation",
        config=SimpleNamespace(get_tool=lambda key: (_ for _ in ()).throw(Exception("not found"))),
        resolve_model_key=lambda key: key or "gpt-4",
        get_model=lambda key: SimpleNamespace(),
        create_dynamic_agent=dynamic,
        run_agent_object=AsyncMock(),
        context_manager=SimpleNamespace(
            has_context=lambda chat_id: False,
            get_metadata=lambda *args, **kwargs: None,
            set_metadata=lambda *args, **kwargs: None,
            start_new_context=lambda: "ctx-conversation",
        ),
    )
    return factory


async def test_orchestrate_reports_an_init_tool_that_did_not_finish():
    failure = PreparationFailed(
        prepared("", [outcome("beads_show", FAILED, "no beads database found")]),
        "init_tools of agent 'executor-abcd1234'",
    )
    factory = orchestrate_factory(AsyncMock(side_effect=failure))
    ctx = SimpleNamespace(
        context=SimpleNamespace(
            factory=factory,
            user_id="user-1",
            context_id="ctx-conversation",
            pipeline_id=None,
            action_state=None,
            action_depth=0,
            stream_observer=None,
            run_control=None,
        ),
        tool_call_id="call-1",
    )

    out = await ORCHESTRATOR_TOOLS["orchestrate"].on_invoke_tool(
        ctx,
        input=json.dumps(
            {"task": "goal", "init_tools": json.dumps([{"name": "beads_show"}])}
        ),
    )

    assert out.startswith("❌ orchestrate:")
    assert "beads_show" in out and "no beads database found" in out
    assert "did not start" in out
    factory.run_agent_object.assert_not_awaited()


async def test_orchestrate_does_not_start_an_executor_on_unreadable_init_tools():
    factory = orchestrate_factory(AsyncMock())
    ctx = SimpleNamespace(
        context=SimpleNamespace(
            factory=factory,
            user_id="user-1",
            context_id="ctx-conversation",
            pipeline_id=None,
            action_state=None,
            action_depth=0,
            stream_observer=None,
            run_control=None,
        ),
        tool_call_id="call-1",
    )

    out = await ORCHESTRATOR_TOOLS["orchestrate"].on_invoke_tool(
        ctx, input=json.dumps({"task": "goal", "init_tools": "{not json"})
    )

    assert out.startswith("❌ orchestrate:")
    assert "did not start" in out and "init_tools" in out
    factory.create_dynamic_agent.assert_not_awaited()


# --------------------------------------------------------------------------
# Background workers
# --------------------------------------------------------------------------


async def test_a_background_worker_acts_for_the_hosts_user():
    from core.background_agents import BackgroundAgentSupervisor

    created = {}

    class Factory:
        async def create_dynamic_agent(self, *, name, instructions, model_key, tool_names):
            return SimpleNamespace(tools=[SimpleNamespace(name=t) for t in tool_names])

        async def run_agent_object_simple(self, worker, task, **kwargs):
            created.update(kwargs)
            return "done"

    supervisor = BackgroundAgentSupervisor(
        factory=Factory(),
        catalog=SimpleNamespace(
            require_model=lambda model: None,
            require_tools=lambda tools: list(tools),
        ),
        user_id="user-1",
    )
    started = await supervisor.start_agent(model="m", task="do it", tools=[])
    await supervisor.wait_agents([started["agent_id"]], timeout_seconds=5)

    assert created["user_id"] == "user-1"
    assert created["timeout"] == 0
