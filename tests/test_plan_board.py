"""The plan card: tracker calls of a turn kept as one live ``plan`` step."""

import json

from tests.test_web_trace import Harness, tool_called, tool_output
from web_chat.plan_board import PlanBoard
from web_chat.trace import StepKind, TraceRecorder

PLAN_ARGS = {
    "tasks": [
        {"key": "research", "title": "Research providers", "description": "find"},
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


def test_a_failed_tracker_call_changes_nothing():
    harness = Harness()
    planned(harness)
    harness.feed(
        tool_called("beads_close", "c2", json.dumps({"bead_id": "p-1"})),
        tool_output("c2", "❌ Error closing bead p-1: no such issue"),
    )
    assert states(plan_steps(harness)[0])["research"] == "ready"


def test_a_turn_without_tracker_calls_shows_no_plan():
    harness = Harness()
    harness.feed(tool_called("file_read", "c1", "{}"), tool_output("c1", "text"))
    assert plan_steps(harness) == []


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
