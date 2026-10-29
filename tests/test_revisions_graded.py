"""Graded revision pinning: old conversations continue instead of being refused.

A conversation is refused only for a real incompatibility (a record this
runtime cannot read, an altered manifest, a changed contract, an unresolved
system). A Grid version change, a resolved system configuration change or a
conversation stored before pinning existed are *continuations*: the turn runs,
the conversation is re-pinned to the current revision, the previous pin is
appended to an append-only history, and the client is told with a typed notice
that is not an error.
"""

from __future__ import annotations

import json

import pytest

from core import __version__ as GRID_VERSION
from core import revisions
from core.context import ContextManager
from tests.test_revision_compatibility import (
    _Socket,
    _message_count,
    _resigned,
    _seeded_legacy,
    _stored_manifest,
    _web_space,
    make_factory,
)
from tests.test_run_agent import ScriptedRunner, use


@pytest.fixture
def factory(tmp_path):
    return make_factory(tmp_path)


async def test_a_changed_system_configuration_continues_and_appends_a_revision(factory, tmp_path):
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    pinned_before = factory.context_manager.revision_manifest(context_id)
    before = _message_count(factory.context_manager, context_id)
    assert factory.context_manager.revision_history(context_id) == []

    factory.config.config.settings.max_turns = 6  # the resolved configuration moved on

    runner = ScriptedRunner("Second answer.")
    with use(runner):
        await factory.run_agent("worker", "Second", context_id=context_id)

    # The turn ran on the stored history.
    assert runner.calls
    assert _message_count(factory.context_manager, context_id) > before

    # Re-pinned to this configuration, with the previous pin kept.
    pinned_after = factory.context_manager.revision_manifest(context_id)
    assert pinned_after["system"]["digest"] != pinned_before["system"]["digest"]
    assert revisions.verify_manifest(
        pinned_after, revisions.environment_of(factory.config)
    )["ok"] is True
    history = factory.context_manager.revision_history(context_id)
    assert len(history) == 1
    assert history[0]["code"] == revisions.SYSTEM_CONFIG_CHANGED
    assert history[0]["status"] == "pinned"
    assert history[0]["manifest"] == pinned_before

    # Persisted with the conversation, not only in memory.
    stored = json.loads((tmp_path / "logs" / "context.json").read_text(encoding="utf-8"))
    assert stored["contexts"][context_id]["revision"] == pinned_after
    assert stored["contexts"][context_id]["revision_history"] == history


async def test_a_grid_version_change_continues(factory):
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    pinned_before = factory.context_manager.revision_manifest(context_id)
    factory.context_manager._contexts[context_id]["revision"] = _resigned(
        _stored_manifest(factory, context_id), grid_version="0.0.1"
    )
    before = _message_count(factory.context_manager, context_id)

    runner = ScriptedRunner("Second answer.")
    with use(runner):
        await factory.run_agent("worker", "Second", context_id=context_id)

    assert runner.calls
    assert _message_count(factory.context_manager, context_id) > before
    pinned_after = factory.context_manager.revision_manifest(context_id)
    assert pinned_after["grid_version"] == GRID_VERSION
    assert pinned_after["digest"] != pinned_before["digest"]
    history = factory.context_manager.revision_history(context_id)
    assert [entry["code"] for entry in history] == [revisions.GRID_VERSION_CHANGED]
    assert history[0]["grid_version"] == "0.0.1"
    assert history[0]["manifest"]["grid_version"] == "0.0.1"


async def test_a_legacy_context_is_pinned_and_continues(tmp_path):
    path = tmp_path / "context.json"
    context_id = _seeded_legacy(path)
    manager = ContextManager(max_history=15, persist_path=str(path), create_initial_context=False)
    factory = make_factory(tmp_path, manager=manager)
    assert manager.revision_status(context_id) == "legacy"

    runner = ScriptedRunner("Answer for old history.")
    with use(runner):
        await factory.run_agent("worker", "Continue this", context_id=context_id)

    # The stored history was read and the turn ran on it.
    assert runner.calls
    assert manager.get_context_messages(context_id, include_full=True)["messages"][0]["content"] == "Read me"
    # Pinned now, with the legacy state recorded rather than forgotten.
    manifest = manager.revision_manifest(context_id)
    assert manifest is not None and revisions.manifest_digest_ok(manifest)
    assert manifest["grid_version"] == GRID_VERSION
    assert manager.revision_status(context_id) == "pinned"
    history = manager.revision_history(context_id)
    assert len(history) == 1
    assert history[0]["code"] == revisions.LEGACY
    assert history[0]["status"] == "legacy"
    assert history[0]["manifest"] is None
    assert history[0]["digest"] is None

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["contexts"][context_id]["revision"] == manifest
    assert stored["contexts"][context_id]["revision_history"] == history


async def test_a_contract_change_is_still_refused(factory):
    """A contract changed at the same manifest version is a real incompatibility."""
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    before = _message_count(factory.context_manager, context_id)
    contracts = {
        **{name: revisions.contract_section(name) for name in revisions.CONTRACTS if name != "history"},
        "history": {**revisions.contract_section("history"), "digest": "sha256:" + "0" * 64},
    }
    factory.context_manager._contexts[context_id]["revision"] = _resigned(
        _stored_manifest(factory, context_id), contracts=contracts
    )

    runner = ScriptedRunner("Never reached.")
    with use(runner):
        with pytest.raises(revisions.ContextCompatibilityError) as refused:
            await factory.run_agent("worker", "Second", context_id=context_id)

    assert refused.value.code == revisions.CONTRACT_CHANGED
    assert refused.value.details["code"] == revisions.CONTRACT_CHANGED
    assert runner.calls == []
    assert _message_count(factory.context_manager, context_id) == before
    # A refusal writes nothing: no re-pin, no history entry.
    assert factory.context_manager.revision_history(context_id) == []


async def test_an_unsupported_contract_version_is_still_refused(factory):
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    contracts = {
        **{name: revisions.contract_section(name) for name in revisions.CONTRACTS if name != "history"},
        "history": {**revisions.contract_section("history"), "version": 2},
    }
    factory.context_manager._contexts[context_id]["revision"] = _resigned(
        _stored_manifest(factory, context_id), contracts=contracts
    )

    runner = ScriptedRunner("Never reached.")
    with use(runner):
        with pytest.raises(revisions.ContextCompatibilityError) as refused:
            await factory.run_agent("worker", "Second", context_id=context_id)
    assert refused.value.code == revisions.CONTRACT_VERSION_UNSUPPORTED
    assert runner.calls == []


async def test_the_revision_history_is_append_only(factory):
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    first = factory.context_manager.revision_manifest(context_id)

    factory.config.config.settings.max_turns = 6
    with use(ScriptedRunner("Two.")):
        await factory.run_agent("worker", "Second", context_id=context_id)
    second = factory.context_manager.revision_manifest(context_id)

    factory.config.config.settings.max_turns = 7
    with use(ScriptedRunner("Three.")):
        await factory.run_agent("worker", "Third", context_id=context_id)

    history = factory.context_manager.revision_history(context_id)
    assert [entry["manifest"]["digest"] for entry in history] == [first["digest"], second["digest"]]
    assert all(entry["code"] == revisions.SYSTEM_CONFIG_CHANGED for entry in history)
    assert factory.context_manager.revision_manifest(context_id)["digest"] != second["digest"]


def test_a_pinned_conversation_without_drift_gets_no_notice(factory):
    manager = factory.context_manager
    context_id = manager.start_new_context()
    state = revisions.assess(manager, context_id, factory.config)
    assert state["status"] == "new" and state["action"] == revisions.PIN and state["notice"] is None

    revisions.admit_turn(manager, context_id, factory.config)
    settled = revisions.assess(manager, context_id, factory.config)
    assert settled["status"] == "pinned" and settled["notice"] is None


def test_the_notice_is_typed_and_not_an_error(factory):
    """The domain notice and the wire event: a code, and a sentence, no error."""
    from web_chat.session import revision_notice_event

    manager = factory.context_manager
    context_id = manager.start_new_context()
    factory.config.config.settings.max_turns = 6
    revisions.admit_turn(manager, context_id, factory.config)
    factory.config.config.settings.max_turns = 7

    notices: list = []
    revisions.admit_turn(manager, context_id, factory.config, notices=notices)
    assert len(notices) == 1
    notice = notices[0]
    assert notice["code"] == revisions.SYSTEM_CONFIG_CHANGED
    assert isinstance(notice["reason"], str) and notice["reason"]
    assert notice["context_id"] == context_id
    assert notice["details"]["changes"] == [revisions.SYSTEM_CONFIG_CHANGED]
    assert notice["details"]["previous_digest"]
    assert notice["details"]["grid_version"] == GRID_VERSION

    event = revision_notice_event(notice)
    assert event == {
        "type": "revision_notice",
        "code": revisions.SYSTEM_CONFIG_CHANGED,
        "content": notice["reason"],
        "context_id": context_id,
        "details": notice["details"],
    }
    assert event["type"] != "error"


async def test_the_web_gate_reports_a_repin_notice_instead_of_an_error(tmp_path):
    """The pre-turn check admits a drifted conversation and returns its notice."""
    from web_chat.session import ChatSession, revision_notice_event

    factory = make_factory(tmp_path)
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    manager = factory.context_manager
    context_id = manager.get_current_context_id()
    factory.config.config.settings.max_turns = 6

    space, calls = _web_space(manager, factory.config)
    session = ChatSession(space, _Socket(), context_id)

    notices = await session._check_revision(None)
    assert [notice["code"] for notice in notices] == [revisions.SYSTEM_CONFIG_CHANGED]
    event = revision_notice_event(notices[0])
    assert event["type"] == "revision_notice" and event["context_id"] == context_id
    # The check writes nothing and does no worked work of its own.
    assert calls == {"quota": 0, "warm": 0, "metadata": 0, "resolve": 0}


def test_the_notice_is_sent_before_the_turn_starts():
    """Structural: the notice goes out after admission is decided, before the turn."""
    import inspect

    from web_chat.session import ChatSession

    source = inspect.getsource(ChatSession._start)
    assert source.index("_check_revision") < source.index("space.admit_turn()")
    assert source.index("space.admit_turn()") < source.index("revision_notice_event(notice)")
    assert source.index("revision_notice_event(notice)") < source.index("AgentTurn(")
