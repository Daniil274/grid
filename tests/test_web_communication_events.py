"""The typed public communication of a turn: ids, privacy, replay and the wire.

`web_chat.observer` turns a turn's trace into typed events - a **message** is
what the agent says to people, a **status** is a short line around its words,
a **report** is internal work and never leaves the server. The same events are
derived live (from step frames), stored beside the answer and replayed from the
history API; this module checks that the three agree, that the privacy gate
holds, and that the live and stored twins carry the same id.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

from core.context import ContextManager
from web_chat.observer import (
    EVENTS_KEY,
    EventKind,
    EventLog,
    EventVisibility,
    event_from_step,
    events_from_trace,
    message_events,
    public_message_events,
    stable_event_id,
)
from web_chat.session import chat_session
from web_chat.turns import TurnBoard


def step(kind, id="s1", **fields):
    """One timeline step as web_chat/trace.py writes it into a trace."""
    base = {
        "id": id,
        "kind": kind,
        "title": "",
        "subtitle": "",
        "body": "",
        "status": "done",
        "at_ms": 1,
        "parent_id": None,
    }
    base.update(fields)
    return base


# -- ids and the gate -------------------------------------------------------


def test_an_event_id_is_stable_for_the_same_origin_and_text():
    first = stable_event_id(EventKind.MESSAGE, "s1", "Let me check the diff.")
    again = stable_event_id("message", "s1", "Let me check the diff.")
    other = stable_event_id("message", "s2", "Let me check the diff.")
    assert first == again and first != other


def test_a_public_event_is_the_only_thing_serialised_for_people():
    log = EventLog(turn_id="t1", author="coder")
    log.message("Let me check the diff.", step_id="s1")
    log.status("Read config.yaml", step_id="s2")
    log.report("thinking out loud")
    public = log.snapshot(public_only=True)
    assert [(e["kind"], e["content"]) for e in public] == [
        ("message", "Let me check the diff."),
        ("status", "Read config.yaml"),
    ]
    assert all(e["visibility"] == "public" for e in public)
    # Stored whole, the report is still there - it just never passes the gate.
    assert [e["kind"] for e in log.snapshot()] == ["message", "status", "report"]


def test_the_same_event_twice_is_recorded_once():
    log = EventLog(author="coder")
    log.message("All set.", step_id="s1")
    log.message("All set.", step_id="s1")
    assert len(log.snapshot()) == 1


def test_an_author_is_a_callable_while_the_turn_still_learns_it():
    label = {"value": ""}
    log = EventLog(author=lambda: label["value"] or "assistant")
    log.message("first")
    label["value"] = "coder"
    log.message("second")
    assert [e["author"] for e in log.snapshot()] == ["assistant", "coder"]


# -- steps to events --------------------------------------------------------


def test_top_level_narration_is_a_public_message_and_nested_narration_is_not():
    own = event_from_step(step("message", id="m1", body="Let me check the diff."))
    nested = event_from_step(
        step("message", id="m2", body="I am the sub-agent.", parent_id="a1")
    )
    assert (own.kind, own.visibility) == (EventKind.MESSAGE, EventVisibility.PUBLIC)
    assert own.content == "Let me check the diff."
    assert nested.kind == EventKind.REPORT
    assert nested.visibility == EventVisibility.INTERNAL


def test_thinking_is_internal_however_it_was_written():
    thinking = event_from_step(step("reasoning", body="the user wants the diff"))
    assert thinking.kind == EventKind.REPORT
    assert not thinking.is_public


def test_a_tool_status_carries_its_title_never_its_payload():
    event = event_from_step(
        step("tool", title="Read config.yaml", subtitle="", body="SECRET_TOKEN=abc")
    )
    assert event.kind == EventKind.STATUS
    assert event.is_public
    assert event.content == "Read config.yaml"
    assert "SECRET_TOKEN" not in event.content


def test_a_sub_agent_tool_and_its_output_stay_internal():
    called = event_from_step(
        step("tool", id="t1", title="Run the tests", parent_id="a1", body="SECRET")
    )
    agent = event_from_step(
        step("agent", id="a1", title="Researcher", body="SECRET report")
    )
    assert called.kind == EventKind.REPORT and not called.is_public
    assert agent.kind == EventKind.REPORT and not agent.is_public


def test_a_trace_becomes_events_in_order_and_silent_steps_vanish():
    steps = [
        step("prepare", id="p1", title="Preparing the runtime"),
        step("message", id="m1", body="Let me look."),
        step("tool", id="t1", title="Read config.yaml"),
    ]
    events = events_from_trace(steps)
    assert [e.step_id for e in events] == ["p1", "m1", "t1"]


# -- stored messages --------------------------------------------------------


def message(role="assistant", text="", metadata=None):
    return SimpleNamespace(role=role, content=text, metadata=metadata or {}, timestamp="2026-01-01T00:00:00Z")


def test_stored_events_are_authoritative_over_the_trace():
    stored = EventLog(turn_id="t1", author="coder")
    stored.message("Let me check the diff.", step_id="s1")
    msg = message(
        "assistant", "Done.",
        metadata={
            EVENTS_KEY: stored.snapshot(public_only=True),
            "trace": [step("message", id="s9", body="a different sentence")],
        },
    )
    events = message_events(msg)
    # The stored list is the authority: the trace sentence it disagrees with
    # is not derived, and the answer is the message's own body - the UI drops
    # a final event that repeats it, so it is not appended here either.
    assert [e.content for e in events if e.kind == EventKind.MESSAGE] == [
        "Let me check the diff.",
    ]


def test_an_explicit_empty_list_means_no_events_rather_than_a_derivation():
    msg = message("assistant", "Done.", metadata={EVENTS_KEY: [], "trace": [step("message", body="old")]})
    assert message_events(msg) == []


def test_a_legacy_message_replays_from_its_trace():
    msg = message(
        "assistant", "All set.",
        metadata={
            "agent_name": "coder",
            "trace": [
                step("message", id="m1", body="Let me check the diff."),
                step("reasoning", id="r1", body="internal"),
            ],
        },
    )
    public = public_message_events(msg)
    assert [(e["kind"], e["content"]) for e in public] == [
        ("message", "Let me check the diff."),
        ("message", "All set."),
    ]
    assert all(e["visibility"] == "public" for e in public)
    assert all(e["author"] == "coder" for e in public)
    assert all(e["id"] for e in public)


def test_a_sub_agent_report_message_never_reaches_people():
    msg = message("assistant", "SECRET sub-agent answer", metadata={"kind": "tool_result"})
    assert public_message_events(msg) == []


def test_a_stored_event_and_its_live_twin_share_an_id():
    live = event_from_step(step("message", id="m1", body="Let me check the diff."))
    msg = message(
        "assistant", "Done.",
        metadata={EVENTS_KEY: [live.as_dict()], "trace": [step("message", id="m1", body="Let me check the diff.")]},
    )
    replayed = message_events(msg)[0]
    assert replayed.id == live.id


# -- the live wire and the stored record ------------------------------------


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def accept(self):
        pass

    async def receive_text(self):
        value = await self.incoming.get()
        if value is None:
            raise WebSocketDisconnect()
        return value

    async def send_json(self, value):
        await self.outgoing.put(value)


def space_for(run):
    manager = ContextManager()
    manager.ensure_context("ctx")
    factory = SimpleNamespace(
        run_agent=run, request_stop=lambda context_id: False, continue_agent=None
    )

    async def resolve_turn(message, **kwargs):
        return SimpleNamespace(
            system=kwargs.get("system_key") or "s",
            agent=kwargs.get("agent_key") or "a",
            config=None,
            factory=factory,
            routed_system=False,
            routed_agent=kwargs.get("agent_key") is None,
            routed=True,
            warning="",
        )

    registry = SimpleNamespace(
        agent_label=lambda system, agent: "Coder",
        selection_is_valid=lambda system, agent: True,
        agent_issues=lambda system, agent: [],
    )
    return SimpleNamespace(
        context_manager=lambda: manager,
        workspace_path=".",
        workspace_label=".",
        user_id="test",
        admit_turn=lambda: None,
        workspace_label_of=lambda context_id: ".",
        registry_of=lambda context_id: registry,
        factory=factory,
        warm_agent=AsyncMock(),
        resolve_turn=resolve_turn,
        update_conversation_metadata=lambda *a, **kw: None,
        registry=registry,
        turns=TurnBoard(),
        deployment=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_narration_reaches_the_page_live_and_is_stored_with_the_answer():
    from agents import RawResponsesStreamEvent, RunItemStreamEvent

    async def run(**kwargs):
        observer = kwargs["stream_observer"]
        observer.handle_event(
            RawResponsesStreamEvent(
                data=SimpleNamespace(type="response.output_text.delta", delta="Let me check the diff.")
            )
        )
        observer.handle_event(
            RunItemStreamEvent(
                name="tool_called",
                item=SimpleNamespace(
                    raw_item=SimpleNamespace(
                        name="read_file", call_id="c1", arguments='{"path": "config.yaml"}', type="function_call"
                    )
                ),
            )
        )
        observer.handle_event(
            RunItemStreamEvent(
                name="tool_output",
                item=SimpleNamespace(
                    raw_item=SimpleNamespace(call_id="c1", type="function_call_output"), output="settings: {}"
                ),
            )
        )
        observer.handle_event(
            RawResponsesStreamEvent(
                data=SimpleNamespace(type="response.output_text.delta", delta="All set.")
            )
        )
        return "All set."

    space = space_for(run)
    # The turn's answer is the last assistant entry; persistence lands on it.
    space.context_manager().add_message("assistant", "All set.", {"agent_name": "Coder"}, context_id="ctx")
    socket = Socket()
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put('{"message":"hello"}')

    frames = []
    async with asyncio.timeout(2):
        while True:
            frames.append(await socket.outgoing.get())
            if frames[-1]["type"] == "done":
                break

    events = [frame["event"] for frame in frames if frame["type"] == "event"]
    messages = [e["content"] for e in events if e["kind"] == "message"]
    assert "Let me check the diff." in messages
    assert all(e["visibility"] == "public" for e in events)
    # The internal thinking of the turn is never a frame.
    assert not any("internal" == e["visibility"] for e in events)

    stored = space.context_manager().get_context_messages("ctx")["messages"][-1]["metadata"]
    assert stored[EVENTS_KEY], "the turn's public events are stored with its answer"
    assert [e["content"] for e in stored[EVENTS_KEY] if e["kind"] == "message"] == ["Let me check the diff."]

    await socket.incoming.put(None)
    await session
