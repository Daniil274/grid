"""beads_plan: a whole plan - tasks and dependencies - in one call."""

import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from tools import beads_tools
from tools.beads_tools import PlanTask, _graph_plan


def task(key, depends_on=(), **fields):
    return PlanTask(key=key, title=key.title(), description=f"do {key}", depends_on=list(depends_on), **fields)


def test_the_plan_is_the_graph_bd_reads():
    plan = _graph_plan([
        task("research", acceptance="five sources"),
        task("compare", ["research", "demo-42"]),
    ])
    assert plan["nodes"][0] == {
        "key": "research", "title": "Research", "description": "do research",
        "priority": 2, "acceptance_criteria": "five sources",
    }
    assert "acceptance_criteria" not in plan["nodes"][1]
    # A key of the plan, else an existing task's id; the edge reads "depends on".
    assert plan["edges"] == [
        {"from_key": "compare", "to_key": "research"},
        {"from_key": "compare", "to_id": "demo-42"},
    ]


@pytest.mark.parametrize(
    "tasks,message",
    [
        ([], "at least one task"),
        ([task("a"), task("a")], "unique: a"),
        ([task("a", ["a"])], "cannot depend on itself"),
        ([PlanTask(key=" ", title="t", description="d")], "needs a key"),
    ],
)
def test_a_wrong_plan_is_named_before_bd_is_asked(tasks, message):
    with pytest.raises(ValueError, match=message):
        _graph_plan(tasks)


def context_in(directory):
    return ToolContext(
        context=SimpleNamespace(factory=None, container_id=None),
        tool_name="beads_plan", tool_call_id="c1", tool_arguments="{}",
    )


async def run_plan(monkeypatch, tmp_path, tasks, replies):
    """Run the tool in *tmp_path* with bd answering *replies* in order."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beads").mkdir(exist_ok=True)
    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        if args[:2] == ["create", "--graph"]:
            calls.append(json.loads((tmp_path / args[2]).read_text(encoding="utf-8")))
        return replies.pop(0)

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    raw = json.dumps({"tasks": tasks})
    result = await beads_tools.beads_plan.on_invoke_tool(context_in(tmp_path), raw)
    return json.loads(result), calls


def ok(data):
    return {"success": True, "output": json.dumps(data), "error": "", "data": data, "exit_code": 0}


async def test_one_call_creates_the_plan_and_names_the_first_wave(monkeypatch, tmp_path):
    tasks = [
        {"key": "research", "title": "Research", "description": "find"},
        {"key": "compare", "title": "Compare", "description": "compare", "depends_on": ["research"]},
        {"key": "docs", "title": "Docs", "description": "read"},
    ]
    replies = [
        ok({"ids": {"compare": "p-2", "docs": "p-3", "research": "p-1"}}),
        ok([{"id": "p-1"}, {"id": "p-3"}, {"id": "older-9"}]),
    ]
    result, calls = await run_plan(monkeypatch, tmp_path, tasks, replies)

    # The plan's order, not bd's.
    assert list(result["ids"]) == ["research", "compare", "docs"]
    assert result["ready"] == [
        {"key": "research", "id": "p-1", "title": "Research"},
        {"key": "docs", "id": "p-3", "title": "Docs"},
    ]
    assert result["waiting"] == [{"key": "compare", "id": "p-2", "waits_for": ["research"]}]
    assert calls[0][:2] == ["create", "--graph"] and calls[1]["edges"] == [{"from_key": "compare", "to_key": "research"}]
    assert calls[2][0] == "ready"
    # The plan file is gone once bd has read it.
    assert not list((tmp_path / ".beads").glob("plan-*.json"))


async def test_bds_refusal_creates_nothing_and_says_why(monkeypatch, tmp_path):
    error = {"error": "invalid graph plan: graph contains a blocking dependency cycle involving node \"x\""}
    replies = [{"success": False, "output": json.dumps(error), "error": "", "data": None, "exit_code": 1}]
    tasks = [
        {"key": "x", "title": "X", "description": "", "depends_on": ["y"]},
        {"key": "y", "title": "Y", "description": "", "depends_on": ["x"]},
    ]
    result, calls = await run_plan(monkeypatch, tmp_path, tasks, replies)

    assert result == {"error": error["error"] + ". Nothing was created."}
    assert not list((tmp_path / ".beads").glob("plan-*.json"))


async def test_without_a_tracker_it_asks_for_beads_init(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    raw = json.dumps({"tasks": [{"key": "a", "title": "A", "description": ""}]})
    result = json.loads(await beads_tools.beads_plan.on_invoke_tool(context_in(tmp_path), raw))
    assert "beads_init" in result["error"]


def test_the_plan_is_declared_like_the_other_tracker_tools():
    assert beads_tools.TOOL_EFFECTS["beads_plan"].kind == "write"
    assert beads_tools.TOOL_ISOLATION["beads_plan"] == beads_tools.TOOL_ISOLATION["beads_create"]
    assert "beads_plan" in beads_tools.TOOL_REQUIREMENTS
