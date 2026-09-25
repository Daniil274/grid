"""ContextManager persistence: atomic, incremental, and never loses an unreadable file."""

import json

from core.context import ContextManager


def test_saves_are_atomic_and_readable_by_a_new_manager(tmp_path):
    path = tmp_path / "context.json"
    first = ContextManager(persist_path=str(path))
    first.add_message("user", "Hello")
    context_id = first.get_current_context_id()

    assert not (tmp_path / "context.json.tmp").exists()
    second = ContextManager(persist_path=str(path))
    assert second.conversation_view(context_id)["messages"][0].content == "Hello"


def test_only_changed_contexts_are_encoded_again(tmp_path, monkeypatch):
    manager = ContextManager(persist_path=str(tmp_path / "context.json"))
    for _ in range(3):
        manager.start_new_context()
        manager.add_message("user", "x")
    encoded = []
    original = manager._encode_context
    monkeypatch.setattr(manager, "_encode_context", lambda b: encoded.append(1) or original(b))

    manager.add_message("user", "y")

    assert len(encoded) == 1


def test_bookkeeping_can_wait_for_the_next_save(tmp_path):
    path = tmp_path / "context.json"
    manager = ContextManager(persist_path=str(path))
    manager.add_message("user", "first")
    context_id = manager.get_current_context_id()
    manager.set_metadata("events", [1], persist=False)
    stored = json.loads(path.read_text(encoding="utf-8"))["contexts"][context_id]["metadata"]
    assert "events" not in stored

    manager.add_message("user", "next")
    stored = json.loads(path.read_text(encoding="utf-8"))["contexts"][context_id]["metadata"]
    assert stored["events"] == [1]


def test_an_unreadable_file_is_kept_not_overwritten(tmp_path):
    path = tmp_path / "context.json"
    path.write_text("{ not json", encoding="utf-8")

    ContextManager(persist_path=str(path)).add_message("user", "new")

    kept = list(tmp_path.glob("context.json.unreadable-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{ not json"


def test_conversation_api_manages_contexts_without_internals(tmp_path):
    manager = ContextManager(persist_path=str(tmp_path / "context.json"))
    manager.ensure_context("ctx-00000001")
    assert manager.update_context_metadata("ctx-00000001", {"title": "T"}, create=False)
    assert not manager.update_context_metadata("ctx-ffffffff", {"title": "T"}, create=False)
    assert manager.conversation_view("ctx-00000001")["metadata"]["title"] == "T"

    active = manager.get_current_context_id()
    assert manager.delete_context(active)
    assert manager.get_current_context_id() not in (None, active)
    assert not manager.delete_context(active)
