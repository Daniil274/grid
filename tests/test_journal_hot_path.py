"""The run record of a turn: written off the event loop, after the browser event.

A chat of tens of megabytes used to re-encode its whole JSON for every tool
call, inside the event loop, before the observer got the event. Here that cost
is measured, and the guarantees that came with the old behaviour - a crashed
turn still finds its calls - are checked to survive.
"""

import asyncio
import json
import threading
import time
from pathlib import Path

from core.context import ContextManager
from core.conversation_store import ConversationStore
from core.factory.journal import RunJournal
from core.factory.turns import TurnRunner

#: One tool call may not block the event loop longer than this (the bead's limit).
BLOCK_LIMIT = 0.005


def journal_for(tmp_path: Path, *, max_history: int = 500):
    """A manager backed by a conversation store, the way the web chat has one."""
    store = ConversationStore(tmp_path / "conversations")
    written = []
    original = store.put

    def put(context_id: str, text: str) -> None:
        written.append((time.perf_counter(), context_id))
        original(context_id, text)

    store.put = put
    manager = ContextManager(
        max_history=max_history,
        create_initial_context=False,
        persist_path=str(tmp_path / "conversations.json"),
        store=store,
    )
    context_id = manager.create_context("ctx-00000001")
    return manager, RunJournal(manager, config=None), context_id, written


def seeded(manager, context_id, *, messages: int = 15, size: int = 550_000) -> None:
    """A conversation that has piled up, as a long chat's does."""
    for index in range(messages):
        manager.append_message_to(context_id, "assistant", f"report {index}: {'x' * size}")
    manager.save(strict=True)


def call(journal, context_id, index):
    journal.record_event(
        event_type="tool_called",
        tool_name="web_fetch",
        arguments=json.dumps({"url": f"https://example.org/{index}"}),
        extra={"retry_count": 0, "call_id": f"call-{index}"},
        persist=True,
        context_id=context_id,
    )


def wait_for_write(written, seen: int = 0, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(written) > seen:
            return True
        time.sleep(0.01)
    return False


def restarted(manager, tmp_path: Path) -> ContextManager:
    """A new process reading the store the background writer left behind."""
    assert manager.store.flush(timeout=10)
    return ContextManager(
        max_history=manager.max_history,
        create_initial_context=False,
        persist_path=str(tmp_path / "conversations.json"),
        store=ConversationStore(tmp_path / "conversations"),
    )


def test_a_tool_call_does_not_block_the_loop_and_is_written_after_the_event(tmp_path):
    manager, journal, context_id, written = journal_for(tmp_path)
    seeded(manager, context_id)
    seen = len(written)

    started = time.perf_counter()
    call(journal, context_id, 0)
    blocked = time.perf_counter() - started
    observer_event = time.perf_counter()  # the browser event leaves here

    assert blocked < BLOCK_LIMIT, f"the loop waited {blocked * 1e3:.1f} ms for a tool call"
    assert not [stamp for stamp, _ in written[seen:] if stamp <= observer_event], "written before the event"

    assert wait_for_write(written, seen), "the record never reached the store"
    assert written[seen][0] > observer_event


async def test_a_turn_of_two_hundred_calls_never_blocks_the_loop(tmp_path):
    manager, journal, context_id, written = journal_for(tmp_path)
    seeded(manager, context_id)
    seen = len(written)

    blocks = []
    for index in range(200):
        started = time.perf_counter()
        call(journal, context_id, index)
        journal.record_event(
            event_type="tool_output",
            tool_name="web_fetch",
            output="y" * 50_000,
            extra={"call_id": f"call-{index}"},
            context_id=context_id,
        )
        blocks.append(time.perf_counter() - started)
        await asyncio.sleep(0)

    document = manager._encode_context(manager._contexts[context_id])
    assert len(document) > 7_000_000  # a conversation the old code re-encoded per call
    assert max(blocks) < BLOCK_LIMIT, f"worst call blocked the loop {max(blocks) * 1e3:.1f} ms"
    assert wait_for_write(written, seen)


def test_bookkeeping_keeps_the_encoded_messages(tmp_path):
    manager, journal, context_id, _ = journal_for(tmp_path)
    seeded(manager, context_id, messages=4, size=1000)

    whole = manager._parts[context_id]["conversation"]
    whole_metadata = manager._parts[context_id]["metadata"]

    manager.set_metadata("pending_agent_run", {"status": "running"}, context_id=context_id)
    manager.save(strict=True)

    # The messages were reused as they were; only the metadata was made anew.
    assert manager._parts[context_id]["conversation"] is whole
    assert manager._parts[context_id]["metadata"] != whole_metadata
    assert "running" in manager._parts[context_id]["metadata"]

    manager.append_message_to(context_id, "user", "next")
    manager.save(strict=True)
    assert manager._parts[context_id]["conversation"] is not whole
    assert '"next"' in manager._parts[context_id]["conversation"]


def test_the_run_record_keeps_a_reference_and_a_preview_not_the_whole_output(tmp_path):
    manager, journal, context_id, _ = journal_for(tmp_path)
    manager.ensure_context(context_id)

    journal.record_event(
        event_type="tool_called",
        tool_name="web_fetch",
        arguments="a" * 100_000,
        output="b" * 1_000_000,
        extra={"call_id": "call-42"},
        persist=True,
        context_id=context_id,
    )

    event = manager.get_context_metadata(context_id)["pending_agent_run"]["tool_events"][-1]
    assert event["step"] == "call-42"  # the step of the trace it belongs to
    assert len(event["arguments"]) <= RunJournal.PREVIEW_LIMIT + 3
    assert len(event["output"]) <= RunJournal.PREVIEW_LIMIT + 3


def test_a_crashed_turn_finds_its_calls_in_the_store(tmp_path):
    manager, journal, context_id, written = journal_for(tmp_path)
    manager.ensure_context(context_id)
    manager.append_message_to(context_id, "user", "Open Notepad")
    journal.update_pending_run(
        agent_key="worker",
        active_context_id=context_id,
        input_preview="Open Notepad",
        status="running",
        task="Open Notepad",
        turn_id="t1",
    )
    seen = len(written)
    call(journal, context_id, 0)
    assert wait_for_write(written, seen)  # written without anyone asking for a save

    restarted_manager = restarted(manager, tmp_path)
    pending = restarted_manager.get_context_metadata(context_id)["pending_agent_run"]
    assert pending["status"] == "running" and pending["turn_id"] == "t1"
    assert [item["tool_name"] for item in pending["tool_events"]] == ["web_fetch"]

    interruption = restarted_manager.recover_abandoned_turn(context_id)
    assert interruption.in_flight == [{"tool": "web_fetch", "arguments": '{"url": "https://example.org/0"}'}]


def test_the_observer_gets_a_stream_event_before_it_is_journaled():
    order = []

    class Result:
        async def stream_events(self):
            yield object()

    class Observer:
        def handle_event(self, event, *, agent_key=None):
            order.append("observer")
            return "text"

    def journaled(event):
        order.append("journal")

    runner = TurnRunner.__new__(TurnRunner)
    fragments = asyncio.run(
        runner._consume_stream(Result(), observer=Observer(), agent_key="worker", action_state=None, on_event=journaled)
    )
    assert fragments == ["text"]
    assert order == ["observer", "journal"]


def test_a_failing_observer_does_not_stop_the_journal():
    order = []

    class Result:
        async def stream_events(self):
            yield object()

    class Observer:
        def handle_event(self, event, *, agent_key=None):
            raise RuntimeError("rendering broke")

    def journaled(event):
        order.append("journal")

    runner = TurnRunner.__new__(TurnRunner)
    fragments = asyncio.run(
        runner._consume_stream(Result(), observer=Observer(), agent_key="worker", action_state=None, on_event=journaled)
    )
    assert fragments == [] and order == ["journal"]


# -- a strict save is a barrier, not a race --------------------------------
def barrier_manager(tmp_path: Path):
    """A manager whose store holds every background put until the test lets it go."""
    store = ConversationStore(tmp_path / "conversations")
    original = store.put
    order: list[tuple[str, str]] = []  # (thread that put, context)
    in_put = threading.Event()
    release = threading.Event()

    def put(context_id: str, text: str) -> None:
        name = threading.current_thread().name
        if name == "context-saver":
            in_put.set()
            # Widen the window a real encode of a big conversation has: the
            # strict save must wait here instead of overtaking this snapshot.
            assert release.wait(10), "the test never released the background write"
        order.append((name, context_id))
        original(context_id, text)

    store.put = put
    manager = ContextManager(
        create_initial_context=False,
        persist_path=str(tmp_path / "conversations.json"),
        store=store,
    )
    return manager, order, in_put, release


def test_a_strict_save_waits_for_a_snapshot_taken_before_it(tmp_path):
    manager, order, in_put, release = barrier_manager(tmp_path)
    # The active conversation: the one a strict save always writes.
    context_id = manager.start_new_context("ctx-00000001")
    manager.append_message_to(context_id, "user", "fine")
    assert in_put.wait(10), "the background save never started"

    strict = threading.Thread(target=lambda: manager.save(strict=True), name="strict-save")
    strict.start()
    strict.join(0.2)
    assert strict.is_alive(), "the strict save overtook the snapshot already in flight"
    release.set()
    strict.join(10)
    assert not strict.is_alive(), "the strict save never got past the background write"

    # The strict save is the last writer; nothing started before it landed after.
    assert order[-1] == ("strict-save", context_id), order
    assert [name for name, _ in order].count("context-saver") == 1
    assert restarted(manager, tmp_path).conversation_view(context_id)["messages"][0].content == "fine"


def test_a_strict_save_carries_the_requests_made_before_it(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    original = store.put
    written: list[str] = []

    def put(context_id: str, text: str) -> None:
        written.append(context_id)
        original(context_id, text)

    store.put = put
    manager = ContextManager(
        create_initial_context=False,
        persist_path=str(tmp_path / "conversations.json"),
        store=store,
    )
    context_id = manager.create_context("ctx-00000001")
    manager.append_message_to(context_id, "user", "fine")
    with manager._save_cond:  # what _request_save does, without waking the thread
        seq = manager._save_seq

    manager.save(strict=True)
    seen = len(written)
    # The run a request of *seq* would start finds nothing left to do: the
    # strict save already wrote it, so nothing is written after the barrier.
    ContextManager._save_async(manager, seq)
    assert written[seen:] == []
