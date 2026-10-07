"""The plan card: tracker calls of a turn kept as one live ``plan`` step."""

import json

from tests.test_web_trace import Harness, tool_called, tool_output
from web_chat.plan_board import PlanBoard
from web_chat.trace import StepKind, TraceRecorder

PLAN_ARGS = {
    "tasks": [
        {
            "key": "research", "title": "Research providers",
            "description": "find", "acceptance": "providers listed",
        },
        {"key": "compare", "title": "Compare", "description": "compare", "depends_on": ["research"]},
        {"key": "docs", "title": "Read docs", "description": "read"},
    ]
}
PLAN_RESULT = {
    "ids": {"research": "p-1", "compare": "p-2", "docs": "p-3"},
    "ready": [{"key": "research", "id": "p-1"}, {"key": "docs", "id": "p-3"}],
    "waiting": [{"key": "compare", "id": "p-2", "waits_for": ["research"]}],
}


def plan_steps(harness):
    return [step for step in harness.steps() if step["kind"] == StepKind.PLAN.value]


def states(step):
    return {task["key"] or task["id"]: task["state"] for task in step["plan"]["tasks"]}


def planned(harness):
    harness.feed(
        tool_called("beads_plan", "c1", json.dumps(PLAN_ARGS)),
        tool_output("c1", json.dumps(PLAN_RESULT)),
    )


def test_a_created_plan_becomes_one_step_with_every_task():
    harness = Harness()
    planned(harness)

    [step] = plan_steps(harness)
    assert step["title"] == "Plan · 0/3 done"
    assert [task["title"] for task in step["plan"]["tasks"]] == ["Research providers", "Compare", "Read docs"]
    assert states(step) == {"research": "ready", "compare": "waiting", "docs": "ready"}
    compare = step["plan"]["tasks"][1]
    assert compare["depends_on"] == ["p-1"] and compare["waits_for"] == ["research"]
    research = step["plan"]["tasks"][0]
    assert research["description"] == "find" and research["acceptance"] == "providers listed"
    # Details no call brought stay at their honest missing state.
    assert compare["acceptance"] == "" and research["notes"] == ""
    assert research["close_reason"] is None and research["report"] is None
    assert step["parent_id"] is None


def test_the_same_step_follows_the_tasks_through_their_agents():
    harness = Harness()
    planned(harness)
    # The coordinator launches an agent that claims research through init_tools.
    init = [{"name": "beads_update", "parameters": {"bead_id": "p-1", "claim": True}}]
    harness.feed(tool_called("orchestrate", "c2", json.dumps({"task": "p-1", "init_tools": json.dumps(init)})))
    assert states(plan_steps(harness)[0])["research"] == "in_progress"

    # Inside the agent's own block, it closes the task.
    agent = harness.observer.nested("Executor", call_id="c2")
    agent.handle_event(tool_called("beads_close", "c3", json.dumps({"bead_id": "p-1", "reason": "done"})))
    agent.handle_event(tool_output("c3", json.dumps({"id": "p-1", "status": "closed"})))

    [step] = plan_steps(harness)
    assert states(step) == {"research": "closed", "compare": "ready", "docs": "ready"}
    assert step["title"] == "Plan · 1/3 done"
    assert step["plan"]["tasks"][0]["close_reason"] == "done"


def test_a_failed_tracker_call_changes_nothing():
    harness = Harness()
    planned(harness)
    harness.feed(
        tool_called("beads_close", "c2", json.dumps({"bead_id": "p-1"})),
        tool_output("c2", "❌ Error closing bead p-1: no such issue"),
    )
    assert states(plan_steps(harness)[0])["research"] == "ready"


def test_updates_write_the_details_they_bring_and_notes_add_up():
    harness = Harness()
    planned(harness)
    harness.feed(
        tool_called("beads_update", "c2", json.dumps({
            "bead_id": "p-2", "claim": True, "description": "compare them", "notes": "started",
        })),
        tool_output("c2", json.dumps({"id": "p-2", "status": "in_progress"})),
    )
    step = plan_steps(harness)[-1]
    compare = step["plan"]["tasks"][1]
    assert (compare["state"], compare["description"], compare["notes"]) == ("in_progress", "compare them", "started")
    # A later note adds to the earlier one, as the tracker keeps them.
    harness.feed(
        tool_called("beads_update", "c3", json.dumps({"bead_id": "p-2", "notes": "half done"})),
        tool_output("c3", json.dumps({"id": "p-2"})),
    )
    compare = plan_steps(harness)[-1]["plan"]["tasks"][1]
    assert compare["notes"] == "started\nhalf done"
    # A close stores its reason with the closed task.
    harness.feed(
        tool_called("beads_close", "c4", json.dumps({"bead_id": "p-2", "reason": "table written"})),
        tool_output("c4", json.dumps({"id": "p-2", "status": "closed"})),
    )
    compare = plan_steps(harness)[-1]["plan"]["tasks"][1]
    assert (compare["state"], compare["close_reason"]) == ("closed", "table written")


def test_a_failed_update_or_close_keeps_the_details_it_had():
    harness = Harness()
    planned(harness)
    harness.feed(
        tool_called("beads_close", "c2", json.dumps({"bead_id": "p-1", "reason": "done"})),
        tool_output("c2", "❌ Error closing bead p-1: locked"),
        tool_called("beads_update", "c3", json.dumps({"bead_id": "p-1", "notes": "note", "description": "changed"})),
        tool_output("c3", json.dumps({"error": "no such issue"})),
    )
    research = plan_steps(harness)[0]["plan"]["tasks"][0]
    # A refused close is no confirmed success: nothing of it lands on the board.
    assert research["state"] == "ready" and research["close_reason"] is None
    assert research["description"] == "find" and research["notes"] == ""


def test_an_orchestrate_report_lands_on_the_task_it_claimed():
    harness = Harness()
    planned(harness)
    init = [{"name": "beads_update", "parameters": {"bead_id": "p-1", "claim": True}}]
    harness.feed(
        tool_called("orchestrate", "c2", json.dumps({"task": "Do the research", "init_tools": json.dumps(init)})),
        tool_output("c2", json.dumps({
            "status": "completed", "seconds": 12.5, "model_calls": 3, "model_key": "gpt",
            "tokens": {"input": 100, "output": 40}, "cost_usd": 0.02, "final": "All providers listed.",
        })),
    )
    research = plan_steps(harness)[-1]["plan"]["tasks"][0]
    assert research["state"] == "in_progress"
    # The report keeps what the run did and cost, and the text it answered with.
    assert research["report"] == {
        "status": "completed", "seconds": 12.5, "model_calls": 3, "model_key": "gpt",
        "tokens": {"input": 100, "output": 40}, "cost_usd": 0.02,
        "final": "All providers listed.",
    }
    assert all(task["report"] is None for task in plan_steps(harness)[-1]["plan"]["tasks"][1:])


def test_a_run_that_failed_or_timed_out_keeps_no_final_text():
    harness = Harness()
    planned(harness)
    init = [{"name": "beads_update", "parameters": {"bead_id": "p-1", "claim": True}}]
    harness.feed(
        tool_called("orchestrate", "c2", json.dumps({"task": "Do the research", "init_tools": json.dumps(init)})),
        tool_output("c2", json.dumps({
            "status": "timeout", "seconds": 60.0, "model_calls": 2,
            "final": "[Executor stopped on a timeout] It made 4 tool calls before stopping.",
        })),
    )
    research = plan_steps(harness)[-1]["plan"]["tasks"][0]
    # What the run cost and why it stopped land; the partial text it managed
    # before stopping is not its result, so it does not pose as one.
    assert research["report"] == {"status": "timeout", "seconds": 60.0, "model_calls": 2}


def test_one_report_of_a_run_over_several_tasks_lands_on_none_of_them():
    harness = Harness()
    planned(harness)
    init = [
        {"name": "beads_update", "parameters": {"bead_id": "p-1", "claim": True}},
        {"name": "beads_update", "parameters": {"bead_id": "p-2", "claim": True}},
    ]
    harness.feed(
        tool_called("orchestrate", "c2", json.dumps({"task": "Research and compare", "init_tools": json.dumps(init)})),
        tool_output("c2", json.dumps({"status": "completed", "seconds": 9.0, "final": "Both done."})),
    )
    [step] = plan_steps(harness)
    # One common report of a run over several tasks is no single task's own:
    # it lands on none of them rather than on all.
    assert all(task["report"] is None for task in step["plan"]["tasks"])
    assert states(step)["research"] == "in_progress" and states(step)["compare"] == "in_progress"


def test_a_report_without_an_explicit_existing_task_id_lands_on_no_task():
    harness = Harness()
    planned(harness)
    # The free-text task names an id, but no init_tools claim one: ids are
    # never read out of text, so the report lands on nothing.
    harness.feed(
        tool_called("orchestrate", "c2", json.dumps({"task": "Work on p-1 (research)"})),
        tool_output("c2", json.dumps({"status": "completed", "seconds": 1.0})),
    )
    [step] = plan_steps(harness)
    assert all(task["report"] is None for task in step["plan"]["tasks"])


def test_a_failed_orchestrate_leaves_no_report():
    harness = Harness()
    planned(harness)
    init = [{"name": "beads_update", "parameters": {"bead_id": "p-1", "claim": True}}]
    harness.feed(
        tool_called("orchestrate", "c2", json.dumps({"task": "Do the research", "init_tools": json.dumps(init)})),
        tool_output("c2", "❌ orchestrate: no access to AgentFactory (expected context.context.factory)."),
    )
    research = plan_steps(harness)[-1]["plan"]["tasks"][0]
    assert research["report"] is None and research["state"] == "in_progress"


def test_a_turn_without_tracker_calls_shows_no_plan():
    harness = Harness()
    harness.feed(tool_called("file_read", "c1", "{}"), tool_output("c1", "text"))
    assert plan_steps(harness) == []


def test_a_created_task_keeps_the_description_it_was_given():
    harness = Harness()
    harness.feed(
        tool_called("beads_create", "c1", json.dumps({"title": "Fix the build", "description": "make it green"})),
        tool_output("c1", "✓ Created issue: grid-a1b — Fix the build\n  Priority: P1\n  Status: open\n"),
    )
    [task] = plan_steps(harness)[-1]["plan"]["tasks"]
    assert task["description"] == "make it green"
    assert task["acceptance"] == "" and task["close_reason"] is None and task["report"] is None


def test_a_new_turn_continues_the_plan_its_conversation_left():
    first = Harness()
    planned(first)
    stored = [{"role": "assistant", "metadata": {"trace": first.steps()}}]

    events = []
    board = PlanBoard.continuing(TraceRecorder(events.append), stored)
    # Nothing shows until a task of the plan changes in this turn.
    assert events == []
    board.finished("beads_close", {"bead_id": "p-1"}, json.dumps({"id": "p-1"}))
    step = events[-1]["step"]
    assert step["kind"] == "plan" and step["title"] == "Plan · 1/3 done"
    assert states(step)["compare"] == "ready"


def test_a_new_turn_restores_the_details_the_plan_left():
    first = Harness()
    planned(first)
    init = [{"name": "beads_update", "parameters": {"bead_id": "p-1", "claim": True}}]
    first.feed(
        tool_called("orchestrate", "c2", json.dumps({"task": "Do the research", "init_tools": json.dumps(init)})),
        tool_output("c2", json.dumps({"status": "completed", "seconds": 5.0, "final": "Providers listed."})),
        tool_called("beads_update", "c3", json.dumps({"bead_id": "p-1", "notes": "noted while working"})),
        tool_output("c3", json.dumps({"id": "p-1"})),
        tool_called("beads_close", "c4", json.dumps({"bead_id": "p-1", "reason": "listed"})),
        tool_output("c4", json.dumps({"id": "p-1", "status": "closed"})),
    )
    stored = [{"role": "assistant", "metadata": {"trace": first.steps()}}]

    events = []
    board = PlanBoard.continuing(TraceRecorder(events.append), stored)
    assert events == []
    board.finished("beads_close", {"bead_id": "p-3"}, json.dumps({"id": "p-3"}))
    step = [event["step"] for event in events if event["step"]["kind"] == "plan"][-1]
    research = step["plan"]["tasks"][0]
    assert research["close_reason"] == "listed"
    assert research["notes"] == "noted while working"
    assert research["report"] == {"status": "completed", "seconds": 5.0, "final": "Providers listed."}
    assert research["description"] == "find" and research["acceptance"] == "providers listed"


def test_an_old_plan_step_without_the_details_continues_anyway():
    # A plan stored before the board kept details: its tasks carry only the
    # fields the old board wrote, and every missing one is at its default.
    stored = [{
        "role": "assistant",
        "metadata": {"trace": [{
            "kind": "plan",
            "plan": {"tasks": [{
                "id": "old-1", "key": "", "title": "Old task", "status": "open",
                "depends_on": [], "state": "ready", "waits_for": [],
            }]},
        }]},
    }]

    events = []
    board = PlanBoard.continuing(TraceRecorder(events.append), stored)
    board.finished("beads_close", {"bead_id": "old-1", "reason": "done"}, json.dumps({"id": "old-1"}))
    [task] = events[-1]["step"]["plan"]["tasks"]
    assert (task["state"], task["close_reason"]) == ("closed", "done")
    assert task["description"] == "" and task["acceptance"] == "" and task["notes"] == ""
    assert task["report"] is None


def test_tasks_created_one_by_one_and_linked_join_the_plan():
    harness = Harness()
    planned(harness)
    harness.feed(
        tool_called("beads_create", "c2", json.dumps({"title": "Verify"})),
        tool_output("c2", json.dumps({"id": "p-4", "title": "Verify", "status": "open"})),
        tool_called("beads_dep", "c3", json.dumps({"action": "add", "child_id": "p-4", "parent_id": "p-2"})),
        tool_output("c3", "added"),
    )
    [step] = plan_steps(harness)
    verify = step["plan"]["tasks"][-1]
    assert (verify["id"], verify["state"], verify["waits_for"]) == ("p-4", "waiting", ["compare"])


def test_tasks_created_by_beads_create_show_without_a_plan_call():
    # beads_create returns what `bd create` prints, not JSON: a system that
    # plans with it alone (engineering) still gets the board.
    harness = Harness()
    harness.feed(
        tool_called("beads_create", "c1", json.dumps({"title": "Fix the build"})),
        tool_output("c1", "✓ Created issue: grid-a1b — Fix the build\n  Priority: P1\n  Status: open\n"),
        tool_called("beads_update", "c2", json.dumps({"bead_id": "grid-a1b", "claim": True})),
        tool_output("c2", "✓ Updated issue: grid-a1b"),
    )
    [step] = plan_steps(harness)
    [task] = step["plan"]["tasks"]
    assert (task["id"], task["title"], task["state"]) == ("grid-a1b", "Fix the build", "in_progress")


def test_a_beads_create_that_made_nothing_changes_nothing():
    harness = Harness()
    harness.feed(
        tool_called("beads_create", "c1", json.dumps({"title": "X"})),
        tool_output("c1", "❌ Error creating bead: Error response from daemon: No such container: 1a2b"),
    )
    assert plan_steps(harness) == []
