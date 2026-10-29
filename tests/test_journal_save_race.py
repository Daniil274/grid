"""The run record and the background saver: no torn record, no dropped save.

Two defects were suspected for the tool events of a running turn:

* ``RunJournal.record_event`` edited the stored ``tool_events`` list in place
  while the saver encoded that metadata in another thread. At HEAD it already
  replaces the record instead (core/factory/journal.py, the ``pending = dict(...)``
  copy); the test below pins that contract, so an in-place append cannot come back.
* A background save that failed left the conversations out of the dirty set and
  was counted as written (core/context.py, ``_snapshot_jobs`` /
  ``_saver_loop``), so the change waited for the next write of that conversation -
  which may never come. That one is fixed here: the snapshot is put back into the
  dirty set and the save is asked for again.
"""

import json
import threading
import time

from core.context import ContextManager
from core.conversation_store import ConversationStore, unpack
from core.factory.journal import RunJournal


def journal_for(tmp_path):
    """A manager backed by a conversation store, the way the web chat has one."""
    store = ConversationStore(tmp_path / "conversations")
    manager = ContextManager(
        max_history=500,
        create_initial_context=False,
        persist_path=str(tmp_path / "conversations.json"),
        store=store,
    )
    context_id = manager.create_context("ctx-00000001")
    return manager, store, RunJournal(manager, config=None), context_id


def stored_metadata(store, context_id):
    """The metadata of one conversation as the store has it on disk.

    The file alone: a save that never reached the store leaves no manifest yet.
    """
    path = store.path_of(context_id)
    if not path.exists():
        return {}
    return json.loads(unpack(path.read_bytes(), context_id, store.codec))["metadata"]

def wait_until(predicate, timeout: float = 10.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_a_failed_background_save_is_retried_and_the_change_is_not_lost(tmp_path, monkeypatch):
    manager, store, journal, context_id = journal_for(tmp_path)
    original = store.put
    failures = {"left": 2}

    def flaky_put(target_id, text):
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("disk full (simulated)")
        original(target_id, text)

    monkeypatch.setattr(store, "put", flaky_put)

    manager.set_metadata("marker", "kept", context_id=context_id)

    # Nothing else writes that conversation: only the retry can put it on disk.
    assert wait_until(lambda: stored_metadata(store, context_id).get("marker") == "kept"), (
        "a failed background save dropped the change: it was never written again"
    )
    assert failures["left"] == 0


def test_a_failed_background_save_is_not_counted_as_written(tmp_path, monkeypatch):
    manager, store, journal, context_id = journal_for(tmp_path)
    failures = {"count": 0}

    def refusing_put(target_id, text):
        failures["count"] += 1
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(store, "put", refusing_put)

    manager.set_metadata("marker", "kept", context_id=context_id)
    assert wait_until(lambda: failures["count"] >= 1), "the background save never ran"

    # The failed run carried nothing: the conversation is still changed, and the
    # request it served is not on record as written.
    assert context_id in manager._dirty, "the failed save dropped the change from the dirty set"
    assert manager._save_written_seq < manager._save_seq, "a failed save was counted as written"

    # And it is tried again on its own, with a pause between attempts.
    tried = failures["count"]
    assert wait_until(lambda: failures["count"] > tried), "the failed save was never retried"

    monkeypatch.undo()
    manager.save(strict=True)


def test_record_event_replaces_the_stored_record_instead_of_editing_it(tmp_path):
    manager, store, journal, context_id = journal_for(tmp_path)
    journal.record_event(
        event_type="tool_called", tool_name="first", extra={"call_id": "c1"},
        persist=False, context_id=context_id,
    )
    # What a saver encoding the metadata in another thread holds at this point.
    handed_over = manager.get_metadata("pending_agent_run", context_id=context_id)
    events = handed_over["tool_events"]
    document = json.dumps(handed_over)

    journal.record_event(
        event_type="tool_called", tool_name="second", extra={"call_id": "c2"},
        persist=False, context_id=context_id,
    )

    stored = manager.get_metadata("pending_agent_run", context_id=context_id)
    assert stored is not handed_over, "the stored record was edited in place"
    assert stored["tool_events"] is not events, "the stored tool_events list was edited in place"
    assert [event["tool_name"] for event in events] == ["first"]
    assert [event["tool_name"] for event in stored["tool_events"]] == ["first", "second"]
    # The object the saver had does not change under its encoder.
    assert json.dumps(handed_over) == document


def test_a_record_read_while_events_arrive_is_always_a_whole_record(tmp_path):
    manager, store, journal, context_id = journal_for(tmp_path)
    journal.record_event(
        event_type="tool_called", tool_name="seed", extra={"call_id": "c0"},
        persist=False, context_id=context_id,
    )
    stop = threading.Event()
    encoded = []
    started = threading.Event()

    def saver():
        started.set()
        while not stop.is_set():
            payload = manager.get_metadata("pending_agent_run", context_id=context_id)
            encoded.append(json.dumps(payload, default=str))

    thread = threading.Thread(target=saver, name="test-saver")
    thread.start()
    started.wait(10)
    for index in range(300):
        journal.record_event(
            event_type="tool_called", tool_name=f"t{index}", extra={"call_id": f"c{index}"},
            persist=False, context_id=context_id,
        )
    stop.set()
    thread.join(10)

    assert encoded, "the saver read nothing"
    for text in encoded:
        payload = json.loads(text)
        for event in payload.get("tool_events") or []:
            assert {"event_type", "timestamp"} <= set(event), "a saver encoded a half-written record"
    assert len(manager.get_metadata("pending_agent_run", context_id=context_id)["tool_events"]) == 100
