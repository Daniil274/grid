"""Conversations kept one file each: compressed, sealed, written in the background."""

import json

import pytest
from cryptography.fernet import Fernet

from core.context import ContextManager
from core.conversation_store import (
    ConversationCodec,
    ConversationStore,
    KeyMissing,
    StoreError,
    export_legacy,
    pack,
    unpack,
)


def codec(*keys):
    return ConversationCodec.from_secrets(",".join(keys))


def test_a_sealed_file_opens_only_as_its_own_conversation_and_with_a_known_key():
    key = Fernet.generate_key().decode()
    sealed = pack('{"hello": "мир"}', "ctx-1", codec(key))
    assert b"hello" not in sealed and b"\xd0\xbc" not in sealed  # neither text nor its bytes
    assert json.loads(unpack(sealed, "ctx-1", codec(key))) == {"hello": "мир"}
    with pytest.raises(StoreError):
        unpack(sealed, "ctx-2", codec(key))  # a file copied under another id
    with pytest.raises(StoreError):
        unpack(sealed, "ctx-1", codec(Fernet.generate_key().decode()))
    with pytest.raises(StoreError):
        unpack(sealed, "ctx-1", None)


def test_a_rotated_key_still_opens_old_files():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    sealed = pack("{}", "ctx-1", codec(old))
    assert unpack(sealed, "ctx-1", codec(new, old)) == "{}"


def test_without_a_key_files_are_compressed_only():
    text = json.dumps({"trace": ["x" * 1000] * 100})
    packed = pack(text, "ctx-1", None)
    assert len(packed) < len(text) / 20
    assert unpack(packed, "ctx-1", None) == text


def manager_at(tmp_path, key=None):
    store = ConversationStore(tmp_path / "conversations", codec(key) if key else None)
    return ContextManager(persist_path=str(tmp_path / "conversations.json"), store=store)


def test_conversations_come_back_after_a_restart_and_only_changed_ones_are_written(tmp_path):
    key = Fernet.generate_key().decode()
    first = manager_at(tmp_path, key)
    a, b = first.start_new_context(), first.start_new_context()
    first.append_message_to(a, "user", "hello a")
    first.append_message_to(b, "user", "hello b")
    first.save(strict=True)
    written = (tmp_path / "conversations" / f"{a}.grc").stat().st_mtime_ns

    first.append_message_to(b, "user", "again b")
    first.save(strict=True)
    assert (tmp_path / "conversations" / f"{a}.grc").stat().st_mtime_ns == written

    second = manager_at(tmp_path, key)
    assert [m.content for m in second.conversation_view(a)["messages"]] == ["hello a"]
    assert [m.content for m in second.conversation_view(b)["messages"]] == ["hello b", "again b"]


def test_a_deleted_conversation_leaves_no_file(tmp_path):
    manager = manager_at(tmp_path)
    context_id = manager.start_new_context()
    manager.append_message_to(context_id, "user", "bye")
    manager.save(strict=True)
    assert manager.delete_context(context_id)
    manager.save(strict=True)
    assert not (tmp_path / "conversations" / f"{context_id}.grc").exists()


def test_the_old_single_file_is_moved_into_the_store_and_kept_aside(tmp_path):
    legacy = ContextManager(persist_path=str(tmp_path / "conversations.json"))
    context_id = legacy.start_new_context()
    legacy.append_message_to(context_id, "user", "from before")
    legacy.save()

    key = Fernet.generate_key().decode()
    moved = manager_at(tmp_path, key)

    assert [m.content for m in moved.conversation_view(context_id)["messages"]] == ["from before"]
    assert not (tmp_path / "conversations.json").exists()
    assert list(tmp_path.glob("conversations.json.migrated-*"))
    assert (tmp_path / "conversations" / f"{context_id}.grc").exists()
    # The store is the record now: a restart reads it.
    again = manager_at(tmp_path, key)
    assert [m.content for m in again.conversation_view(context_id)["messages"]] == ["from before"]


def test_an_unreadable_conversation_is_kept_aside_and_the_rest_load(tmp_path):
    manager = manager_at(tmp_path)
    good, bad = manager.start_new_context(), manager.start_new_context()
    manager.append_message_to(good, "user", "fine")
    manager.append_message_to(bad, "user", "broken")
    manager.save(strict=True)
    (tmp_path / "conversations" / f"{bad}.grc").write_bytes(b"GRC1\x01garbage")

    reloaded = manager_at(tmp_path)
    assert reloaded.conversation_view(good) is not None
    assert reloaded.conversation_view(bad) is None
    assert (tmp_path / "conversations" / f"{bad}.grc.unreadable").exists()


@pytest.mark.parametrize("restart_key", [None, "other"])
def test_a_missing_or_wrong_key_stops_the_load_and_moves_nothing(tmp_path, restart_key):
    key = Fernet.generate_key().decode()
    manager = manager_at(tmp_path, key)
    context_id = manager.start_new_context()
    manager.append_message_to(context_id, "user", "secret")
    manager.save(strict=True)

    wrong = Fernet.generate_key().decode() if restart_key else None
    with pytest.raises(KeyMissing, match="GRID_SECRETS_KEY"):
        manager_at(tmp_path, wrong)
    # Nothing went aside: with the right key back, the chat is there.
    assert not list((tmp_path / "conversations").glob("*.unreadable"))
    again = manager_at(tmp_path, key)
    assert [m.content for m in again.conversation_view(context_id)["messages"]] == ["secret"]


def test_the_store_exports_back_to_the_old_single_file(tmp_path):
    key = Fernet.generate_key().decode()
    manager = manager_at(tmp_path, key)
    context_id = manager.start_new_context()
    manager.append_message_to(context_id, "user", "take me back")
    manager.save(strict=True)

    target = tmp_path / "rollback" / "conversations.json"
    target.parent.mkdir()
    assert export_legacy(tmp_path / "conversations", target, codec(key)) >= 1
    # A build without the store reads it as its own.
    legacy = ContextManager(persist_path=str(target))
    assert [m.content for m in legacy.conversation_view(context_id)["messages"]] == ["take me back"]
