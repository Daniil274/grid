"""``say_public``: one explicit way for a run - a child especially - to be heard.

The Responses API lets a provider mark a message ``commentary``, but Chat
Completions and the Claude CLI carry no such phase, so a sub-agent there had no
way to say anything to people at all. This tool is that channel and only that
channel: the text becomes a public event under the speaking run's own author,
live and in the stored turn, while the run's reasoning, output and reports stay
internal. It is a tool call, so every adapter reaches it the same way.
"""

import asyncio
import json
from types import SimpleNamespace

from core.factory.tools import ToolAssembly
from tools.message_tools import SAY_PUBLIC, say_public_tool, TOOL_EFFECTS, TOOL_ISOLATION
from utils.tool_effects import READ
from utils.tool_isolation import WORKSPACE, is_confined
from web_chat.observer import EventKind, EventLog, EventVisibility, WebStreamObserver
from web_chat.trace import TraceRecorder


class Harness:
    """A turn observer with a real public log, a trace and a live frame sink."""

    def __init__(self, turn_id="run-1"):
        self.trace = []
        self.frames = []
        self.log = EventLog(turn_id=turn_id, author="Coder")
        self.recorder = TraceRecorder(self.trace.append)
        self.observer = WebStreamObserver(
            self.recorder,
            emit_token=lambda _text: None,
            agent_label="Coder",
            emit_event=self.frames.append,
            events=self.log,
            turn_id=turn_id,
        )


def call(view, text):
    """What a tool call of ``say_public`` returns, with *view* as the run's view."""
    context = SimpleNamespace(context=SimpleNamespace(stream_observer=view))
    return json.loads(asyncio.run(say_public_tool(context, text)))


def test_a_child_is_heard_through_the_tool_whatever_its_provider():
    harness = Harness()
    child = harness.observer.nested("researcher", call_id="call-1")

    answer = call(child, "The router rejects the second route.")

    assert answer["sent"] is True and answer["author"] == "researcher"
    public = harness.observer.turn_events(public_only=True)
    assert [event["content"] for event in public] == ["The router rejects the second route."]
    event = public[0]
    # Who said it, which run it belongs to, and where in the trace it happened.
    assert event["kind"] == "message" and event["visibility"] == "public"
    assert event["id"] == answer["id"]
    assert event["author"] == "researcher" and event["author_key"] == "researcher"
    assert event["turn_id"] == "run-1"
    assert event["parent_id"] == child._parent_id
    # Live and stored are the same event, and a trace row sits under the child.
    assert harness.frames == [{"type": "event", "event": event}]
    assert any(step["id"] == event["step_id"] for step in harness.recorder.snapshot())


def test_a_top_level_run_speaks_into_its_own_log():
    harness = Harness()

    answer = call(harness.observer, "Two of the tests fail on this branch.")

    assert answer["sent"] is True and answer["author"] == "Coder"
    public = harness.observer.turn_events(public_only=True)
    assert [(event["author"], event["content"]) for event in public] == [
        ("Coder", "Two of the tests fail on this branch.")
    ]
    assert public[0]["step_id"] and harness.frames


def test_the_same_words_twice_are_two_messages():
    harness = Harness()
    first = call(harness.observer, "Still running.")
    second = call(harness.observer, "Still running.")

    assert first["id"] != second["id"]
    assert len(harness.observer.turn_events(public_only=True)) == 2


def test_a_run_with_no_public_channel_publishes_nothing():
    harness = Harness()

    assert call(None, "Nobody can see this.")["sent"] is False
    assert call(object(), "Nor this.")["sent"] is False
    assert call(harness.observer, "   ")["sent"] is False
    assert harness.observer.turn_events(public_only=True) == []
    assert harness.frames == []


def test_a_report_is_not_a_message_even_from_the_same_run():
    harness = Harness()
    assert harness.observer.record_report("thinking out loud") is not None
    assert harness.observer.turn_events(public_only=True) == []

    call(harness.observer, "A real message.")
    stored = harness.observer.turn_events(public_only=True)
    assert [event["kind"] for event in stored] == ["message"]
    assert all(event["visibility"] == EventVisibility.PUBLIC.value for event in stored)


def test_the_message_keeps_its_public_kind_when_it_is_rederived():
    harness = Harness()
    call(harness.observer, "Read the diff before you commit.")
    event = harness.observer.turn_events(public_only=True)[0]

    row = next(step for step in harness.recorder.snapshot() if step["id"] == event["step_id"])
    assert row["kind"] == EventKind.MESSAGE.value


class _Assembly(ToolAssembly):
    """Tool assembly without a factory behind it."""

    def __init__(self):
        self.config = SimpleNamespace(
            config=SimpleNamespace(
                settings=SimpleNamespace(max_tool_output_tokens=None, max_tool_output=None)
            ),
            project_tools_loader=None,
        )

    def _wrap_tool_with_policy(self, tool, tool_name, kind, effect=None):
        return tool


def test_every_agent_is_given_the_tool_once():
    assembly = _Assembly()

    tools = assembly._with_public_message_tool([])

    assert [tool.name for tool in tools] == [SAY_PUBLIC]
    # Asked twice, it stays one tool: the list is returned unchanged.
    assert assembly._with_public_message_tool(tools) is tools
    already = [SimpleNamespace(name=SAY_PUBLIC)]
    assert assembly._with_public_message_tool(already) is already


def test_the_tool_says_something_and_changes_nothing():
    assert TOOL_EFFECTS[SAY_PUBLIC].kind == READ
    assert is_confined(TOOL_ISOLATION[SAY_PUBLIC])
    assert TOOL_ISOLATION[SAY_PUBLIC] == WORKSPACE


async def test_the_assembled_tool_reaches_the_child_view_through_the_gate():
    """As the SDK calls it: the factory's wrapped tool, a child's run context."""
    from agents.tool_context import ToolContext

    from core.agent_factory import AgentFactory
    from core.factory.run_context import GridRunContext

    harness = Harness()
    child = harness.observer.nested("Executor", call_id="call-1")
    factory = AgentFactory.__new__(AgentFactory)
    factory.config = SimpleNamespace(project_tools_loader=None)
    factory.action_gate = None

    tool = factory._with_public_message_tool([])[0]
    context = ToolContext(
        context=GridRunContext(factory=None, stream_observer=child),
        tool_name=SAY_PUBLIC,
        tool_call_id="call-2",
        tool_arguments='{"text": "Checking the failing test."}',
    )
    answer = json.loads(
        await tool.on_invoke_tool(context, '{"text": "Checking the failing test."}')
    )

    assert answer["sent"] is True and answer["author"] == "Executor"
    assert [event["content"] for event in harness.observer.turn_events(public_only=True)] == [
        "Checking the failing test."
    ]
