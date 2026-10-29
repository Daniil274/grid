"""The chat's wire stays small: lean steps, no copies, glued deltas, a snapshot.

The live stream and a stored conversation follow one rule (``web_chat.trace``
``LAZY_STEP_FIELDS``): identity, labels and status always travel; the big
fields are fetched from the step endpoint. Text deltas are glued on the server,
and a socket that reconnects receives the state so far, not the frame journal.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import RunItemStreamEvent

from web_chat.observer import WebStreamObserver
from web_chat.payload import normalize_payload
from web_chat.session import AgentTurn, DELTA_FLUSH_BYTES, MAX_REPLAY_NOTES
from tests.test_run_agent import factory  # noqa: F401 - the fixture
from web_chat.trace import (
    LAZY_FIELD_BYTES,
    LAZY_STEP_FIELDS,
    StepKind,
    TraceRecorder,
    slim_step,
    slim_steps,
)
from web_chat.views import project_metadata


def tool_called(name: str, call_id: str, arguments: str) -> RunItemStreamEvent:
    return RunItemStreamEvent(
        name="tool_called",
        item=SimpleNamespace(raw_item=SimpleNamespace(
            name=name, call_id=call_id, arguments=arguments, type="function_call")),
    )


def tool_output(call_id: str, output: str) -> RunItemStreamEvent:
    return RunItemStreamEvent(
        name="tool_output",
        item=SimpleNamespace(
            raw_item=SimpleNamespace(call_id=call_id, type="function_call_output"),
            output=output,
        ),
    )


def big_output(size: int = LAZY_FIELD_BYTES * 5) -> str:
    return "line of a long tool output\n" * (size // 26 + 1)


class Recorder:
    """Collects the frames a recorder emits, as the transport would."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.recorder = TraceRecorder(self.frames.append)
        self.observer = WebStreamObserver(
            self.recorder, emit_token=lambda _: None, agent_label="Agent"
        )

    def step_frames(self) -> list[dict]:
        return [frame["step"] for frame in self.frames if frame["type"] == "step"]


class FakeManager:
    def get_context_messages(self, context_id, limit=40):
        return {"messages": []}


class FakeSession:
    def __init__(self) -> None:
        self.send_lock = asyncio.Lock()
        self.sent: list[dict] = []
        self.manager = FakeManager()
        self.context_id = "ctx"
        self.space = SimpleNamespace(limits=None)

    async def send(self, event):
        self.sent.append(event)

    async def send_unlocked(self, event):
        self.sent.append(event)

    def release(self, turn):
        pass

    def agent_label(self, system, agent):
        return f"{system}/{agent}"


async def finished_turn(tokens: int = 0, output: str | None = None) -> tuple[AgentTurn, FakeSession]:
    session = FakeSession()
    turn = AgentTurn(session, "hello", system_key=None, agent_key=None)
    if output is not None:
        turn._observer.handle_event(tool_called("bash_tool", "c1", '{"command": "ls"}'))
        turn._observer.handle_event(tool_output("c1", output))
    for _ in range(tokens):
        turn._token("word ")
    turn._queue.put_nowait({"type": "_finished"})
    await turn._consume()
    return turn, session


# -- one rule for live frames and history ---------------------------------
def test_a_live_step_frame_is_lean_and_marked_lazy():
    harness = Recorder()
    harness.observer.handle_event(tool_called("bash_tool", "c1", '{"command": "ls"}'))
    harness.observer.handle_event(tool_output("c1", big_output()))

    frames = harness.step_frames()
    opened, closed = frames[0], frames[-1]
    for frame in (opened, closed):
        # Identity, labels and status travel.
        assert {"id", "kind", "title", "status", "refs", "at_ms"} <= frame.keys()
        assert closed.get("result_payload") is None and not closed.get("body")
        assert closed["lazy"] is True
        # Small fields stay: the row needs them to draw its head.
        assert len(json.dumps(closed, ensure_ascii=False)) < LAZY_FIELD_BYTES
    assert opened["detail"] == '{\n  "command": "ls"\n}'


def test_a_small_step_ships_whole_and_keeps_its_body():
    harness = Recorder()
    harness.observer.handle_event(tool_called("read_file", "c1", '{"path": "a.py"}'))
    harness.observer.handle_event(tool_output("c1", "print(1)"))

    closed = harness.step_frames()[-1]
    assert closed["body"] == "print(1)"
    assert "lazy" not in closed
    assert "result_payload" in closed


def test_thinking_keeps_its_text_but_not_its_other_payloads():
    step = {
        "id": "s1", "kind": "reasoning", "title": "Thinking", "status": "done",
        "body": "x" * (LAZY_FIELD_BYTES * 2), "detail": "", "refs": [],
    }
    assert slim_step(step)["body"] == step["body"]  # the text *is* the step
    tool = {**step, "kind": "tool"}
    assert slim_step(tool)["lazy"] is True and "body" not in slim_step(tool)


def test_history_and_the_live_stream_share_the_same_rule():
    from web_chat import views

    assert views.slim_steps is slim_steps
    assert views.LAZY_STEP_FIELDS == LAZY_STEP_FIELDS
    stored = [{"id": "s1", "kind": "tool", "title": "read", "body": big_output()}]
    assert slim_steps(stored) == [slim_step(stored[0])]
    # A stored trace itself is untouched: only the wire view is slim.
    assert stored[0]["body"] == big_output()


def test_a_step_that_lost_nothing_is_the_same_object():
    step = {"id": "s1", "kind": "tool", "title": "ls", "body": "a b"}
    assert slim_step(step) is step


# -- no duplicate copies ---------------------------------------------------
def test_a_plain_text_payload_does_not_repeat_its_raw_text():
    payload = normalize_payload("a plain tool output")
    assert "raw_text" not in payload
    assert payload["parts"] == [{"kind": "text", "media_type": "text/plain", "data": "a plain tool output"}]
    # Structured payloads keep the exact source: it is not the part's text.
    structured = normalize_payload('{"x": 1, "y": false}')
    assert structured["raw_text"] == '{"x": 1, "y": false}'


def test_an_observer_keeps_a_large_output_once():
    harness = Recorder()
    output = big_output()
    harness.observer.handle_event(tool_called("bash_tool", "c1", '{"command": "ls"}'))
    harness.observer.handle_event(tool_output("c1", output))

    stored = harness.recorder.snapshot()[-1]
    # The output lives in the typed payload; ``body`` would be the same bytes.
    # The payload does not repeat the text as ``raw_text`` either.
    assert stored["body"] == ""
    payload = stored["result_payload"]
    assert "raw_text" not in payload
    assert output in payload["parts"][0]["data"]
    # The typed payload marks the step as done and the wire frame stays small.
    assert stored["status"] == "done"
    assert not harness.step_frames()[-1].get("body")


def test_a_small_output_keeps_its_legacy_body():
    harness = Recorder()
    harness.observer.handle_event(tool_called("read_file", "c1", '{"path": "a.py"}'))
    harness.observer.handle_event(tool_output("c1", "print(1)"))
    assert harness.recorder.snapshot()[-1]["body"] == "print(1)"


# -- glued deltas, snapshot instead of a journal ---------------------------
@pytest.mark.asyncio
async def test_an_answer_of_9000_deltas_flows_in_few_frames():
    turn, session = await finished_turn(tokens=9000)
    tokens = [frame for frame in session.sent if frame["type"] == "token"]
    assert len(tokens) < 300
    assert "".join(frame["content"] for frame in tokens) == "word " * 9000
    # No frame of the answer is larger than the flush budget plus one delta.
    assert all(len(frame["content"].encode()) <= DELTA_FLUSH_BYTES * 2 for frame in tokens)
    assert turn._pending == [] and len(turn._notes) <= MAX_REPLAY_NOTES


@pytest.mark.asyncio
async def test_a_reconnecting_socket_gets_a_snapshot_not_a_journal():
    turn, live = await finished_turn(tokens=9000, output=big_output())
    viewer = FakeSession()
    await turn.attach(viewer)

    header, replay = viewer.sent[0], viewer.sent[1:]
    assert header["type"] == "attached"
    assert len(replay) < 300  # ~one lean step + one token, not 9000 frames
    token = [frame for frame in replay if frame["type"] == "token"]
    assert len(token) == 1 and token[0]["content"] == "word " * 9000
    steps = [frame["step"] for frame in replay if frame["type"] == "step"]
    assert steps and all("lazy" in step or "body" in step for step in steps)
    # The replay is smaller than the live stream of the same turn.
    size = lambda frames: len(json.dumps(frames, ensure_ascii=False))  # noqa: E731
    assert size(replay) < size(live.sent)


@pytest.mark.asyncio
async def test_a_reasoning_delta_replays_as_its_step_text():
    turn, _ = await finished_turn(tokens=0)
    turn._recorder.reasoning_delta("hello ")
    turn._recorder.reasoning_delta("world")
    reasoning = next(step for step in turn._recorder.snapshot() if step["kind"] == StepKind.REASONING.value)
    turn._recorder.end_reasoning()

    viewer = FakeSession()
    await turn.attach(viewer)
    steps = [frame["step"] for frame in viewer.sent if frame["type"] == "step"]
    assert any(step["id"] == reasoning["id"] and step["body"] == "hello world" for step in steps)
    # The text is in the step, so no separate delta frames are replayed.
    assert not [frame for frame in viewer.sent if frame["type"] == "reasoning"]


# -- the conversation GET does not carry a second trace -------------------
def test_metadata_is_projected_by_an_explicit_whitelist():
    metadata = {
        "system_key": "s", "agent_key": "a", "routed_agent": "a",
        "tool_outputs": [{"output": "z" * 20000} for _ in range(100)],
        "checkpoints": {"turn": {"before": "x" * 50000}},
    }
    shown = project_metadata(metadata)
    assert shown == {"system_key": "s", "agent_key": "a", "routed_agent": "a"}
    assert len(json.dumps(shown)) < 200


def test_a_conversation_get_does_not_return_tool_outputs(factory):
    from tests.test_branches import web_client

    manager = factory.context_manager
    context_id = manager.start_new_context()
    outputs = [{"tool": "bash_tool", "output": "z" * 20000} for _ in range(100)]
    manager.update_context_metadata(
        context_id,
        {"created_by_web": True, "system_key": "s", "routed_agent": "worker",
         "workspace": "work", "tool_outputs": outputs},
    )
    response = web_client(factory).get(f"/api/chat/conversations/{context_id}")
    assert response.status_code == 200
    metadata = response.json()["metadata"]
    assert "tool_outputs" not in metadata
    assert metadata["system_key"] == "s" and metadata["routed_agent"] == "worker"
    assert metadata["workspace"] == "work"
    assert len(response.content) < 10_000


def test_the_step_endpoint_serves_a_live_step(factory):
    from fastapi.testclient import TestClient

    from tests.test_web_ui_routes import SAME_SITE, _DummySpace, _server

    manager = factory.context_manager
    context_id = manager.start_new_context()
    full = {"id": "s1", "kind": "tool", "title": "read", "body": "x" * 9000,
            "result_payload": {"version": 1, "parts": [{"kind": "text", "data": "x" * 9000}]}}
    live = SimpleNamespace(step_record=lambda step_id: full if step_id == "s1" else None)
    space = _DummySpace()
    space._context_manager = manager
    space.registry = SimpleNamespace(
        default_key=lambda: "s", factory=lambda system: factory, keys=lambda: ["s"],
        systems=lambda: [], can_route=False, has_catalog=False,
    )
    space.turns.register(context_id, live, SimpleNamespace(done=lambda: False))

    client = TestClient(_server(space).app, headers=SAME_SITE)
    response = client.get(f"/api/chat/conversations/{context_id}/messages/live/steps/s1")
    assert response.status_code == 200
    assert response.json()["body"] == "x" * 9000


# -- a stopped turn still delivers the text it already produced ------------
async def test_a_cancelled_turn_flushes_the_text_it_has(tmp_path=None):
    """Cancel lands between deltas: the held text must reach the tabs, not vanish.

    A turn stopped by the user (or by a shutdown) is cancelled in the middle of
    its stream; the deltas of the last answer were still glued in the buffer,
    and the tail never arrived. _consume's finally sends them.
    """
    session = FakeSession()
    turn = AgentTurn(session, "hello", system_key=None, agent_key=None)
    await turn._flush_deltas()  # nothing held yet
    start = len(session.sent)
    # Text already glued into the buffer...
    turn._buffer_delta({"type": "token", "content": "buffered "})
    # ...and text the reader has not taken from the queue yet.
    turn._token("first ")
    consumer = asyncio.create_task(turn._consume())
    # Let the consumer take what is queued and settle on the next event.
    for _ in range(100):
        await asyncio.sleep(0)
        if turn._queue.empty():
            break
    turn._token("tail ")  # arrives while it waits: still in the queue
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    tokens = [frame["content"] for frame in session.sent[start:] if frame["type"] == "token"]
    assert "".join(tokens) == "buffered first tail "
    assert turn._pending == [] and turn._pending_bytes == 0


async def test_a_flush_cancelled_mid_send_keeps_the_frames_it_did_not_send():
    class CancelAfterOne(FakeSession):
        async def send(self, event):
            await super().send(event)
            raise asyncio.CancelledError()

    session = CancelAfterOne()
    turn = AgentTurn(session, "hello", system_key=None, agent_key=None)
    turn._buffer_delta({"type": "token", "content": "one "})
    turn._buffer_delta({"type": "reasoning", "id": "r1", "delta": "two"})
    with pytest.raises(asyncio.CancelledError):
        await turn._flush_deltas()
    # The frame that was sent left the buffer; the one that was not stays for
    # the flush in _consume's finally.
    assert [frame["content"] if frame["type"] == "token" else frame["delta"]
            for frame in turn._pending] == ["two"]
