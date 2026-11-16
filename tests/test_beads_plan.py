"""beads_plan: a whole plan - tasks and dependencies - in one call."""

import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from core import tracker_operations
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


async def run_plan(monkeypatch, tmp_path, tasks, replies, request_id=None):
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
    if request_id is not None:
        raw = json.dumps({"tasks": tasks, "request_id": request_id})
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
    replies = [
        {"success": False, "output": json.dumps(error), "error": "", "data": None, "exit_code": 1},
        # The tracker is asked for the operation's marker before "nothing was
        # created" is said: a refusal may still have left a partial graph.
        ok([]),
    ]
    tasks = [
        {"key": "x", "title": "X", "description": "", "depends_on": ["y"]},
        {"key": "y", "title": "Y", "description": "", "depends_on": ["x"]},
    ]
    result, calls = await run_plan(monkeypatch, tmp_path, tasks, replies)

    assert result["error"] == error["error"] + ". Nothing was created."
    assert result["committed"] is False and result["code"] == "tracker_rejected"
    assert [call for call in calls if isinstance(call, list)][-1][:2] == ["list", "--external-contains"]
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


# -- the durable operation protocol -------------------------------------------
#
# These faults are injected at the bd boundary: the create may answer, may be
# stopped on timeout, or may even have committed before its answer was lost.
# What is asserted is the plan's own honesty, not a mock's shape.

TWO = [
    {"key": "a", "title": "A", "description": "do a"},
    {"key": "b", "title": "B", "description": "do b", "depends_on": ["a"]},
]


def timing_out():
    """bd stopped on timeout: it answered nothing, and may have written."""
    return {"success": False, "output": "", "error": "", "data": None, "exit_code": -9, "timed_out": True}


def bd_that(graphs, replies, list_answer):
    """A fake bd: records every graph it was given, answers *replies*, lists *list_answer*.

    *list_answer* is called with the graph the create was given, so a test can
    answer with exactly the markers bd would have written.
    """
    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        if args[:2] == ["create", "--graph"]:
            graph = json.loads((tmp_path_of[0] / args[2]).read_text(encoding="utf-8"))
            graphs.append(graph)
            return replies.pop(0)
        if args[:2] == ["list", "--external-contains"]:
            return ok(list_answer(graphs[0]) if graphs else [])
        if args[0] == "ready":
            return replies.pop(0) if replies else ok([])
        raise AssertionError(f"unexpected bd call: {args}")

    return bd


tmp_path_of = [None]


def markers(graph, ids):
    """What bd would return for an operation's markers: the whole marked tasks.

    A marker alone is not proof of content, so the fake answers with the
    fields and edges the graph asked for, as a real ``bd list --json`` does.
    """
    depends: dict[str, list] = {node["key"]: [] for node in graph["nodes"]}
    for edge in graph.get("edges", []):
        target = ids.get(edge["to_key"]) if "to_key" in edge else edge.get("to_id")
        depends.setdefault(edge["from_key"], []).append(target)
    items = []
    for node in graph["nodes"]:
        item = {
            "id": ids[node["key"]],
            "external_ref": node["external_ref"],
            "title": node["title"],
            "description": node.get("description", ""),
            "priority": node.get("priority", 2),
            "issue_type": "task",
            "dependencies": [{"depends_on_id": dep} for dep in depends.get(node["key"], []) if dep],
        }
        if node.get("acceptance_criteria"):
            item["acceptance_criteria"] = node["acceptance_criteria"]
        items.append(item)
    return items


async def plan_with(monkeypatch, tmp_path, tasks, bd, request_id=None):
    tmp_path_of[0] = tmp_path
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beads").mkdir(exist_ok=True)
    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    raw = json.dumps({"tasks": tasks, **({"request_id": request_id} if request_id else {})})
    return json.loads(await beads_tools.beads_plan.on_invoke_tool(context_in(tmp_path), raw))


async def test_a_lost_answer_is_reconciled_from_the_marker(monkeypatch, tmp_path):
    graphs, calls = [], []
    bd = bd_that(graphs, [timing_out()], lambda graph: markers(graph, {"a": "p-1", "b": "p-2"}))
    original = bd

    def tracking(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    result = await plan_with(monkeypatch, tmp_path, TWO, tracking, request_id="req-1")

    assert result["committed"] is True and result["reconciled"] is True
    assert result["ids"] == {"a": "p-1", "b": "p-2"}
    # The marker rode the same write: it is the plan's own, per task.
    assert [node["external_ref"] for node in graphs[0]["nodes"]] == [
        f"grid-op:{result['operation_id']}:a", f"grid-op:{result['operation_id']}:b",
    ]
    # The graph was created once; the answer lost, not the plan.
    assert len([call for call in calls if call[:2] == ["create", "--graph"]]) == 1


async def test_a_failed_ready_keeps_every_id_and_says_the_wave_is_unknown(monkeypatch, tmp_path):
    graphs = []
    ready_error = {"success": False, "output": "", "error": "database is locked", "data": None, "exit_code": 1}
    bd = bd_that(graphs, [ok({"ids": {"a": "p-1", "b": "p-2"}}), ready_error], lambda graph: [])
    result = await plan_with(monkeypatch, tmp_path, TWO, bd)

    assert result["committed"] is True
    assert result["ids"] == {"a": "p-1", "b": "p-2"}
    assert result["ready"] == [] and result["ready_unknown"] is True
    assert "database is locked" in result["ready_error"]


async def test_the_same_request_is_answered_from_the_journal_without_a_second_write(monkeypatch, tmp_path):
    graphs, replies = [], [ok({"ids": {"a": "p-1", "b": "p-2"}}), ok([{"id": "p-1"}])]
    calls = []
    bd = bd_that(graphs, replies, lambda graph: [])
    original = bd

    def tracking(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    first = await plan_with(monkeypatch, tmp_path, TWO, tracking, request_id="req-7")
    creates = len([call for call in calls if call[:2] == ["create", "--graph"]])
    second = await plan_with(monkeypatch, tmp_path, TWO, tracking, request_id="req-7")

    assert first["ids"] == second["ids"] == {"a": "p-1", "b": "p-2"}
    assert second["duplicate"] is True and second["operation_id"] == first["operation_id"]
    # Repeating the request wrote nothing: the journal answered.
    assert len([call for call in calls if call[:2] == ["create", "--graph"]]) == creates == 1


async def test_a_crash_between_the_write_and_the_journal_is_adopted(monkeypatch, tmp_path):
    tmp_path_of[0] = tmp_path
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beads").mkdir()
    # The crashed call: its "started" record is on disk, nothing concluded it.
    # Its fingerprint is the one the tool itself derives from this very plan
    # (the same request id with a different payload is a conflict, not a repeat).
    plan = _graph_plan([PlanTask(**task) for task in TWO])
    crashed = tracker_operations.begin(
        str(tmp_path),
        "plan",
        request_id="req-crash",
        actor="grid",  # the crashed call was this tool, which names its actor
        detail={"keys": ["a", "b"], "nodes": plan["nodes"], "edges": plan["edges"]},
    )

    seen = []

    def bd(args, **kwargs):
        seen.append(args)
        if args[:2] == ["list", "--external-contains"]:
            prefix = tracker_operations.marker_prefix(crashed.operation_id)
            graph = dict(plan, nodes=[dict(node, external_ref=f"{prefix}:{node['key']}")
                                      for node in plan["nodes"]])
            return ok(markers(graph, {"a": "p-1", "b": "p-2"}))
        if args[0] == "ready":
            return ok([{"id": "p-1"}])
        raise AssertionError(f"nothing may be created again: {args}")

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = json.loads(await beads_tools.beads_plan.on_invoke_tool(
        context_in(tmp_path), json.dumps({"tasks": TWO, "request_id": "req-crash"})
    ))

    assert result["committed"] is True and result["reconciled"] is True
    assert result["ids"] == {"a": "p-1", "b": "p-2"}
    assert not [call for call in seen if call[:2] == ["create", "--graph"]]


async def test_a_partial_creation_keeps_the_ids_it_made_and_is_not_repeated(monkeypatch, tmp_path):
    graphs = []
    bd = bd_that(graphs, [timing_out()], lambda graph: markers(graph, {"a": "p-1", "b": "p-2"})[:1])
    result = await plan_with(monkeypatch, tmp_path, TWO, bd)

    assert result["committed"] is None and result["code"] == "tracker_plan_partial"
    assert result["ids"] == {"a": "p-1"} and result["retryable"] is False


async def test_a_wrong_request_id_is_refused_before_bd_is_asked(monkeypatch, tmp_path):
    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        return ok({})

    result = await plan_with(monkeypatch, tmp_path, TWO, bd, request_id="not a key!")

    assert result["code"] == "tracker_request_id" and result["committed"] is False
    assert calls == []


async def test_markers_with_the_wrong_edges_are_not_a_commit(monkeypatch, tmp_path):
    graphs = []

    def list_answer(graph):
        items = markers(graph, {"a": "p-1", "b": "p-2"})
        for item in items:
            item["dependencies"] = []  # the edge b -> a never reached the tracker
        return items

    bd = bd_that(graphs, [timing_out()], list_answer)
    result = await plan_with(monkeypatch, tmp_path, TWO, bd, request_id="edges-1")

    # Every key is marked, but the graph is not the one asked for.
    assert result["committed"] is None and result["code"] == "tracker_plan_mismatch"
    assert result["ids"] == {"a": "p-1", "b": "p-2"}
    assert any("dependencies" in problem for problem in result["problems"])


async def test_markers_with_the_wrong_content_are_not_a_commit(monkeypatch, tmp_path):
    graphs = []

    def list_answer(graph):
        items = markers(graph, {"a": "p-1", "b": "p-2"})
        items[0]["title"] = "Some other title"
        items[1]["priority"] = 0
        return items

    bd = bd_that(graphs, [timing_out()], list_answer)
    result = await plan_with(monkeypatch, tmp_path, TWO, bd, request_id="content-1")

    assert result["committed"] is None and result["code"] == "tracker_plan_mismatch"
    assert any("different title" in problem for problem in result["problems"])
    assert any("different priority" in problem for problem in result["problems"])


async def test_a_repeat_does_not_adopt_a_plan_with_other_content(monkeypatch, tmp_path):
    plan = _graph_plan([PlanTask(**task) for task in TWO])
    crashed = tracker_operations.begin(
        str(tmp_path),
        "plan",
        request_id="req-other",
        actor="grid",
        detail={"keys": ["a", "b"], "nodes": plan["nodes"], "edges": plan["edges"]},
    )

    def bd(args, **kwargs):
        if args[:2] == ["list", "--external-contains"]:
            prefix = tracker_operations.marker_prefix(crashed.operation_id)
            graph = dict(plan, nodes=[dict(node, external_ref=f"{prefix}:{node['key']}")
                                      for node in plan["nodes"]])
            items = markers(graph, {"a": "p-1", "b": "p-2"})
            items[1]["dependencies"] = []  # b does not depend on a
            return ok(items)
        raise AssertionError(f"nothing may be created again: {args}")

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beads").mkdir(exist_ok=True)
    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = json.loads(await beads_tools.beads_plan.on_invoke_tool(
        context_in(tmp_path), json.dumps({"tasks": TWO, "request_id": "req-other"})
    ))

    assert result["committed"] is None and result["code"] == "tracker_plan_mismatch"
    assert result["duplicate"] is True


async def test_a_redirect_plan_input_lives_in_the_actual_tracker(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    alias = tmp_path / "alias"
    (shared / ".beads").mkdir(parents=True)
    (shared / ".beads" / "metadata.json").write_text(
        json.dumps({"backend": "dolt", "database": "beads.db"}), encoding="utf-8")
    (alias / ".beads").mkdir(parents=True)
    (alias / ".beads" / "redirect").write_text("../shared/.beads\n", encoding="utf-8")
    monkeypatch.chdir(alias)
    calls = []

    def bd(args, **kwargs):
        from pathlib import Path
        calls.append((args, kwargs))
        assert kwargs["cwd"] == str(shared)
        if args[:2] == ["create", "--graph"]:
            assert (Path(kwargs["cwd"]) / args[2]).is_file()
            return ok({"ids": {"a": "p-1"}})
        return ok([{"id": "p-1"}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = json.loads(await beads_tools.beads_plan.on_invoke_tool(
        context_in(alias), json.dumps({"tasks": [
            {"key": "a", "title": "A", "description": "do a"}], "request_id": "redirect-plan"})))
    assert result["committed"] is True
    assert not list((shared / ".beads").glob("plan-*.json"))
    assert calls


async def test_a_plan_input_write_refusal_is_typed_before_bd_runs(monkeypatch, tmp_path):
    from pathlib import Path
    (tmp_path / ".beads").mkdir()
    monkeypatch.chdir(tmp_path)
    original = Path.write_text

    def write_text(path, *args, **kwargs):
        if path.name.startswith("plan-"):
            raise PermissionError("graph input is read-only")
        return original(path, *args, **kwargs)

    def no_bd(*args, **kwargs):
        pytest.fail("bd must not run without a graph input")

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(beads_tools, "_run_bd_command", no_bd)
    result = json.loads(await beads_tools.beads_plan.on_invoke_tool(
        context_in(tmp_path), json.dumps({"tasks": [
            {"key": "a", "title": "A", "description": "do a"}]})))
    assert result["isError"] is True and result["committed"] is False
    assert result["code"] == "tracker_plan_input"
