"""parallel: an agent's own calls side by side, in lanes (core.parallel_lanes)."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import yaml
from agents import FunctionTool
from agents.tool_context import ToolContext
from agents.usage import Usage

from core.action_policy import ActionGate
from core.agent_factory import AgentFactory
from core.config import Config
from core.parallel_lanes import guard
from tools.parallel_tools import parallel_tool
from utils.tool_effects import DELEGATE, read, run, write


def _tool(name, effect, body, kind="function"):
    """A tool as the factory wraps it: the call runs as its lane allows."""

    async def invoke(ctx, args):
        return await guard(body, name, kind, effect, None, ActionGate._locator)(ctx, args)

    return FunctionTool(
        name=name,
        description=name,
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=invoke,
    )


def _context(tmp_path):
    factory = SimpleNamespace(
        config=SimpleNamespace(get_working_directory=lambda: str(tmp_path)), container_id=None
    )
    return ToolContext(
        context=SimpleNamespace(factory=factory, stream_observer=None),
        usage=Usage(),
        tool_name="parallel",
        tool_call_id="call-1",
        tool_arguments="",
    )


async def _parallel(tmp_path, tools, *calls, max_parallel=4):
    tool = parallel_tool(tools, max_parallel=max_parallel)
    payload = {
        "calls": [
            {"tool": name, "arguments": json.dumps(arguments), "write_paths": paths}
            for name, arguments, paths in calls
        ]
    }
    out = await asyncio.wait_for(tool.on_invoke_tool(_context(tmp_path), json.dumps(payload)), 5)
    return json.loads(out)


async def test_independent_calls_run_side_by_side_and_answer_in_call_order(tmp_path):
    started = {"a": asyncio.Event(), "b": asyncio.Event()}

    def waiting_for(other, answer):
        async def body(ctx, args):
            started[answer].set()
            # Finishes only once the other call is running too.
            await started[other].wait()
            return json.dumps({"answer": answer})

        return body

    out = await _parallel(
        tmp_path,
        [_tool("a", read(), waiting_for("b", "a")), _tool("b", read(), waiting_for("a", "b"))],
        ("a", {}, []),
        ("b", {}, []),
    )

    assert [result["output"] for result in out["results"]] == [{"answer": "a"}, {"answer": "b"}]
    assert out["wall_seconds"] <= out["sum_seconds"] + 0.1


async def test_calls_that_change_something_take_turns(tmp_path):
    active, peak = [0], [0]

    async def body(ctx, args):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.02)
        active[0] -= 1
        return "ok"

    shell = _tool("shell", run("command"), body)
    await _parallel(tmp_path, [shell], *[("shell", {"command": "make build"}, []) for _ in range(3)])
    assert peak[0] == 1

    peak[0] = 0
    await _parallel(tmp_path, [shell], *[("shell", {"command": "ls"}, []) for _ in range(3)])
    assert peak[0] == 3  # a read-only command changes nothing


async def test_a_file_write_must_stay_inside_its_calls_write_paths(tmp_path):
    written = []

    async def body(ctx, args):
        written.append(json.loads(args)["filepath"])
        return "written"

    edit = _tool("file_write", write("filepath"), body)
    out = await _parallel(
        tmp_path,
        [edit],
        ("file_write", {"filepath": "src/a.py"}, ["src"]),
        ("file_write", {"filepath": "docs/notes.md"}, ["tests"]),
        ("file_write", {"filepath": "README.md"}, []),
    )

    assert written == ["src/a.py"]
    assert "outside the paths this task may change (write_paths: tests)" in out["results"][1]["output"]
    assert "write_paths: none" in out["results"][2]["output"]


@pytest.mark.parametrize(
    "calls,expected",
    [
        ((("e", {}, ["src"]), ("e", {}, ["src/a.py"])), "both write"),
        ((("e", {}, ["../outside"]),), "is not in the workspace"),
        ((("missing", {}, []),), "'missing' is not one of your tools"),
        ((("parallel", {}, []),), "cannot run inside parallel"),
        ((("e", [1], []),), "must be a JSON object"),
    ],
)
async def test_a_wrong_batch_runs_nothing(tmp_path, calls, expected):
    ran = []

    async def body(ctx, args):
        ran.append(args)
        return "ran"

    out = await _parallel(tmp_path, [_tool("e", write("filepath"), body)], *calls)
    assert expected in out["error"] and "Nothing was run" in out["error"]
    assert out["available"] == ["e"]
    assert ran == []


async def test_a_delegated_agent_writes_in_its_lane_without_blocking_itself(tmp_path):
    async def write_body(ctx, args):
        return "written"

    edit = _tool("file_write", write("filepath"), write_body)

    async def agent_body(ctx, args):
        # The sub-agent's own write, in the lane its call inherited.
        return await edit.on_invoke_tool(ctx, json.dumps({"filepath": "src/x.py"}))

    out = await _parallel(
        tmp_path,
        [_tool("call_worker", None, agent_body, kind="agent"), _tool("delegate", DELEGATE, agent_body)],
        ("call_worker", {}, ["src/x.py"]),
        ("delegate", {}, ["src/y"]),
    )
    assert out["results"][0]["output"] == "written"
    assert "outside the paths" in out["results"][1]["output"]


def test_the_factory_binds_parallel_to_the_agents_own_tools(tmp_path):
    config = {
        "settings": {
            "default_agent": "coordinator",
            "working_directory": str(tmp_path / "work"),
            "logs_directory": str(tmp_path / "logs"),
            "mcp_enabled": False,
            "agent_logging": {"enabled": False},
        },
        "providers": {"p": {"name": "p", "base_url": "https://llm.test/v1", "api_key": "test"}},
        "models": {"m": {"name": "m", "provider": "p", "context_window": 100000}},
        "prompt_templates": {"base": "You help."},
        "tools": {
            "parallel": {"type": "function", "max_parallel": 2},
            "file_read": {"type": "function"},
            "grep_tool": {"type": "function"},
        },
        "agents": {
            "coordinator": {
                "name": "Coordinator",
                "model": "m",
                "tools": ["parallel", "file_read"],
                "base_prompt": "base",
            }
        },
    }
    (tmp_path / "work").mkdir()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    factory = AgentFactory(Config(str(path)), tracing_level=None)

    async def tool_names():
        tools = await factory._get_agent_tools(factory.config.get_agent("coordinator"), "coordinator")
        bound = next(tool for tool in tools if tool.name == "parallel")
        # The batch names the tools it may call: the agent's own, nothing else.
        context = _context(tmp_path)
        context.context = SimpleNamespace(factory=factory, stream_observer=None)
        out = json.loads(
            await bound.on_invoke_tool(
                context, json.dumps({"calls": [{"tool": "grep_tool", "arguments": "{}", "write_paths": []}]})
            )
        )
        return [tool.name for tool in tools], out

    names, out = asyncio.run(tool_names())
    assert names.count("parallel") == 1 and "read_file" in names  # file_read's SDK name
    assert "'grep_tool' is not one of your tools" in out["error"]
    assert out["available"] == ["read_file"]


async def test_calls_inside_a_serialized_step_join_it_instead_of_queueing():
    """parallel runs as one step of the conversation's serial pipeline; the
    steps of its calls join that step's scope, so they do not wait in line."""
    from core.tracing.pipeline_registry import PipelineRegistry

    registry = PipelineRegistry()
    pipeline_id = await registry.register_pipeline("test", "ctx-parallel01")
    started = [asyncio.Event(), asyncio.Event()]

    async def inner(index):
        async def step():
            started[index].set()
            await started[1 - index].wait()
            return index

        return await registry.run_serialized_step(
            pipeline_id=pipeline_id, agent_name=f"tool:{index}", step_coro_factory=step
        )

    async def batch():
        return await asyncio.gather(inner(0), inner(1))

    result = await asyncio.wait_for(
        registry.run_serialized_step(pipeline_id=pipeline_id, agent_name="tool:parallel", step_coro_factory=batch),
        5,
    )
    assert result == [0, 1]
