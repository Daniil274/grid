"""The operation protocol of the single write tools: repeats, lost answers, markers."""

import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from agents.tool_context import ToolContext

from core import tracker_operations
from tools import beads_tools


def context_in(directory):
    return ToolContext(
        context=SimpleNamespace(factory=None, container_id=None),
        tool_name="beads", tool_call_id="c1", tool_arguments="{}",
    )


def ok(data, output=None):
    return {"success": True, "output": json.dumps(data) if output is None else output, "error": "", "data": data, "exit_code": 0}


def timed_out():
    return {"success": False, "output": "", "error": "", "data": None, "exit_code": -9, "timed_out": True}


async def invoke(tool, monkeypatch, directory, arguments):
    monkeypatch.chdir(directory)
    (directory / ".beads").mkdir(exist_ok=True)
    return await tool.on_invoke_tool(context_in(directory), json.dumps(arguments))


# -- create -------------------------------------------------------------------

async def test_the_same_create_request_makes_one_bead(monkeypatch, tmp_path):
    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        return ok({"id": "p-1", "title": "Fix", "status": "open"})

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    first = json.loads(await invoke(beads_tools.beads_create, monkeypatch, tmp_path, {"title": "Fix", "request_id": "c-1"}))
    second = json.loads(await invoke(beads_tools.beads_create, monkeypatch, tmp_path, {"title": "Fix", "request_id": "c-1"}))

    assert first["id"] == second["id"] == "p-1"
    assert second["duplicate"] is True
    assert len(calls) == 1
    # The marker is bd's own external_ref, so a lost answer can be looked up.
    assert calls[0][:1] == ["create"] and any(str(arg).startswith("grid-op:") for arg in calls[0])


async def test_a_lost_create_answer_is_reconciled_by_its_marker(monkeypatch, tmp_path):
    reference = ["?"]

    def bd(args, **kwargs):
        if args[0] == "create":
            # The op id is minted inside the call; the fake list answers with
            # exactly the marker this create wrote.
            reference[0] = args[args.index("--external-ref") + 1]
            return timed_out()
        return ok([{"id": "p-7", "title": "Fix", "description": "", "priority": 1,
                    "issue_type": "task", "external_ref": reference[0]}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = json.loads(await invoke(beads_tools.beads_create, monkeypatch, tmp_path, {"title": "Fix"}))

    assert result["id"] == "p-7" and result["committed"] is True and result["reconciled"] is True


async def test_a_lost_create_answer_with_no_marker_is_unknown(monkeypatch, tmp_path):
    def bd(args, **kwargs):
        if args[0] == "create":
            return timed_out()
        return ok([])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = await invoke(beads_tools.beads_create, monkeypatch, tmp_path, {"title": "Fix"})

    failure = json.loads(result)
    assert failure["isError"] is True and failure["code"] == "tracker_unknown"
    assert failure["committed"] is None and "may or may not" in failure["error"]
    [operation] = _journal(tmp_path)
    view = tracker_operations.lookup(tmp_path, operation_id=operation["operation_id"])
    # Nothing may repeat it blindly: the journal keeps the unknown conclusion.
    assert view["state"] == tracker_operations.UNKNOWN and view["committed"] is None


async def test_a_refusal_before_anything_ran_is_definite(monkeypatch, tmp_path):
    def bd(args, **kwargs):
        return {"success": False, "output": "", "error": "The tracker is busy.", "data": None,
                "exit_code": -1, "failed_before_run": True, "code": "tracker_busy"}

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = await invoke(beads_tools.beads_create, monkeypatch, tmp_path, {"title": "Fix"})

    failure = json.loads(result)
    assert failure["isError"] is True and failure["code"] == "tracker_busy"
    [operation] = _journal(tmp_path)
    assert tracker_operations.lookup(tmp_path, operation_id=operation["operation_id"])["committed"] is False


# -- close --------------------------------------------------------------------

async def test_a_lost_close_is_confirmed_by_reading_the_task_back(monkeypatch, tmp_path):
    written = ["?"]

    def bd(args, **kwargs):
        if args[0] == "close":
            written[0] = [arg for arg in args if arg.startswith("--reason=")][0].split("=", 1)[1]
            return timed_out()
        return ok([{"id": "p-1", "status": "closed", "close_reason": written[0]}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = await invoke(beads_tools.beads_close, monkeypatch, tmp_path, {"bead_id": "p-1", "reason": "done"})

    answer = json.loads(result)
    assert answer["status"] == "closed" and answer["reconciled"] is True
    assert "[grid-op:" in written[0]


async def test_a_lost_close_the_tracker_does_not_show_is_unknown(monkeypatch, tmp_path):
    def bd(args, **kwargs):
        if args[0] == "close":
            return timed_out()
        return ok([{"id": "p-1", "status": "open"}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = await invoke(beads_tools.beads_close, monkeypatch, tmp_path, {"bead_id": "p-1", "reason": "done"})

    failure = json.loads(result)
    assert failure["isError"] is True and failure["code"] == "tracker_unknown"


async def test_the_same_close_request_answers_once(monkeypatch, tmp_path):
    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        return ok({"id": "p-1", "status": "closed"})

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    arguments = {"bead_id": "p-1", "reason": "done", "request_id": "close-1"}
    await invoke(beads_tools.beads_close, monkeypatch, tmp_path, arguments)
    await invoke(beads_tools.beads_close, monkeypatch, tmp_path, arguments)

    assert len(calls) == 1


# -- paths --------------------------------------------------------------------

def test_an_explicit_path_may_not_switch_the_bound_tracker(monkeypatch, tmp_path):
    import utils.path_utils

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared"
    (shared / ".beads").mkdir(parents=True)
    other = workspace / "other"
    (other / ".beads").mkdir(parents=True)
    factory = SimpleNamespace(
        container_id=None,
        tracker_directory=str(shared),
        config=SimpleNamespace(get_working_directory=lambda: str(workspace)),
    )
    context = SimpleNamespace(context=SimpleNamespace(factory=None, container_id=None))

    with utils.path_utils.factory_path_context(factory):
        # The bound tracker, by another spelling: the same database.
        assert beads_tools._resolve_directory(context, ".") == str(shared)
        assert beads_tools._resolve_directory(context, "other/../") == str(shared)
        # A tracker of its own inside the workspace is a different database.
        try:
            beads_tools._resolve_directory(context, "other")
        except ValueError as refused:
            assert "may not switch" in str(refused)
        else:  # pragma: no cover - the refusal is the point
            raise AssertionError("a second tracker was silently accepted")


def _journal(directory):
    path = tracker_operations.journal_path(directory)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM operations ORDER BY rowid")]
    finally:
        connection.close()


# -- typed conflict and reconciliation ---------------------------------------

async def test_a_request_id_reused_for_a_different_create_is_a_typed_conflict(monkeypatch, tmp_path):
    def bd(args, **kwargs):
        return ok({"id": "p-1", "title": "Fix", "status": "open"})

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    await invoke(beads_tools.beads_create, monkeypatch, tmp_path, {"title": "Fix", "request_id": "same"})
    result = await invoke(beads_tools.beads_create, monkeypatch, tmp_path, {"title": "Something else", "request_id": "same"})

    failure = json.loads(result)
    assert failure["isError"] is True and failure["code"] == "tracker_request_conflict"
    assert failure["retryable"] is False


async def test_a_repeat_after_an_unknown_is_reconciled_not_repeated(monkeypatch, tmp_path):
    created = ["?"]
    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        if args[0] == "create":
            created[0] = args[args.index("--external-ref") + 1]
            return timed_out()
        # The first lookup cannot see it yet: the write is unknown.
        return ok([] if len(calls) == 2 else [{"id": "p-9", "title": "Fix", "description": "",
                                               "priority": 1, "issue_type": "task",
                                               "external_ref": created[0]}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    arguments = {"title": "Fix", "request_id": "unknown-1"}
    first = json.loads(await invoke(beads_tools.beads_create, monkeypatch, tmp_path, arguments))
    assert first["isError"] is True and first["committed"] is None

    # The same request is repeated: the marker is there now, so the journal is
    # upgraded instead of writing a second bead.
    second = json.loads(await invoke(beads_tools.beads_create, monkeypatch, tmp_path, arguments))
    assert second["id"] == "p-9" and second["committed"] is True and second["duplicate"] is True
    assert len([call for call in calls if call[0] == "create"]) == 1


async def test_an_update_is_not_proven_by_a_state_that_already_matched(monkeypatch, tmp_path):
    def bd(args, **kwargs):
        if args[0] == "update":
            return timed_out()
        # The task already reads exactly as the update intended - and carries
        # no marker of this operation.
        return ok([{"id": "p-1", "status": "in_progress", "assignee": "grid:me"}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = await invoke(
        beads_tools.beads_update, monkeypatch, tmp_path, {"bead_id": "p-1", "status": "in_progress"}
    )

    failure = json.loads(result)
    assert failure["isError"] is True and failure["code"] == "tracker_unknown"


async def test_an_update_is_proven_by_the_marker_it_wrote(monkeypatch, tmp_path):
    written = ["?"]

    def bd(args, **kwargs):
        if args[0] == "update":
            written[0] = [arg for arg in args if arg.startswith("--set-metadata=")][0].split("=", 1)[1]
            return timed_out()
        operation_id = written[0].split("grid_op=", 1)[1]
        return ok([{"id": "p-1", "status": "in_progress", "assignee": "grid:me",
                    "metadata": {"grid_op": operation_id}}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = await invoke(
        beads_tools.beads_update, monkeypatch, tmp_path, {"bead_id": "p-1", "status": "in_progress"}
    )

    answer = json.loads(result)
    assert answer["status"] == "in_progress" and answer["reconciled"] is True


async def test_a_closed_task_without_this_operations_reason_is_unknown(monkeypatch, tmp_path):
    def bd(args, **kwargs):
        if args[0] == "close":
            return timed_out()
        return ok([{"id": "p-1", "status": "closed", "close_reason": "closed by someone else"}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = await invoke(beads_tools.beads_close, monkeypatch, tmp_path, {"bead_id": "p-1", "reason": "done"})

    failure = json.loads(result)
    assert failure["isError"] is True and failure["code"] == "tracker_unknown"


# -- whose request it is, and whose write it was ------------------------------

def actor_context(user, run_id=None):
    """A run of one user: the actor namespaces the request, the run owns writes."""
    raw = SimpleNamespace(factory=None, container_id=None, user_id=user)
    if run_id is not None:
        raw.run_id = run_id
    return ToolContext(context=raw, tool_name="beads", tool_call_id="c1", tool_arguments="{}")


async def invoke_as(tool, monkeypatch, directory, arguments, context):
    monkeypatch.chdir(directory)
    (directory / ".beads").mkdir(exist_ok=True)
    return await tool.on_invoke_tool(context, json.dumps(arguments))


async def test_two_users_naming_one_request_do_not_replay_each_other(monkeypatch, tmp_path):
    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        return ok({"id": f"p-{len(calls)}", "title": "Fix", "priority": 1, "issue_type": "task"})

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    arguments = {"title": "Fix", "request_id": "retry-1"}
    alice = json.loads(await invoke_as(beads_tools.beads_create, monkeypatch, tmp_path, arguments,
                                       actor_context("alice")))
    bob = json.loads(await invoke_as(beads_tools.beads_create, monkeypatch, tmp_path, arguments,
                                     actor_context("bob")))

    assert alice["id"] == "p-1" and bob["id"] == "p-2"
    assert "duplicate" not in bob
    assert len([call for call in calls if call[0] == "create"]) == 2
    [a, b] = _journal(tmp_path)
    assert {a["actor"], b["actor"]} == {"grid:alice", "grid:bob"}


async def test_a_marked_task_with_a_different_payload_is_not_a_commit(monkeypatch, tmp_path):
    reference = ["?"]

    def bd(args, **kwargs):
        if args[0] == "create":
            reference[0] = args[args.index("--external-ref") + 1]
            return timed_out()
        # The marker is ours, the task is not what this create asked for.
        return ok([{"id": "p-1", "title": "Somebody else's task", "description": "",
                    "priority": 1, "issue_type": "task", "external_ref": reference[0]}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = json.loads(await invoke(beads_tools.beads_create, monkeypatch, tmp_path, {"title": "Fix"}))

    assert result["isError"] is True and result["committed"] is None
    assert result["code"] == "tracker_unknown"
    [operation] = _journal(tmp_path)
    view = tracker_operations.lookup(tmp_path, operation_id=operation["operation_id"])
    assert view["state"] == tracker_operations.UNKNOWN


async def test_a_close_with_our_marker_but_other_words_is_not_a_commit(monkeypatch, tmp_path):
    written = ["?"]

    def bd(args, **kwargs):
        if args[0] == "close":
            written[0] = [arg for arg in args if arg.startswith("--reason=")][0].split("=", 1)[1]
            return timed_out()
        # Same operation marker, but the reason is not the one this call gave.
        stamp = written[0][written[0].index("[grid-op:"):]
        return ok([{"id": "p-1", "status": "closed", "close_reason": f"closed by hand {stamp}"}])

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = await invoke(beads_tools.beads_close, monkeypatch, tmp_path, {"bead_id": "p-1", "reason": "done"})

    failure = json.loads(result)
    assert failure["isError"] is True and failure["code"] == "tracker_unknown"
    assert failure["committed"] is None


async def test_a_repeat_of_a_claim_does_not_hand_ownership_to_another_run(monkeypatch, tmp_path):
    from core import tracker_lifecycle

    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        return ok({"id": "p-1", "status": "in_progress", "assignee": "grid:alice"})

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    arguments = {"bead_id": "p-1", "claim": True, "request_id": "claim-1"}
    first = json.loads(await invoke_as(beads_tools.beads_update, monkeypatch, tmp_path, arguments,
                                       actor_context("alice", "run-a")))
    assert first["status"] == "in_progress"
    assert tracker_lifecycle.lookup(str(tmp_path), "p-1")["owner_run_id"] == "run-a"

    # The same user's next run repeats the request: it gets the answer back,
    # but the claim stays the run that made it - no write, no takeover.
    await invoke_as(beads_tools.beads_update, monkeypatch, tmp_path, arguments,
                    actor_context("alice", "run-b"))

    view = tracker_lifecycle.lookup(str(tmp_path), "p-1")
    assert view["owner_run_id"] == "run-a" and view["state"] == tracker_lifecycle.CLAIMED
    assert len([call for call in calls if call[0] == "update"]) == 1
    assert {entry["run_id"] for entry in tracker_lifecycle.checkpoints(str(tmp_path), "p-1")} == {"run-a"}


# -- against a real tracker ---------------------------------------------------

def _require_bd():
    if shutil.which("bd") is None:
        pytest.skip("bd is not installed")


async def _real_tracker(monkeypatch, tmp_path):
    """A genuine bd tracker in *tmp_path*, driven through the real gateway."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beads").mkdir(exist_ok=True)
    answer = await beads_tools.beads_init.on_invoke_tool(context_in(tmp_path), json.dumps({"directory": "."}))
    assert "❌" not in answer, answer


async def test_a_real_tracker_proves_a_lost_create_by_its_marked_task(monkeypatch, tmp_path):
    _require_bd()
    await _real_tracker(monkeypatch, tmp_path)
    real = beads_tools._run_bd_command

    def bd(args, **kwargs):
        result = real(args, **kwargs)
        # The write reaches the tracker; its answer is lost on the way back.
        return timed_out() if args[0] == "create" else result

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    result = json.loads(await beads_tools.beads_create.on_invoke_tool(
        context_in(tmp_path), json.dumps({"title": "Real task", "request_id": "real-1"})
    ))

    assert result["committed"] is True and result["reconciled"] is True
    # The task the tracker really holds is the one this request asked for.
    shown = real(["list", "--json", "--limit", "0", "--all"], cwd=str(tmp_path))
    [task] = shown["data"]
    assert task["id"] == result["id"] and task["title"] == "Real task"


async def test_a_real_tracker_plan_is_reconciled_by_verifying_its_graph(monkeypatch, tmp_path):
    _require_bd()
    await _real_tracker(monkeypatch, tmp_path)
    real = beads_tools._run_bd_command

    def bd(args, **kwargs):
        result = real(args, **kwargs)
        return timed_out() if args[:2] == ["create", "--graph"] else result

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    tasks = [
        {"key": "a", "title": "A", "description": "do a"},
        {"key": "b", "title": "B", "description": "do b", "depends_on": ["a"]},
    ]
    result = json.loads(await beads_tools.beads_plan.on_invoke_tool(
        context_in(tmp_path), json.dumps({"tasks": tasks, "request_id": "real-plan"})
    ))

    assert result["committed"] is True and result["reconciled"] is True
    assert set(result["ids"]) == {"a", "b"}
    listed = real(["list", "--json", "--limit", "0", "--all"], cwd=str(tmp_path))["data"]
    [task_b] = [task for task in listed if task["id"] == result["ids"]["b"]]
    assert [dep.get("depends_on_id") for dep in task_b["dependencies"]] == [result["ids"]["a"]]

    # Repeating the request asks the journal, not the tracker.
    again = json.loads(await beads_tools.beads_plan.on_invoke_tool(
        context_in(tmp_path), json.dumps({"tasks": tasks, "request_id": "real-plan"})
    ))
    assert again["duplicate"] is True and again["operation_id"] == result["operation_id"]
    assert len(real(["list", "--json", "--limit", "0", "--all"], cwd=str(tmp_path))["data"]) == 2


async def test_a_real_marker_with_other_content_is_not_a_commit(monkeypatch, tmp_path):
    _require_bd()
    await _real_tracker(monkeypatch, tmp_path)
    real = beads_tools._run_bd_command

    # A write under this operation's name that is not this request's task.
    started = tracker_operations.begin(
        str(tmp_path), "create", request_id="clash", actor="grid",
        detail={"title": "Right", "priority": 1, "type": "task", "description": "",
                "acceptance": "", "assignee": "", "depends_on": []},
    )
    made = real(
        ["create", "--json", "--priority", "1", "--type", "task",
         "--external-ref", tracker_operations.marker(started.operation_id), "--", "Wrong"],
        cwd=str(tmp_path),
    )
    assert made["success"]

    monkeypatch.setattr(beads_tools, "_run_bd_command", real)
    result = json.loads(await beads_tools.beads_create.on_invoke_tool(
        context_in(tmp_path), json.dumps({"title": "Right", "request_id": "clash"})
    ))

    assert result["isError"] is True and result["committed"] is None


# -- the plain service API the UI backend will call --------------------------

async def test_the_service_coroutines_are_the_tools_own_implementation(monkeypatch, tmp_path):
    from core import tracker_service

    calls = []

    def bd(args, **kwargs):
        calls.append(args)
        if args[:2] == ["create", "--graph"]:
            return ok({"ids": {"a": "p-1", "b": "p-2"}})
        if args[0] == "ready":
            return ok([{"id": "p-1"}, {"id": "p-2"}])
        return ok({"id": "p-1", "title": "Fix", "priority": 1, "issue_type": "task"})

    monkeypatch.setattr(beads_tools, "_run_bd_command", bd)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beads").mkdir()

    receipt = await tracker_service.create_bead(
        context=context_in(tmp_path), title="Fix", request_id="svc-1"
    )
    assert not receipt.is_error and receipt.committed is True
    assert receipt.payload["id"] == "p-1" and receipt.operation_id

    second = await tracker_service.create_bead(
        context=context_in(tmp_path), title="Fix", request_id="svc-1"
    )
    assert second.payload["duplicate"] is True and second.operation_id == receipt.operation_id
    assert len([call for call in calls if call[0] == "create" and "--graph" not in call]) == 1

    planned = await tracker_service.plan(
        context=context_in(tmp_path),
        tasks=[
            {"key": "a", "title": "A", "description": "do a"},
            {"key": "b", "title": "B", "description": "do b", "depends_on": ["a"]},
        ],
        request_id="svc-plan",
    )
    assert planned.committed is True and planned.payload["ids"] == {"a": "p-1", "b": "p-2"}
    # The tool wrapper and the service are one implementation, not two.
    assert not hasattr(tracker_service, "on_invoke_tool")
