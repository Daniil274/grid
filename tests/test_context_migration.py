"""Reference-only bootstrap: a new pinned context, the old history never executed.

A legacy conversation (and a pinned one this runtime cannot continue) can be
*referenced* from a new empty conversation. The dry run writes nothing and
returns a machine-readable report with a confirmation token bound to the exact
source state; applying it needs that token and an explicit confirmation, pins
the new conversation before writing its record, and imports no history,
executions, queues, sessions or workspace files.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import migration, revisions
from core.context import ContextManager
from core.conversation_store import ConversationStore
from tests.test_revision_compatibility import (
    _resigned,
    _seeded_legacy,
    make_factory,
)

# -- helpers -------------------------------------------------------------------


def legacy_manager(tmp_path: Path, context_id: str = "ctx-legacy01"):
    """A manager holding one stored conversation without a manifest."""
    path = tmp_path / "context.json"
    _seeded_legacy(path, context_id)
    manager = ContextManager(max_history=50, persist_path=str(path), create_initial_context=False)
    return manager, context_id, path


def pinned_manager(tmp_path: Path, config, *, incompatible: bool = False):
    """A manager holding one conversation pinned to *config*."""
    manager = ContextManager(
        max_history=50, persist_path=str(tmp_path / "pinned.json"), create_initial_context=False
    )
    context_id = manager.create_context()
    manager.add_message("user", "Pinned history", context_id=context_id)
    manager.pin_revision(
        context_id, revisions.build_manifest(context_id, revisions.environment_of(config))
    )
    if incompatible:
        # Any resolved setting changes the configuration digest.
        config.config.settings.max_turns = 99
    return manager, context_id


def stored(manager: ContextManager, context_id: str) -> dict:
    view = manager.conversation_view(context_id)
    assert view is not None
    return view


def titles(manager: ContextManager) -> list:
    return [manager.get_context_metadata(context_id).get("title") for context_id in manager.list_context_ids()]


# -- dry run -------------------------------------------------------------------


def test_dry_run_reports_unknown_provenance_and_writes_nothing(tmp_path):
    manager, context_id, path = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    before = path.read_bytes()
    ids_before = manager.list_context_ids()

    report = migration.plan(manager, context_id, config)

    assert report["action"] == migration.ACTION
    assert report["version"] == migration.VERSION
    assert report["status"] == "ready"
    assert report["code"] == migration.READY

    source = report["source"]
    assert source["provenance"] == "unknown"  # explicit, not a compatibility claim
    assert source["revision_status"] == "legacy"
    assert source["manifest"] == {"status": "none"}
    assert source["messages"] == 1

    reference = report["reference"]
    assert reference["executable"] is False and reference["replay"] is False
    assert reference["history"]["selected"] is True
    assert reference["history"]["messages"] == 1
    assert reference["history"]["kept_in"] == context_id
    assert reference["artifacts"]["copied"] is False
    assert reference["excluded"]["agent_sessions"] == "not-imported"
    assert reference["excluded"]["replay"] is False

    assert report["confirmation"]["required"] is True
    assert report["confirmation"]["imports_executable_state"] is False
    assert report["effects"]["model_calls"] == 0
    assert report["limitations"]["history_imported"] is False
    assert report["limitations"]["original_preserved"] is True
    assert report["limitations"]["provenance"] == "unknown"

    # Nothing was created and the source file was not rewritten.
    assert manager.list_context_ids() == ids_before
    assert path.read_bytes() == before


def test_the_confirmation_token_binds_the_exact_source_state(tmp_path):
    manager, context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config

    first = migration.plan(manager, context_id, config)
    again = migration.plan(manager, context_id, config)
    assert first["confirmation"]["token"] == again["confirmation"]["token"]

    manager.add_message("user", "A new message", context_id=context_id)
    changed = migration.plan(manager, context_id, config)
    assert changed["confirmation"]["token"] != first["confirmation"]["token"]


def test_a_source_without_a_resolved_system_cannot_be_bootstrapped(tmp_path):
    manager, context_id, _ = legacy_manager(tmp_path)

    report = migration.plan(manager, context_id, None)

    assert report["status"] == "blocked"
    assert report["code"] == migration.SYSTEM_UNRESOLVED
    assert manager.list_context_ids() == [context_id]


def test_a_manager_that_cannot_persist_does_not_offer_a_bootstrap(tmp_path):
    path = tmp_path / "context.json"
    _seeded_legacy(path)
    manager = ContextManager(
        max_history=50, persist_path=str(path), create_initial_context=False, read_only=True
    )
    config = make_factory(tmp_path).config

    report = migration.plan(manager, "ctx-legacy01", config)

    assert report["status"] == "blocked"
    assert report["code"] == migration.PERSISTENCE_UNAVAILABLE
    assert manager.list_context_ids() == ["ctx-legacy01"]


# -- confirmation and idempotence ---------------------------------------------


def test_apply_requires_an_explicit_request_bound_confirmation(tmp_path):
    manager, context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    token = migration.plan(manager, context_id, config)["confirmation"]["token"]

    missing = migration.apply(manager, context_id, config, confirmation=token)
    assert missing["status"] == "blocked"
    assert missing["code"] == migration.CONFIRMATION_REQUIRED
    assert manager.list_context_ids() == [context_id]

    wrong = migration.apply(manager, context_id, config, confirmation="f" * 64, confirm=True)
    assert wrong["status"] == "blocked"
    assert wrong["code"] == migration.CONFIRMATION_STALE
    assert manager.list_context_ids() == [context_id]

    # A token issued for another conversation is not accepted here.
    other_path = tmp_path / "other.json"
    _seeded_legacy(other_path, "ctx-other99")
    other = ContextManager(max_history=50, persist_path=str(other_path), create_initial_context=False)
    other_token = migration.plan(other, "ctx-other99", config)["confirmation"]["token"]
    crossed = migration.apply(manager, context_id, config, confirmation=other_token, confirm=True)
    assert crossed["status"] == "blocked"
    assert crossed["code"] == migration.CONFIRMATION_STALE
    assert manager.list_context_ids() == [context_id]


def test_a_source_changed_after_the_preview_refuses_the_confirmation(tmp_path):
    manager, context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    token = migration.plan(manager, context_id, config)["confirmation"]["token"]

    manager.add_message("user", "Arrived while the dialog was open", context_id=context_id)

    stale = migration.apply(manager, context_id, config, confirmation=token, confirm=True)
    assert stale["status"] == "blocked"
    assert stale["code"] == migration.CONFIRMATION_STALE
    assert manager.list_context_ids() == [context_id]


def test_apply_creates_one_empty_pinned_context_and_preserves_the_original(tmp_path):
    manager, context_id, path = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    original = stored(manager, context_id)
    original_messages = [message.model_dump() for message in original["messages"]]
    token = migration.plan(manager, context_id, config)["confirmation"]["token"]

    report = migration.apply(manager, context_id, config, confirmation=token, confirm=True)

    assert report["status"] == "applied"
    assert report["code"] == migration.APPLIED
    target_id = report["target"]["context_id"]
    assert target_id != context_id
    assert report["effects"] == {
        "external": [],
        "model_calls": 0,
        "tool_calls": 0,
        "replays": 0,
        "local_writes": ["new-conversation", "revision-pin", "migration-record"],
    }

    # The target is a *new, empty* conversation: no history was adopted.
    target = stored(manager, target_id)
    assert target["messages"] == []
    assert manager.get_context_messages(target_id, include_full=True)["count"] == 0
    assert manager.get_context_executions(target_id)["count"] == 0
    metadata = target["metadata"]
    assert metadata["created_by_web"] is True
    assert "pending_messages" not in metadata
    assert "pending_agent_run" not in metadata
    assert "checkpoints" not in metadata
    assert "session_epochs" not in metadata
    assert metadata["migration"]["source_context_id"] == context_id
    assert metadata["migration"]["executable_state"] == "none"
    assert metadata["migration"]["reference"]["excluded"]["pending_messages"] == 0

    # The target was pinned before its record; the stored pin is the one later
    # turns are judged by and it is compatible here.
    manifest = manager.revision_manifest(target_id)
    assert manifest is not None and revisions.manifest_digest_ok(manifest)
    assert manifest["context_id"] == target_id
    assert manifest["grid_version"] == revisions.GRID_VERSION
    assert revisions.admit_turn(manager, target_id, config) == manifest
    assert report["target"]["manifest"]["status"] == "pinned"

    # The original is untouched: same messages, still legacy, no manifest.
    after = stored(manager, context_id)
    assert [message.model_dump() for message in after["messages"]] == original_messages
    assert manager.revision_status(context_id) == "legacy"
    assert manager.revision_manifest(context_id) is None
    stored_file = json.loads(path.read_text(encoding="utf-8"))
    assert stored_file["contexts"][context_id]["conversation_history"] == json.loads(
        json.dumps(original_messages)
    )
    assert "revision" not in stored_file["contexts"][context_id]
    # And the source's own non-executable metadata (queues, sessions) is still there,
    # not imported into the target.
    assert stored_file["contexts"][context_id]["metadata"] == original["metadata"]


def test_approvals_queues_sessions_and_credentials_are_never_carried_over(tmp_path):
    """The target is a new chat with its own state: nothing pending or secret moves."""
    manager, context_id, _ = legacy_manager(tmp_path)
    manager.update_context_metadata(
        context_id,
        {
            "pending_approvals": [{"approval_id": "ap-1", "tool": "shell"}],
            "credentials": {"OPENAI_API_KEY": "sk-super-secret-token-1234567890"},
            "action_policy": {"remembered": [{"tool": "shell", "allow": True}]},
            "pending_messages": [{"text": "queued"}],
            "pending_agent_run": {"status": "waiting"},
            "session_epochs": {"worker": 3},
            "checkpoints": {"turn-1": "cp-1"},
            "workspace": "/tmp/old-chat-workspace",
            "routed_system": "old-system",
            "routed_agent": "old-agent",
        },
    )
    config = make_factory(tmp_path).config
    token = migration.plan(manager, context_id, config)["confirmation"]["token"]

    report = migration.apply(manager, context_id, config, confirmation=token, confirm=True)

    assert report["status"] == "applied"
    target_id = report["target"]["context_id"]
    metadata = manager.get_context_metadata(target_id)
    for key in (
        "pending_approvals",
        "credentials",
        "action_policy",
        "pending_messages",
        "pending_agent_run",
        "session_epochs",
        "checkpoints",
        "workspace",
        "routed_system",
        "routed_agent",
    ):
        assert key not in metadata
    # Neither the report nor the target record may carry a secret or a queue.
    rendered = json.dumps({"report": report, "metadata": metadata})
    assert "super-secret-token" not in rendered
    assert "queued" not in rendered
    excluded = metadata["migration"]["reference"]["excluded"]
    assert excluded["pending_messages"] == 1
    assert excluded["pending_agent_run"] == "waiting"
    assert excluded["agent_sessions"] == "not-imported"
    assert excluded["checkpoints"] == "not-restored"
    # The source keeps its own state; only a reference was taken.
    source_metadata = manager.get_context_metadata(context_id)
    assert source_metadata["credentials"]["OPENAI_API_KEY"].startswith("sk-")


def test_apply_is_idempotent(tmp_path):
    manager, context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    token = migration.plan(manager, context_id, config)["confirmation"]["token"]

    first = migration.apply(manager, context_id, config, confirmation=token, confirm=True)
    target_id = first["target"]["context_id"]
    ids_after_first = manager.list_context_ids()
    manifest = manager.revision_manifest(target_id)
    # Using the new conversation does not make the source migrate again.
    manager.add_message("user", "A first turn in the new conversation", context_id=target_id)

    second = migration.apply(manager, context_id, config, confirmation=token, confirm=True)
    without_token = migration.apply(manager, context_id, config)

    for report in (second, without_token):
        assert report["status"] == "completed"
        assert report["code"] == migration.COMPLETED
        assert report["idempotent"] is True
        assert report["target"]["context_id"] == target_id
    assert manager.list_context_ids() == ids_after_first
    assert manager.revision_manifest(target_id) == manifest
    assert sum(1 for context in manager.list_context_ids() if target_id == context) == 1


# -- corrupt, future and incompatible sources ----------------------------------


def test_a_tampered_source_manifest_is_reported_not_migrated(tmp_path):
    config = make_factory(tmp_path).config
    manager, context_id = pinned_manager(tmp_path, config)
    manager._contexts[context_id]["revision"]["system"]["digest"] = "sha256:" + "0" * 64
    ids_before = manager.list_context_ids()

    report = migration.plan(manager, context_id, config)

    assert report["status"] == "blocked"
    assert report["code"] == migration.SOURCE_TAMPERED
    # Corrupt provenance is never presented as pinned or recorded.
    assert report["source"]["provenance"] == "unknown"
    assert report["limitations"]["provenance"] == "unknown"
    assert report["confirmation"]["token"] is None
    blocked = migration.apply(manager, context_id, config, confirmation="0" * 64, confirm=True)
    assert blocked["code"] == migration.SOURCE_TAMPERED
    assert manager.list_context_ids() == ids_before


def test_a_future_manifest_version_is_refused_fail_safe(tmp_path):
    config = make_factory(tmp_path).config
    manager, context_id = pinned_manager(tmp_path, config)
    manager._contexts[context_id]["revision"] = _resigned(
        manager.revision_manifest(context_id), manifest_version=99
    )

    report = migration.plan(manager, context_id, config)

    assert report["status"] == "blocked"
    assert report["code"] == migration.SOURCE_FUTURE
    assert report["source"]["provenance"] == "unknown"
    assert report["limitations"]["provenance"] == "unknown"
    assert manager.list_context_ids() == [context_id]


def test_a_malformed_manifest_or_a_foreign_one_is_refused(tmp_path):
    config = make_factory(tmp_path).config
    manager, context_id = pinned_manager(tmp_path, config)

    manager._contexts[context_id]["revision"] = {"manifest_version": 1}
    malformed = migration.plan(manager, context_id, config)
    assert malformed["status"] == "blocked"
    assert malformed["code"] == migration.SOURCE_MALFORMED
    assert malformed["source"]["provenance"] == "unknown"
    assert malformed["limitations"]["provenance"] == "unknown"

    manager, context_id = pinned_manager(tmp_path / "foreign", config)
    manager._contexts[context_id]["revision"] = _resigned(
        manager.revision_manifest(context_id), context_id="ctx-someone-else"
    )
    foreign = migration.plan(manager, context_id, config)
    assert foreign["status"] == "blocked"
    assert foreign["code"] == migration.SOURCE_MISMATCH
    assert foreign["source"]["provenance"] == "unknown"
    assert foreign["limitations"]["provenance"] == "unknown"


def test_a_pinned_but_incompatible_source_can_be_referenced(tmp_path):
    config = make_factory(tmp_path).config
    manager, context_id = pinned_manager(tmp_path / "source", config, incompatible=True)
    source_manifest = manager.revision_manifest(context_id)
    token = migration.plan(manager, context_id, config)["confirmation"]["token"]

    preview = migration.plan(manager, context_id, config)
    assert preview["status"] == "ready"
    assert preview["source"]["provenance"] == "pinned"
    assert preview["limitations"]["provenance"] == "recorded"
    assert preview["source"]["revision_status"] == "pinned"
    assert preview["source"]["compatibility"]["ok"] is False

    report = migration.apply(manager, context_id, config, confirmation=token, confirm=True)
    assert report["status"] == "applied"
    target_id = report["target"]["context_id"]
    assert stored(manager, target_id)["messages"] == []
    # Pinned to *this* configuration, not to the source's.
    assert revisions.admit_turn(manager, target_id, config) is not None
    assert manager.revision_manifest(context_id) == source_manifest
    assert manager.revision_status(context_id) == "pinned"


def test_a_compatible_source_is_not_copied(tmp_path):
    config = make_factory(tmp_path).config
    manager, context_id = pinned_manager(tmp_path, config)

    report = migration.plan(manager, context_id, config)

    assert report["status"] == "blocked"
    assert report["code"] == migration.SOURCE_COMPATIBLE
    assert manager.list_context_ids() == [context_id]


def test_an_empty_or_unpinned_source_is_not_migrated(tmp_path):
    config = make_factory(tmp_path).config
    manager = ContextManager(
        max_history=50, persist_path=str(tmp_path / "empty.json"), create_initial_context=False
    )
    manager.add_message("user", "New in this process", context_id=manager.create_context())
    unpinned_id = manager.list_context_ids()[0]

    unpinned = migration.plan(manager, unpinned_id, config)
    assert unpinned["status"] == "blocked"
    assert unpinned["code"] == migration.SOURCE_UNPINNED

    empty_path = tmp_path / "empty-stored.json"
    empty_path.write_text(
        json.dumps(
            {
                "active_context_id": "ctx-empty001",
                "contexts": {
                    "ctx-empty001": {
                        "conversation_history": [],
                        "execution_history": [],
                        "metadata": {},
                        "created_at": "2026-01-01T00:00:00",
                        "updated_at": "2026-01-01T00:00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    empty = ContextManager(max_history=50, persist_path=str(empty_path), create_initial_context=False)
    report = migration.plan(empty, "ctx-empty001", config)
    assert report["status"] == "blocked"
    assert report["code"] == migration.SOURCE_EMPTY


def test_a_damaged_target_blocks_instead_of_creating_a_second_copy(tmp_path):
    manager, context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    token = migration.plan(manager, context_id, config)["confirmation"]["token"]
    target_id = migration.apply(manager, context_id, config, confirmation=token, confirm=True)["target"][
        "context_id"
    ]
    manager._contexts[target_id]["revision"]["system"]["digest"] = "sha256:" + "0" * 64
    ids_before = manager.list_context_ids()

    report = migration.plan(manager, context_id, config)

    assert report["status"] == "blocked"
    assert report["code"] == migration.TARGET_INVALID
    assert manager.list_context_ids() == ids_before


# -- the source store is not rewritten ----------------------------------------


def test_a_store_backed_source_file_is_byte_for_byte_unchanged(tmp_path):
    """With per-conversation storage, bootstrapping touches only the new file."""
    directory = tmp_path / "conversations"
    store = ConversationStore(directory)
    manager = ContextManager(
        max_history=50, persist_path=str(tmp_path / "unused.json"), create_initial_context=False, store=store
    )
    context_id = manager.create_context("ctx-legacy02")
    manager.add_message("user", "Stored before revisions", context_id=context_id)
    # Make it legacy: no manifest was pinned, and the store now holds it.
    manager._save_to_file(strict=True)
    store.flush(durable=True, timeout=5)
    manager = ContextManager(
        max_history=50, persist_path=str(tmp_path / "unused.json"), create_initial_context=False, store=store
    )
    assert manager.revision_status("ctx-legacy02") == "legacy"
    source_path = store.path_of("ctx-legacy02")
    source_messages = [message.model_dump() for message in stored(manager, "ctx-legacy02")["messages"]]

    config = make_factory(tmp_path).config
    token = migration.plan(manager, "ctx-legacy02", config)["confirmation"]["token"]
    report = migration.apply(manager, "ctx-legacy02", config, confirmation=token, confirm=True)

    assert report["status"] == "applied"
    target_id = report["target"]["context_id"]
    assert store.path_of(target_id).exists()
    assert manager.revision_status("ctx-legacy02") == "legacy"
    assert source_path.exists()
    # Re-read the store: the source's history and its "no manifest" state are
    # exactly what they were before the bootstrap.
    store.flush(durable=True, timeout=5)
    reloaded = ContextManager(
        max_history=50, persist_path=str(tmp_path / "unused.json"), create_initial_context=False, store=store
    )
    assert reloaded.revision_status("ctx-legacy02") == "legacy"
    assert reloaded.revision_manifest("ctx-legacy02") is None
    assert [message.model_dump() for message in stored(reloaded, "ctx-legacy02")["messages"]] == source_messages
    assert stored(reloaded, target_id)["messages"] == []
    assert reloaded.revision_status(target_id) == "pinned"


# -- over HTTP: owner space, explicit confirmation, machine-readable report ----


class _Turns:
    """The turn holding a web space uses; no turn runs in these tests."""

    def is_busy(self, context_id: str) -> bool:
        return False

    def is_running(self, context_id: str) -> bool:
        return False

    def claim(self, context_id: str) -> bool:
        return True

    def release(self, context_id: str) -> None:
        return None


class _Space:
    def __init__(self, manager: ContextManager, factory, user_id: str):
        self._manager = manager
        self._factory = factory
        self.user_id = user_id
        self.turns = _Turns()
        self.registry = SimpleNamespace(
            default_key=lambda: "main", config=lambda key: self._factory.config
        )

    def context_manager(self) -> ContextManager:
        return self._manager

    def registry_of(self, context_id: str):
        return self.registry

    async def registry_for(self, context_id: str):
        return self.registry


def web_client(tmp_path):
    """Two users, each with a space of their own; alice has a legacy conversation."""
    from fastapi.testclient import TestClient

    from web_chat.identity import User
    from web_chat.server import WebChatServer
    from web_chat.spaces import SpacePool

    spaces = {}

    def build(user_id: str):
        root = tmp_path / user_id
        root.mkdir(exist_ok=True)
        path = root / "context.json"
        if user_id == "alice":
            _seeded_legacy(path)
        manager = ContextManager(
            max_history=50, persist_path=str(path), create_initial_context=False
        )
        factory = make_factory(root)
        factory.context_manager = manager
        spaces[user_id] = _Space(manager, factory, user_id)
        return spaces[user_id]

    async def identify(connection):
        name = connection.headers.get("X-Test-User", "alice")
        return User(id=name, username=name, role="user")

    server = WebChatServer(
        SimpleNamespace(voice_enabled=lambda: False),
        SpacePool(build),
        identify=identify,
        warm_user=None,
    )
    return TestClient(server.app, headers={"Origin": "http://testserver"}), spaces


def test_the_migration_api_is_owner_scoped_and_requires_the_bound_confirmation(tmp_path):
    client, _spaces = web_client(tmp_path)
    alice = {"X-Test-User": "alice"}
    bob = {"X-Test-User": "bob"}

    preview = client.get("/api/chat/conversations/ctx-legacy01/migration", headers=alice)
    assert preview.status_code == 200, preview.text
    report = preview.json()
    assert report["action"] == migration.ACTION and report["status"] == "ready"
    assert report["source"]["provenance"] == "unknown"
    token = report["confirmation"]["token"]

    # Another user's space does not contain this conversation at all.
    assert client.get("/api/chat/conversations/ctx-legacy01/migration", headers=bob).status_code == 404
    assert client.post(
        "/api/chat/conversations/ctx-legacy01/migration",
        headers=bob,
        json={"plan_token": token, "confirm": True},
    ).status_code == 404

    # No confirmation, and a token that does not match the request.
    assert client.post(
        "/api/chat/conversations/ctx-legacy01/migration",
        headers=alice,
        json={"plan_token": token, "confirm": False},
    ).status_code == 400
    stale = client.post(
        "/api/chat/conversations/ctx-legacy01/migration",
        headers=alice,
        json={"plan_token": "0" * 64, "confirm": True},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == migration.CONFIRMATION_STALE

    applied = client.post(
        "/api/chat/conversations/ctx-legacy01/migration",
        headers=alice,
        json={"plan_token": token, "confirm": True},
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["status"] == "applied" and body["code"] == migration.APPLIED
    target_id = body["target"]["context_id"]
    assert body["reference"]["executable"] is False
    assert body["effects"]["model_calls"] == 0 and body["effects"]["tool_calls"] == 0

    # The new conversation is in the rail, the original is still legacy.
    listed = client.get("/api/chat/conversations", headers=alice).json()
    by_id = {item["id"]: item for item in listed}
    assert target_id in by_id
    assert by_id[target_id]["message_count"] == 0
    assert by_id["ctx-legacy01"]["message_count"] == 1

    again = client.get("/api/chat/conversations/ctx-legacy01/migration", headers=alice).json()
    assert again["status"] == "completed"
    assert again["target"]["context_id"] == target_id


# -- the target is a fresh chat with an explicit selection ---------------------

SELECTION = {"system_key": "main", "agent_key": "worker"}


def test_the_target_is_a_fresh_chat_with_an_explicit_selection(tmp_path):
    """The new chat picks its system/agent like any new chat - not the source's routing."""
    manager, context_id, _ = legacy_manager(tmp_path)
    manager.update_context_metadata(
        context_id, {"routed_system": "old-routed", "routed_agent": "old-agent", "title": "Old chat"}
    )
    config = make_factory(tmp_path).config
    token = migration.plan(manager, context_id, config, selection=SELECTION)["confirmation"]["token"]

    # The confirmation is bound to the selection: another one is a stale token.
    other = {"system_key": "main", "agent_key": "reviewer"}
    wrong_selection = migration.apply(
        manager, context_id, config, confirmation=token, confirm=True, selection=other
    )
    assert wrong_selection["status"] == "blocked"
    assert wrong_selection["code"] == migration.CONFIRMATION_STALE
    assert manager.list_context_ids() == [context_id]

    report = migration.apply(
        manager, context_id, config, confirmation=token, confirm=True, selection=SELECTION
    )

    assert report["status"] == "applied"
    assert report["target"]["system_key"] == SELECTION["system_key"]
    assert report["target"]["agent_key"] == SELECTION["agent_key"]
    target_id = report["target"]["context_id"]
    metadata = manager.get_context_metadata(target_id)
    # Fresh metadata: the selection a new chat records, never the source's routing.
    assert metadata["system_key"] == SELECTION["system_key"]
    assert metadata["agent_key"] == SELECTION["agent_key"]
    assert metadata["title"] == migration.TARGET_TITLE
    assert "routed_system" not in metadata and "routed_agent" not in metadata
    assert metadata[migration.RECORD_KEY]["target_selection"] == SELECTION
    assert manager.list_context_ids() == [context_id, target_id]


def test_a_used_target_stays_completed_while_its_configuration_is_the_same(tmp_path):
    """A turn writes the live selection into the metadata; the pin is what counts."""
    manager, context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    token = migration.plan(manager, context_id, config, selection=SELECTION)["confirmation"]["token"]
    target_id = migration.apply(
        manager, context_id, config, confirmation=token, confirm=True, selection=SELECTION
    )["target"]["context_id"]
    # What an ordinary turn leaves behind: the live selection and routing.
    manager.update_context_metadata(
        target_id,
        {"system_key": None, "agent_key": None, "routed_system": "main", "routed_agent": "worker"},
    )

    again = migration.plan(manager, context_id, config, selection=SELECTION)

    assert again["status"] == "completed"
    assert again["target"]["context_id"] == target_id
    assert manager.revision_manifest(target_id) is not None


def test_a_target_pinned_to_a_changed_configuration_is_refused_not_completed(tmp_path):
    manager, context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    token = migration.plan(manager, context_id, config, selection=SELECTION)["confirmation"]["token"]
    target_id = migration.apply(
        manager, context_id, config, confirmation=token, confirm=True, selection=SELECTION
    )["target"]["context_id"]
    manifest = manager.revision_manifest(target_id)
    ids_before = manager.list_context_ids()

    # The same system, edited since the bootstrap: another resolved config.
    changed = make_factory(tmp_path, max_turns=99).config

    preview = migration.plan(manager, context_id, changed, selection=SELECTION)
    applied = migration.apply(
        manager,
        context_id,
        changed,
        confirmation=preview["confirmation"]["token"] or "0" * 64,
        confirm=True,
        selection=SELECTION,
    )

    for report in (preview, applied):
        assert report["status"] == "blocked"
        assert report["code"] == migration.TARGET_INCOMPATIBLE
        assert report["target"]["context_id"] == target_id
        assert "not re-pinned" in report["reason"]
        assert "system_config_changed" in report["reason"]
    assert manager.revision_manifest(target_id) == manifest
    assert manager.list_context_ids() == ids_before


def test_the_confirmation_is_bound_to_the_target_environment(tmp_path):
    """The token covers the environment the new chat would be pinned to."""
    manager, context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    environment = revisions.environment_of(config)
    expected = revisions.digest_of(revisions.redacted(environment))

    preview = migration.plan(manager, context_id, config, selection=SELECTION)

    assert preview["status"] == "ready"
    # The digest is reported, and it is the secret-safe one.
    assert preview["target"]["environment_digest"] == expected
    assert expected.startswith("sha256:")

    # Another resolved environment, same source and same selection: another token.
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = make_factory(other_dir, max_turns=99).config
    other_digest = revisions.digest_of(revisions.redacted(revisions.environment_of(other)))
    assert other_digest != expected
    moved = migration.plan(manager, context_id, other, selection=SELECTION)
    assert moved["target"]["environment_digest"] == other_digest
    assert moved["confirmation"]["token"] != preview["confirmation"]["token"]


def test_a_preview_in_one_environment_cannot_apply_in_another(tmp_path):
    """A legacy preview for cfg1 is stale when applied for cfg2, same selection."""
    manager, context_id, path = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    changed = make_factory(tmp_path, max_turns=99).config
    before = path.read_bytes()
    ids_before = manager.list_context_ids()
    token = migration.plan(manager, context_id, config, selection=SELECTION)["confirmation"]["token"]

    stale = migration.apply(
        manager, context_id, changed, confirmation=token, confirm=True, selection=SELECTION
    )

    assert stale["status"] == "blocked"
    assert stale["code"] == migration.CONFIRMATION_STALE
    assert stale["confirmation"]["required"] is True
    assert stale["confirmation"]["accepted"] is False
    # No target, and the source is untouched: still legacy, same bytes, same ids.
    assert manager.list_context_ids() == ids_before
    assert path.read_bytes() == before
    assert manager.revision_manifest(context_id) is None
    assert manager.revision_status(context_id) == "legacy"


def test_report_limitations_never_claim_provenance_without_an_established_source():
    """A missing source section is unknown, not recorded."""
    blocked = migration._report("blocked", migration.SOURCE_UNKNOWN, "reason")
    assert blocked["source"] == {}
    assert blocked["limitations"]["provenance"] == "unknown"
    assert (
        migration._report(
            "blocked", migration.SOURCE_UNKNOWN, "reason", source={"context_id": "ctx-x"}
        )["limitations"]["provenance"]
        == "unknown"
    )
    assert (
        migration._report(
            "blocked", migration.SOURCE_UNKNOWN, "reason", source={"provenance": "unknown"}
        )["limitations"]["provenance"]
        == "unknown"
    )
    assert (
        migration._report(
            "blocked", migration.SOURCE_UNKNOWN, "reason", source={"provenance": "pinned"}
        )["limitations"]["provenance"]
        == "recorded"
    )
    assert (
        migration._report(
            "blocked", migration.SOURCE_UNKNOWN, "reason", source={"provenance": "recorded"}
        )["limitations"]["provenance"]
        == "recorded"
    )


def test_a_completed_report_keeps_the_source_provenance_unknown(tmp_path):
    manager, context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    token = migration.plan(manager, context_id, config, selection=SELECTION)["confirmation"]["token"]
    target_id = migration.apply(
        manager, context_id, config, confirmation=token, confirm=True, selection=SELECTION
    )["target"]["context_id"]

    completed = migration.plan(manager, context_id, config, selection=SELECTION)

    assert completed["status"] == "completed"
    assert completed["target"]["context_id"] == target_id
    # The record says "unknown" and the repeated report says so too.
    assert manager.get_context_metadata(target_id)[migration.RECORD_KEY]["source_provenance"] == "unknown"
    assert completed["source"]["provenance"] == "unknown"


def test_an_unknown_integrity_verdict_fails_closed(tmp_path, monkeypatch):
    config = make_factory(tmp_path).config
    manager, context_id = pinned_manager(tmp_path, config)
    ids_before = manager.list_context_ids()

    def unknown(manifest):
        return {"ok": False, "code": "revision.something_new", "reason": "a verdict from the future"}

    monkeypatch.setattr(revisions, "verify_integrity", unknown)
    report = migration.plan(manager, context_id, config, selection=SELECTION)

    assert report["status"] == "blocked"
    assert report["code"] == migration.SOURCE_INTEGRITY_UNKNOWN
    assert "revision.something_new" in report["reason"]
    assert report["source"]["provenance"] == "unknown"
    assert report["limitations"]["provenance"] == "unknown"
    assert manager.list_context_ids() == ids_before


def test_an_unknown_source_is_reported_and_nothing_is_created(tmp_path):
    manager, _context_id, _ = legacy_manager(tmp_path)
    config = make_factory(tmp_path).config
    ids_before = manager.list_context_ids()

    preview = migration.plan(manager, "ctx-missing", config, selection=SELECTION)
    applied = migration.apply(
        manager, "ctx-missing", config, confirmation="0" * 64, confirm=True, selection=SELECTION
    )

    for report in (preview, applied):
        assert report["status"] == "blocked"
        assert report["code"] == migration.SOURCE_UNKNOWN
    assert manager.list_context_ids() == ids_before


# -- over HTTP, a real user space with two routed systems --------------------

TWO_SYSTEM_AGENTS = {"alpha": "assistant", "beta": "builder"}


def _system_yaml(agent_key: str) -> str:
    return json.dumps(
        {
            "settings": {
                "default_agent": agent_key,
                "max_history": 10,
                "max_turns": 5,
                "agent_timeout": 60,
                "working_directory": ".",
                "allow_path_override": True,
                "mcp_enabled": False,
            },
            "isolation": {"enabled": False},
            "providers": {"p": {"name": "p", "base_url": "http://localhost", "api_key": "test"}},
            "models": {"m": {"name": "m", "provider": "p"}},
            "agents": {agent_key: {"name": agent_key.title(), "model": "m", "tools": []}},
        }
    )


def two_system_space(tmp_path):
    """A real UserSpace whose catalog offers two routed systems, with a legacy chat.

    ``alpha`` is the default and its agent is ``assistant``; ``beta`` is another
    system with its own agent, so a source routed to it is exactly the case the
    server used to pin while the new chat routed elsewhere.
    """
    from web_chat.deployment import Deployment
    from web_chat.space import SpaceLayout, UserSpace

    root = tmp_path / "alice"
    layout = SpaceLayout.under(root)
    layout.workspace.mkdir(parents=True)
    _seeded_legacy(layout.conversations, "ctx-legacy01")

    (tmp_path / "alpha.yaml").write_text(_system_yaml("assistant"), encoding="utf-8")
    (tmp_path / "beta.yaml").write_text(_system_yaml("builder"), encoding="utf-8")
    catalog = {
        "settings": {"max_history": 10, "working_directory": "."},
        "providers": {"p": {"name": "p", "base_url": "http://localhost", "api_key": "test"}},
        "models": {"m": {"name": "m", "provider": "p"}},
        "routing": {
            "model": "m",
            "default_system": "alpha",
            "systems": {
                "alpha": {"config": "alpha.yaml", "description": "General assistance"},
                "beta": {"config": "beta.yaml", "description": "Building and changing code"},
            },
        },
    }
    (tmp_path / "catalog.yaml").write_text(json.dumps(catalog), encoding="utf-8")
    deployment = Deployment(routing_path=str(tmp_path / "catalog.yaml"))
    space = UserSpace(deployment, user_id="alice", layout=layout, chat_spaces=True)
    return space, layout


def real_web_client(space):
    from fastapi.testclient import TestClient

    from web_chat.identity import User
    from web_chat.server import WebChatServer
    from web_chat.spaces import SpacePool

    async def identify(connection):
        return User(id="alice", username="alice", role="user")

    server = WebChatServer(
        SimpleNamespace(voice_enabled=lambda: False),
        SpacePool(lambda user_id: space),
        identify=identify,
        warm_user=None,
    )
    return TestClient(server.app, headers={"Origin": "http://testserver"})


def test_a_real_two_system_space_previews_without_building_anything(tmp_path):
    space, layout = two_system_space(tmp_path)
    client = real_web_client(space)

    preview = client.get("/api/chat/conversations/ctx-legacy01/migration", headers={"X-Test-User": "alice"})

    assert preview.status_code == 200, preview.text
    report = preview.json()
    assert report["status"] == "ready", report
    # The current default system and its default agent, explicitly.
    assert report["target"]["system_key"] == "alpha"
    assert report["target"]["agent_key"] == "assistant"
    assert report["source"]["provenance"] == "unknown"
    assert report["confirmation"]["token"]
    # No chat runtime was built: no directory, no container, no factory.
    assert space._chat_runtimes == {}
    assert space.registry.built_factories() == {}
    assert not layout.chat_spaces.exists() or list(layout.chat_spaces.iterdir()) == []


def test_a_real_two_system_space_targets_a_fresh_chat_the_first_turn_can_run(tmp_path):
    from web_chat.session import ChatSession

    space, layout = two_system_space(tmp_path)
    client = real_web_client(space)
    headers = {"X-Test-User": "alice"}
    manager = space.context_manager()

    preview = client.get("/api/chat/conversations/ctx-legacy01/migration", headers=headers).json()
    token = preview["confirmation"]["token"]
    applied = client.post(
        "/api/chat/conversations/ctx-legacy01/migration",
        headers=headers,
        json={"plan_token": token, "confirm": True},
    )
    assert applied.status_code == 200, applied.text
    report = applied.json()
    assert report["status"] == "applied"
    target_id = report["target"]["context_id"]
    assert target_id != "ctx-legacy01"

    # A fresh chat: the explicit selection of a new chat, its own space, and a
    # record that keeps the legacy provenance unknown.
    metadata = manager.get_context_metadata(target_id)
    assert metadata["system_key"] == "alpha"
    assert metadata["agent_key"] == "assistant"
    assert metadata["title"] == migration.TARGET_TITLE
    assert metadata["chat_space"] == target_id
    assert "routed_system" not in metadata and "routed_agent" not in metadata
    assert metadata[migration.RECORD_KEY]["source_provenance"] == "unknown"
    # The space is metadata only: the migration itself made no directory.
    assert not (layout.chat_spaces / target_id).exists()

    # The real pre-turn revision gate of a chat session allows the first turn.
    fake_session = SimpleNamespace(space=space, manager=manager, context_id=target_id)
    asyncio.run(ChatSession._check_revision(fake_session, None))

    # An ordinary first turn resolves that exact system and agent - not another
    # system - and the revision gate admits it. This is the turn that builds the
    # chat's normal space: a runtime, not the migration.
    resolution = asyncio.run(
        space.resolve_turn(
            "hello", system_key=metadata["system_key"], agent_key=metadata["agent_key"],
            context_id=target_id,
        )
    )
    assert (resolution.system, resolution.agent) == ("alpha", "assistant")
    assert resolution.routed_system is False and resolution.routed_agent is False
    manifest = revisions.admit_turn(manager, target_id, resolution.config)
    assert manifest is not None and manifest["context_id"] == target_id
    # The first turn has the normal new chat's space, as a chat made by hand.
    assert (layout.chat_spaces / target_id).is_dir()

    # Idempotent: the same target, nothing new, still the recorded provenance.
    again = client.get("/api/chat/conversations/ctx-legacy01/migration", headers=headers).json()
    assert again["status"] == "completed"
    assert again["target"]["context_id"] == target_id
    assert again["source"]["provenance"] == "unknown"
    second = client.post(
        "/api/chat/conversations/ctx-legacy01/migration",
        headers=headers,
        json={"plan_token": token, "confirm": True},
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "completed"
    assert set(manager.list_context_ids()) == {"ctx-legacy01", target_id}
