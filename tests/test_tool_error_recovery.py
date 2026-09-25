"""A failing or mistaken tool call is reported to the agent; it never ends the run.

Covers the gate (a tool's own error is neither a denial nor a stop), the
outermost tool wrapper, tool-name resolution, and sub-agents, which hand their
caller a report instead of dying on an SDK error.
"""

import asyncio
import json
from types import SimpleNamespace

import agents
import pytest
from agents import Agent, Runner, function_tool
from agents.exceptions import AgentsException, MaxTurnsExceeded, ModelBehaviorError
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from core.action_policy import ActionRunState
from core.agent_factory import (
    AgentFactory,
    _build_missing_tool_stub,
    _resolve_model_tool_name,
)
from core.config import Config
from tests.test_action_policy import call, setup_gate


# --- gate -------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ModelBehaviorError("Invalid JSON input for tool x"),
        AgentsException("Error invoking MCP tool x: Unknown tool"),
        OSError("No such file"),
    ],
)
async def test_a_tool_error_does_not_stop_the_run(error):
    gate, _, state, ctx = setup_gate()

    async def failing(inner_ctx, args):
        raise error

    with pytest.raises(type(error)):
        await call(gate, ctx, invoke=failing)

    assert state.stopped is False
    assert state.denials == 0  # a failure is not a policy denial
    assert state.chain[-1]["outcome"] == "failed"
    assert (await call(gate, ctx)).startswith("ran:")


async def test_a_failed_call_stays_in_the_trajectory_the_validator_sees():
    gate, validator, _, ctx = setup_gate()

    async def failing(inner_ctx, args):
        raise RuntimeError("half done")

    with pytest.raises(RuntimeError):
        await call(gate, ctx, tool="deploy", invoke=failing)
    await call(gate, ctx)

    packet = validator.evaluate.await_args.args[0]
    assert [e["tool"] for e in packet["untrusted_chain"]["executed"]] == ["deploy"]


async def test_cancellation_still_stops_the_run():
    gate, _, state, ctx = setup_gate()

    async def cancelled(inner_ctx, args):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await call(gate, ctx, invoke=cancelled)
    assert state.stopped is True


# --- outermost wrapper and name resolution ------------------------------------


class Scripted(Model):
    """A model that plays back fixed responses, one per turn."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)

    async def get_response(self, *args, **kwargs):
        return ModelResponse(output=[self.outputs.pop(0)], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def tool_call(name, arguments):
    return ResponseFunctionToolCall(
        type="function_call", name=name, arguments=arguments, call_id="c1", id="c1"
    )


def message(text):
    return ResponseOutputMessage(
        id="m",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def tool_outputs(result):
    return [
        item.raw_item["output"]
        for item in result.new_items
        if item.type == "tool_call_output_item"
    ]


async def test_an_error_raised_outside_the_tool_function_reaches_the_agent():
    tool = function_tool(lambda text: text, name_override="lookup")

    async def broken_wrapper(ctx, args):
        raise RuntimeError("pipeline unavailable")

    tool.on_invoke_tool = broken_wrapper
    owner = SimpleNamespace(action_gate=None)
    AgentFactory._wrap_tool_with_policy(owner, tool, "lookup", "function")
    agent = Agent(
        name="worker",
        model=Scripted(tool_call("lookup", '{"text": "x"}'), message("done")),
        tools=[tool],
    )

    result = await Runner.run(agent, "go", max_turns=3)

    assert result.final_output == "done"
    assert "pipeline unavailable" in tool_outputs(result)[0]


async def test_an_unknown_tool_is_reported_with_the_closest_names():
    @function_tool
    def file_read(path: str) -> str:
        return path

    agent = Agent(
        name="worker",
        model=Scripted(tool_call("file_raed", '{"path": "a"}'), message("done")),
        tools=[file_read],
    )

    result = await Runner.run(agent, "go", max_turns=3)

    assert result.final_output == "done"
    assert "Did you mean: file_read?" in tool_outputs(result)[0]


@pytest.mark.parametrize(
    "called",
    ["call_worker<|channel|>commentary", "call_worker_commentary", "call_worker.final"],
)
def test_a_channel_suffix_resolves_to_the_tool(called):
    assert _resolve_model_tool_name(called, {"call_worker": object()}) == "call_worker"


def test_an_alias_is_resolved_before_a_suffix_is_stripped(monkeypatch):
    monkeypatch.setattr(
        "tools.function_tools.TOOL_ALIASES",
        {"improvement_review_final": "record_final_review"},
    )
    tools = {"record_final_review": object(), "improvement_review": object()}
    assert (
        _resolve_model_tool_name("improvement_review_final", tools)
        == "record_final_review"
    )


def test_an_unresolvable_name_is_kept_for_the_error_message():
    assert _resolve_model_tool_name("nothing_final", {"x": object()}) == "nothing_final"


async def test_missing_tool_stub_without_close_names_lists_the_tools():
    stub = _build_missing_tool_stub("zzz", ["alpha", "beta"])
    text = await stub.on_invoke_tool(None, "{}")
    assert "Did you mean" not in text and "alpha, beta" in text


# --- sub-agents ---------------------------------------------------------------

CONFIG = """
settings:
  default_agent: "test_agent"
  max_turns: 2
  working_directory: "."
  config_directory: "."
  mcp_enabled: false
  agent_logging:
    enabled: false
providers:
  openai:
    name: "openai"
    base_url: "https://api.openai.com/v1"
models:
  gpt-4:
    name: "gpt-4"
    provider: "openai"
prompt_templates:
  base: |
    You are a helpful assistant.
agents:
  test_agent:
    name: "Test Agent"
    model: "gpt-4"
    tools: []
    base_prompt: "base"
    description: "Test agent"
"""


class FailingStream:
    """A streamed run that made a tool call and then hit an SDK error."""

    def __init__(self, error):
        self.error = error
        self.final_output = None
        called = SimpleNamespace(
            type="tool_call_item",
            raw_item=SimpleNamespace(name="file_write", arguments="{}", call_id="c1"),
        )
        self.new_items = [called]

    async def stream_events(self):
        raise self.error
        yield  # pragma: no cover


def sub_agent_tool(tmp_path, monkeypatch, run_streamed):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(
        agents, "Runner", SimpleNamespace(run_streamed=staticmethod(run_streamed))
    )
    factory = AgentFactory(Config(str(config_path)))
    tool = factory._create_context_aware_agent_tool(
        "test_agent", SimpleNamespace(name="Test Agent"), "call_test_agent", "Delegate"
    )
    return factory, tool


def tool_ctx(user_id=None):
    return ToolContext(
        context=SimpleNamespace(
            action_state=ActionRunState(task="Review the diff"),
            action_depth=0,
            user_id=user_id,
        ),
        tool_name="call_test_agent",
        tool_call_id="call-1",
        tool_arguments="{}",
    )


@pytest.mark.parametrize(
    "error, reason",
    [
        (ModelBehaviorError("Tool x not found in agent"), "stopped on an error"),
        (MaxTurnsExceeded("Max turns (2) exceeded"), "reached its turn limit"),
    ],
)
async def test_a_stopped_sub_agent_reports_what_it_did(
    tmp_path, monkeypatch, error, reason
):
    _, tool = sub_agent_tool(
        tmp_path, monkeypatch, lambda **kwargs: FailingStream(error)
    )

    output = await tool.on_invoke_tool(tool_ctx(), '{"input": "Fix the bug"}')

    assert reason in output
    assert "file_write" in output  # the caller knows what may already be done
    assert "Context ID: ctx-" in output


async def test_a_named_context_continues_the_same_sub_agent_session(
    tmp_path, monkeypatch
):
    sessions = []

    def run_streamed(**kwargs):
        sessions.append(kwargs["session"].session_id)
        return SimpleNamespace(
            stream_events=lambda: _no_events(), final_output="report", new_items=[]
        )

    factory, tool = sub_agent_tool(tmp_path, monkeypatch, run_streamed)

    first = await tool.on_invoke_tool(tool_ctx("u1"), '{"input": "Start"}')
    context_id = first.rsplit("Context ID: ", 1)[1].strip()
    await tool.on_invoke_tool(
        tool_ctx("u1"), json.dumps({"input": f"Continue {context_id}"})
    )
    await tool.on_invoke_tool(
        tool_ctx("u2"), json.dumps({"input": f"Continue {context_id}"})
    )

    assert sessions[0] == sessions[1]
    assert sessions[2] != sessions[0]  # another user never opens that session


async def _no_events():
    return
    yield  # pragma: no cover


async def test_agent_tool_accepts_the_documented_argument_aliases(
    tmp_path, monkeypatch
):
    inputs = []

    def run_streamed(**kwargs):
        inputs.append(kwargs["input"])
        return SimpleNamespace(
            stream_events=lambda: _no_events(), final_output="report", new_items=[]
        )

    factory, tool = sub_agent_tool(tmp_path, monkeypatch, run_streamed)
    wrapped = factory._wrap_agent_tool(tool, "Test Agent")

    for arguments in ('{"task": "Check core/"}', '{"prompt": "Check core/"}'):
        output = await wrapped.on_invoke_tool(tool_ctx(), arguments)
        assert "report" in output

    assert inputs == ["Check core/", "Check core/"]
