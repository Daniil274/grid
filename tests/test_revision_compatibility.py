"""Revision compatibility: pinned conversations, graded admission.

A conversation is pinned to a revision the first time a turn writes into it and
judged before every later turn. Only a real incompatibility is refused with a
machine-readable reason (a manifest this runtime cannot read, an altered one, a
changed contract, an unresolved system). A Grid version, Python runtime or
resolved system configuration that moved on, and a conversation stored before
pinning existed (legacy), are continuations: the turn runs on the stored
history, the conversation is re-pinned to the current revision, the previous
pin is appended to an append-only ``revision_history``, and the client is told
with a typed notice that is not an error.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from agents import SQLiteSession

from core import __version__ as GRID_VERSION
from core import revisions
from core.agent_factory import AgentFactory
from core.config import Config
from core.context import ContextManager
from core.conversation_store import ConversationCodec, ConversationStore
from tests.test_run_agent import ScriptedRunner, use

ROOT = Path(__file__).resolve().parents[1]

#: Values that must never reach a stored manifest.
SECRET = "sk-live-0123456789abcdefghijklmn"
PROMPT = "PRIVATE PROMPT: you are the worker"


def _config_text(tmp_path: Path, **settings) -> str:
    config = {
        "settings": {
            "default_agent": "worker",
            "max_turns": 5,
            "agent_timeout": 30,
            "working_directory": str(tmp_path / "work"),
            "logs_directory": str(tmp_path / "logs"),
            "mcp_enabled": False,
            "agent_logging": {"enabled": False},
            **settings,
        },
        "providers": {"p": {"name": "p", "base_url": "https://llm.test/v1", "api_key": SECRET}},
        "models": {"m": {"name": "m", "provider": "p", "context_window": 100000}},
        "prompt_templates": {"base": PROMPT},
        "agents": {
            "worker": {"name": "Worker", "model": "m", "tools": [], "base_prompt": "base"},
            "reviewer": {"name": "Reviewer", "model": "m", "tools": [], "base_prompt": "base"},
        },
    }
    (tmp_path / "work").mkdir(exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(path)


def make_factory(tmp_path: Path, *, manager: ContextManager | None = None, **settings) -> AgentFactory:
    factory = AgentFactory(Config(_config_text(tmp_path, **settings)), tracing_level=None)
    if manager is not None:
        factory.context_manager = manager
    sessions = {}
    factory._get_agent_session = lambda agent_key, context_id: sessions.setdefault(
        (agent_key, context_id), SQLiteSession(f"{agent_key}-{context_id}")
    )
    factory.sessions = sessions
    return factory


@pytest.fixture
def factory(tmp_path):
    return make_factory(tmp_path)


def _seeded_legacy(path: Path, context_id: str = "ctx-legacy01") -> str:
    """A conversation stored before revisions existed: no manifest."""
    path.write_text(
        json.dumps(
            {
                "active_context_id": context_id,
                "contexts": {
                    context_id: {
                        "conversation_history": [
                            {
                                "role": "user",
                                "content": "Read me",
                                "timestamp": "2026-01-01T00:00:00",
                                "metadata": {},
                            }
                        ],
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
    return context_id


def _resigned(manifest: dict, **changes) -> dict:
    """A manifest with *changes* applied and its digest recomputed (a forgery)."""
    from copy import deepcopy

    forged = deepcopy(manifest)
    forged.pop("digest", None)
    forged.update(changes)
    forged["digest"] = revisions.digest_of(forged)
    return forged


def _stored_manifest(factory: AgentFactory, context_id: str) -> dict:
    """The manifest as it sits in the manager's own memory."""
    return factory.context_manager._contexts[context_id]["revision"]


def _message_count(manager: ContextManager, context_id: str) -> int:
    view = manager.get_context_messages(context_id, include_full=True) or {}
    return len(view.get("messages") or [])


# -- pinning -------------------------------------------------------------------


async def test_a_new_conversation_is_pinned_and_the_manifest_is_secret_free(factory, tmp_path):
    with use(ScriptedRunner("Done.")):
        await factory.run_agent("worker", "Fix it")

    context_id = factory.context_manager.get_current_context_id()
    manifest = factory.context_manager.revision_manifest(context_id)

    assert manifest is not None
    assert revisions.manifest_digest_ok(manifest)
    assert manifest["manifest_version"] == revisions.MANIFEST_VERSION
    assert manifest["grid_version"] == GRID_VERSION
    assert set(manifest["contracts"]) == set(revisions.CONTRACTS)
    assert manifest["system"]["status"] == "resolved"
    assert manifest["system"]["defaults"]["default_agent"] == "worker"
    assert manifest["system"]["defaults"]["agents"]["worker"]["model"] == "m"

    # Persisted with the conversation in the JSON file the CLI uses.
    stored = json.loads((tmp_path / "logs" / "context.json").read_text(encoding="utf-8"))
    assert stored["contexts"][context_id]["revision"] == manifest

    # No prompt, no credential, no environment value in the manifest itself.
    text = json.dumps(manifest)
    assert SECRET not in text
    assert PROMPT not in text
    assert "api_key" not in text.lower()
    # The projection keeps keys and resolved limits only.
    assert manifest["system"]["defaults"].keys() == {
        "default_agent", "max_history", "max_turns", "agent_timeout",
        "action_policy", "agents", "models", "tools",
    }


async def test_pinning_is_immutable(factory):
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    pinned = factory.context_manager.revision_manifest(context_id)

    with use(ScriptedRunner("Two.")):
        await factory.run_agent("worker", "Second", context_id=context_id)
    assert factory.context_manager.revision_manifest(context_id) == pinned

    # A second pin is refused, whoever asks.
    replacement = revisions.build_manifest(context_id, revisions.environment_of(factory.config))
    replacement["pinned_at"] = "2099-01-01T00:00:00"
    replacement["digest"] = revisions.digest_of(
        {key: value for key, value in replacement.items() if key != "digest"}
    )
    assert factory.context_manager.pin_revision(context_id, replacement) is False
    assert factory.context_manager.revision_manifest(context_id) == pinned


# -- graded admission: continuations -------------------------------------------


async def test_a_changed_system_configuration_continues_on_the_stored_history(factory):
    """A moved-on configuration is not incompatibility: the next turn runs.

    Only a real incompatibility is refused before external operations. A
    configuration change is a continuation: the stored history is kept, the
    conversation is re-pinned and the previous pin is appended to its history.
    """
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    before = _message_count(factory.context_manager, context_id)
    pinned_before = factory.context_manager.revision_manifest(context_id)

    factory.config.config.settings.max_turns = 6  # resolved configuration changed

    runner = ScriptedRunner("Second answer.")
    with use(runner):
        await factory.run_agent("worker", "Second", context_id=context_id)

    # The turn ran, on the history it already had, and stored its request.
    assert runner.calls
    assert _message_count(factory.context_manager, context_id) > before

    # Re-pinned to this configuration, with the previous pin kept, not lost.
    pinned_after = factory.context_manager.revision_manifest(context_id)
    assert pinned_after["system"]["digest"] != pinned_before["system"]["digest"]
    assert revisions.verify_manifest(
        pinned_after, revisions.environment_of(factory.config)
    )["ok"] is True
    history = factory.context_manager.revision_history(context_id)
    assert [entry["code"] for entry in history] == [revisions.SYSTEM_CONFIG_CHANGED]
    assert history[0]["status"] == "pinned"
    assert history[0]["manifest"] == pinned_before


async def test_a_changed_prompt_continues_and_is_repinned(tmp_path):
    """The prompt is part of the system digest: a new one re-pins, never refuses."""
    factory = make_factory(tmp_path)
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    pinned_before = factory.context_manager.revision_manifest(context_id)

    config_file = Path(factory.config.config_path)
    document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    document["prompt_templates"]["base"] = "A different prompt"
    config_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    factory.config = Config(str(config_file))
    factory._agent_sessions = {}

    runner = ScriptedRunner("Second answer.")
    with use(runner):
        await factory.run_agent("worker", "Second", context_id=context_id)

    assert runner.calls
    pinned_after = factory.context_manager.revision_manifest(context_id)
    assert pinned_after["system"]["digest"] != pinned_before["system"]["digest"]
    assert [
        entry["code"] for entry in factory.context_manager.revision_history(context_id)
    ] == [revisions.SYSTEM_CONFIG_CHANGED]


async def test_a_tampered_manifest_is_refused(factory):
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()

    _stored_manifest(factory, context_id)["system"]["digest"] = "sha256:" + "0" * 64

    with use(ScriptedRunner("Never reached.")):
        with pytest.raises(revisions.ContextCompatibilityError) as refused:
            await factory.run_agent("worker", "Second", context_id=context_id)
    assert refused.value.code == revisions.TAMPERED


@pytest.mark.parametrize(
    "changes, code",
    [
        ({"manifest_version": 99}, revisions.UNKNOWN_MANIFEST),
        (
            {
                "contracts": {
                    **{
                        name: revisions.contract_section(name)
                        for name in revisions.CONTRACTS
                        if name != "history"
                    },
                    "history": {**revisions.contract_section("history"), "version": 2},
                }
            },
            revisions.CONTRACT_VERSION_UNSUPPORTED,
        ),
    ],
)
async def test_a_manifest_this_runtime_cannot_read_fails_closed(factory, changes, code):
    """A future format version or an unsupported contract is a real refusal."""
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()

    forged = _resigned(factory.context_manager.revision_manifest(context_id), **changes)
    factory.context_manager._contexts[context_id]["revision"] = forged

    with use(ScriptedRunner("Never reached.")):
        with pytest.raises(revisions.ContextCompatibilityError) as refused:
            await factory.run_agent("worker", "Second", context_id=context_id)
    assert refused.value.code == code
    # A refusal writes nothing: no re-pin, no history entry.
    assert factory.context_manager.revision_history(context_id) == []


async def test_a_pinned_grid_version_change_continues(factory):
    """Another Grid version pinned the conversation, but its history is readable."""
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    before = _message_count(factory.context_manager, context_id)
    factory.context_manager._contexts[context_id]["revision"] = _resigned(
        _stored_manifest(factory, context_id), grid_version="0.0.1"
    )

    runner = ScriptedRunner("Second answer.")
    with use(runner):
        await factory.run_agent("worker", "Second", context_id=context_id)

    assert runner.calls
    assert _message_count(factory.context_manager, context_id) > before
    pinned_after = factory.context_manager.revision_manifest(context_id)
    assert pinned_after["grid_version"] == GRID_VERSION
    history = factory.context_manager.revision_history(context_id)
    assert [entry["code"] for entry in history] == [revisions.GRID_VERSION_CHANGED]
    assert history[0]["grid_version"] == "0.0.1"


# -- legacy conversations ------------------------------------------------------


async def test_a_legacy_conversation_is_readable_and_adopted_by_a_turn(tmp_path):
    """A conversation stored before pinning existed continues, and is pinned.

    Its history was written by a runtime that kept no manifest, but the history
    itself is intact, so the next turn runs on it. Adoption is explicit: the
    conversation is pinned to the current revision and the legacy state is
    recorded in the append-only history rather than forgotten.
    """
    path = tmp_path / "context.json"
    context_id = _seeded_legacy(path)
    manager = ContextManager(max_history=15, persist_path=str(path), create_initial_context=False)
    factory = make_factory(tmp_path, manager=manager)

    # Reading the history still works.
    history = manager.get_context_messages(context_id, include_full=True)
    assert history["messages"][0]["content"] == "Read me"
    assert manager.revision_status(context_id) == "legacy"

    runner = ScriptedRunner("Answer for old history.")
    with use(runner):
        await factory.run_agent("worker", "Continue this", context_id=context_id)

    # The turn ran, and the stored history is still the one it started from.
    assert runner.calls
    assert manager.get_context_messages(context_id, include_full=True)["messages"][0]["content"] == "Read me"
    assert manager.revision_status(context_id) == "pinned"
    manifest = manager.revision_manifest(context_id)
    assert manifest is not None
    assert revisions.manifest_digest_ok(manifest)
    assert manifest["grid_version"] == GRID_VERSION

    # The legacy state is recorded, "no manifest" included.
    entries = manager.revision_history(context_id)
    assert [entry["code"] for entry in entries] == [revisions.LEGACY]
    assert entries[0]["status"] == "legacy"
    assert entries[0]["manifest"] is None
    assert entries[0]["digest"] is None

    # Persisted with the conversation, not only in memory.
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["contexts"][context_id]["revision"] == manifest
    assert stored["contexts"][context_id]["revision_history"] == entries


async def test_continue_runs_through_the_same_gate(tmp_path):
    """The Continue button is judged like any other turn, never exempt from it.

    A legacy conversation is a continuation, so it is admitted and pinned; a
    conversation with a changed contract is refused on this path too.
    """
    from utils.exceptions import AgentError

    path = tmp_path / "context.json"
    context_id = _seeded_legacy(path)
    manager = ContextManager(max_history=15, persist_path=str(path), create_initial_context=False)
    factory = make_factory(tmp_path, manager=manager)

    # Not a revision refusal: the gate admits the legacy conversation *before*
    # the turn finds that there is no interrupted turn to continue.
    with pytest.raises(AgentError):
        await factory.continue_agent("worker", context_id)
    assert manager.revision_status(context_id) == "pinned"
    assert [entry["code"] for entry in manager.revision_history(context_id)] == [revisions.LEGACY]

    # A real incompatibility is still refused here, with its machine code.
    contracts = {
        **{name: revisions.contract_section(name) for name in revisions.CONTRACTS if name != "history"},
        "history": {**revisions.contract_section("history"), "digest": "sha256:" + "0" * 64},
    }
    manager._contexts[context_id]["revision"] = _resigned(
        manager.revision_manifest(context_id), contracts=contracts
    )
    with pytest.raises(revisions.ContextCompatibilityError) as refused:
        await factory.continue_agent("worker", context_id)
    assert refused.value.code == revisions.CONTRACT_CHANGED


async def test_an_empty_stored_conversation_is_adopted_by_a_turn(tmp_path):
    """An empty chat reopened after a restart is legacy, and one turn pins it.

    Its missing manifest is stored state, so the conversation is not silently
    treated as new: the adoption goes through the graded path and is recorded
    in its revision history. A chat created in this process pins as usual.
    """
    path = tmp_path / "context.json"
    context_id = "ctx-empty01"
    path.write_text(
        json.dumps(
            {
                "active_context_id": context_id,
                "contexts": {
                    context_id: {
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
    manager = ContextManager(max_history=15, persist_path=str(path), create_initial_context=False)
    factory = make_factory(tmp_path, manager=manager)
    assert manager.revision_status(context_id) == "legacy"

    runner = ScriptedRunner("Hi there.")
    with use(runner):
        await factory.run_agent("worker", "Hi", context_id=context_id)
    assert runner.calls
    assert manager.revision_status(context_id) == "pinned"
    assert revisions.manifest_digest_ok(manager.revision_manifest(context_id))
    entries = manager.revision_history(context_id)
    assert [entry["code"] for entry in entries] == [revisions.LEGACY]
    assert entries[0]["status"] == "legacy"


async def test_a_branch_is_judged_on_its_own_inherited_revision(factory):
    """A branch carries the parent's pin and continues on it, on its own.

    The branch is not silently re-pinned at the fork (it inherits the parent's
    manifest byte for byte), and a turn on it continues: the branch - never the
    parent - is re-pinned, and the parent is left exactly as it was.
    """
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    parent = factory.context_manager.get_current_context_id()
    user_message = next(
        message
        for message in factory.context_manager._contexts[parent]["conversation"]
        if message.role == "user"
    )
    parent_manifest = factory.context_manager.revision_manifest(parent)

    factory.config.config.settings.max_turns = 7  # the configuration moved on
    branch, _slot = factory.context_manager.fork_context(parent, user_message.metadata["message_id"])

    # The branch carries the parent's history and therefore its revision.
    branch_manifest = factory.context_manager.revision_manifest(branch)
    assert branch_manifest["forked_from"] == parent
    assert branch_manifest["grid_version"] == parent_manifest["grid_version"]
    assert branch_manifest["system"] == parent_manifest["system"]
    assert revisions.manifest_digest_ok(branch_manifest)
    runner = ScriptedRunner("Edited answer.")
    with use(runner):
        await factory.run_agent("worker", "Edited", context_id=branch)
    assert runner.calls

    # The branch continued on the history it inherited and was re-pinned;
    # the parent was not touched by its child's turn.
    assert factory.context_manager.revision_manifest(parent) == parent_manifest
    assert factory.context_manager.revision_history(parent) == []
    assert [
        entry["code"] for entry in factory.context_manager.revision_history(branch)
    ] == [revisions.SYSTEM_CONFIG_CHANGED]
    assert factory.context_manager.revision_manifest(branch)["system"]["digest"] != parent_manifest["system"]["digest"]


def test_the_gate_sits_before_any_external_operation():
    """TurnRunner._run_turn admits the revision before it stores or calls out."""
    import inspect

    from core.factory.turns import TurnRunner

    source = inspect.getsource(TurnRunner._run_turn)
    gate = source.index("admit_turn(self.context_manager")
    assert gate < source.index("recover_abandoned_turn")
    assert gate < source.index("append_message_to")
    assert gate < source.index("_run_with_retries")


# -- encrypted conversation store ---------------------------------------------


async def test_the_encrypted_store_carries_the_manifest(tmp_path):
    directory = tmp_path / "conversations"
    codec = ConversationCodec.from_secrets("test-secrets-key")
    manager = ContextManager(
        max_history=15,
        create_initial_context=True,
        store=ConversationStore(directory, codec),
    )
    factory = make_factory(tmp_path, manager=manager)

    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = manager.get_current_context_id()
    manager.save(strict=True)

    # The sealed file holds no readable prompt or credential.
    sealed = b"".join(path.read_bytes() for path in directory.glob("*.grc"))
    assert SECRET.encode() not in sealed
    assert PROMPT.encode() not in sealed

    reloaded = ContextManager(
        max_history=15,
        create_initial_context=False,
        store=ConversationStore(directory, codec),
    )
    assert reloaded.revision_status(context_id) == "pinned"
    pinned = reloaded.revision_manifest(context_id)
    assert revisions.manifest_digest_ok(pinned)
    assert revisions.verify_manifest(pinned, revisions.environment_of(factory.config))["ok"] is True


# -- release manifest ----------------------------------------------------------


def test_the_release_manifest_matches_the_code():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "release_check.py"), "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, report["failed"]
    assert report["ok"] is True
    assert report["failed"] == []


# -- regression: refusals leave no trace of a turn -----------------------------


async def test_a_refused_turn_records_no_execution_and_no_journal_entry(factory):
    """C2: a revision refusal is not a failed turn - nothing is journaled."""
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    _stored_manifest(factory, context_id)["system"]["digest"] = "sha256:" + "0" * 64
    before = len(factory.context_manager._contexts[context_id]["executions"])
    metadata_before = factory.context_manager.get_all_metadata(context_id=context_id)

    with use(ScriptedRunner("Never reached.")):
        with pytest.raises(revisions.ContextCompatibilityError):
            await factory.run_agent("worker", "Second", context_id=context_id)

    assert len(factory.context_manager._contexts[context_id]["executions"]) == before
    metadata_after = factory.context_manager.get_all_metadata(context_id=context_id)
    assert json.dumps(metadata_after.get("pending_agent_run"), sort_keys=True, default=str) == json.dumps(
        metadata_before.get("pending_agent_run"), sort_keys=True, default=str
    )
    assert metadata_after.get("runtime_events") == metadata_before.get("runtime_events")


# -- regression: the gate validates before it pins -----------------------------


def test_an_unresolved_system_is_never_pinned(tmp_path):
    """C3: validate the environment before writing a manifest, not after."""
    from types import SimpleNamespace

    manager = ContextManager(max_history=15)
    context_id = manager.start_new_context()
    config_without_system = SimpleNamespace(config=None)

    with pytest.raises(revisions.ContextCompatibilityError) as refused:
        revisions.admit_turn(manager, context_id, config_without_system)

    assert refused.value.code == revisions.SYSTEM_UNRESOLVED
    assert manager.revision_manifest(context_id) is None
    assert manager.revision_status(context_id) == "new"


# -- regression: no silent bypass ---------------------------------------------


def test_an_unknown_conversation_fails_closed(tmp_path):
    """C4: a context this manager does not know is refused, not pinned."""
    factory = make_factory(tmp_path)
    with pytest.raises(revisions.ContextCompatibilityError) as refused:
        revisions.admit_turn(factory.context_manager, "ctx-missing", factory.config)
    assert refused.value.code == revisions.NO_CONTEXT


def test_a_manager_without_a_revision_api_fails_closed(tmp_path):
    """C4: an object that keeps no revisions cannot be judged, unless it says so."""
    from types import SimpleNamespace

    factory = make_factory(tmp_path)
    with pytest.raises(revisions.ContextCompatibilityError) as refused:
        revisions.admit_turn(SimpleNamespace(), "ctx-1", factory.config)
    assert refused.value.code == revisions.NO_CONTEXT

    explicit = SimpleNamespace(revision_trusted_ephemeral=True)
    assert revisions.admit_turn(explicit, "ctx-1", factory.config) is None


# -- regression: malformed and future manifests are decisions, not crashes -----


@pytest.mark.parametrize(
    "manifest",
    [
        ["not", "an", "object"],
        {"manifest_version": 1},
        {"digest": "sha256:bad", "manifest_version": 1},
        {
            "digest": "sha256:" + "0" * 64,
            "manifest_version": "2",
            "grid_version": GRID_VERSION,
            "contracts": {},
            "platform": {},
            "system": {},
        },
        {
            "digest": "sha256:" + "0" * 64,
            "manifest_version": 99,
            "grid_version": GRID_VERSION,
            "contracts": [],
            "platform": "linux",
            "system": [],
        },
        {
            "digest": "sha256:" + "0" * 64,
            "manifest_version": 1,
            "grid_version": GRID_VERSION,
            "contracts": {"history": ["not", "an", "object"]},
            "platform": {"python_series": 3.11},
            "system": "resolved",
        },
    ],
)
def test_a_malformed_manifest_returns_a_machine_decision(manifest):
    """C6: any type at any level is answered, never an AttributeError."""
    decision = revisions.verify_manifest(manifest, revisions.environment_of(None))
    assert decision["ok"] is False
    assert isinstance(decision["code"], str) and decision["code"].startswith("revision.")
    assert isinstance(decision["reason"], str)
    # Inspectors stay readable too.
    assert isinstance(revisions.manifest_summary(manifest), dict)


async def test_a_manifest_of_another_conversation_is_refused(factory):
    """C6: a copied manifest is bound to its conversation, not to this one."""
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()
    copied = _resigned(_stored_manifest(factory, context_id), context_id="ctx-somewhere")
    factory.context_manager._contexts[context_id]["revision"] = copied

    with use(ScriptedRunner("Never reached.")):
        with pytest.raises(revisions.ContextCompatibilityError) as refused:
            await factory.run_agent("worker", "Second", context_id=context_id)
    assert refused.value.code == revisions.CONTEXT_MISMATCH


# -- regression: external sources are part of the config digest ----------------


def _config_with_external_sources(tmp_path: Path, skill_text: str = "Skill v1", tool_text: str = "x = 1") -> str:
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    (skills / "guide.md").write_text(skill_text, encoding="utf-8")
    package = tmp_path / "tools" / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / "impl.py").write_text(tool_text, encoding="utf-8")
    (package / ".env").write_text("API_KEY=sk-live-should-never-be-read", encoding="utf-8")
    config = {
        "settings": {
            "default_agent": "worker",
            "max_turns": 5,
            "agent_timeout": 30,
            "working_directory": str(tmp_path / "work"),
            "logs_directory": str(tmp_path / "logs"),
            "mcp_enabled": False,
            "agent_logging": {"enabled": False},
        },
        "providers": {"p": {"name": "p", "base_url": "https://llm.test/v1", "api_key": SECRET}},
        "models": {"m": {"name": "m", "provider": "p", "context_window": 100000}},
        "prompt_templates": {"base": PROMPT},
        "tools": {"local": {"type": "function", "name": "local", "tool_package": "./tools/pkg"}},
        "agents": {
            "worker": {
                "name": "Worker",
                "model": "m",
                "tools": ["local"],
                "base_prompt": "base",
                "system_skills": ["guide"],
            }
        },
    }
    (tmp_path / "work").mkdir(exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(path)


async def test_a_changed_skill_instruction_continues(tmp_path):
    """External sources are part of the system digest: a new skill re-pins."""
    path = _config_with_external_sources(tmp_path)
    factory = AgentFactory(Config(path), tracing_level=None)
    factory._get_agent_session = lambda agent_key, context_id: SQLiteSession(f"{agent_key}-{context_id}")
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()

    (tmp_path / "skills" / "guide.md").write_text("Skill v2, different instructions", encoding="utf-8")
    factory.config = Config(path)
    factory._agent_sessions = {}
    runner = ScriptedRunner("Second answer.")
    with use(runner):
        await factory.run_agent("worker", "Second", context_id=context_id)
    assert runner.calls
    assert [
        entry["code"] for entry in factory.context_manager.revision_history(context_id)
    ] == [revisions.SYSTEM_CONFIG_CHANGED]


async def test_a_changed_local_tool_source_continues(tmp_path):
    """A changed local tool source is a configuration change, not a refusal."""
    path = _config_with_external_sources(tmp_path)
    factory = AgentFactory(Config(path), tracing_level=None)
    factory._get_agent_session = lambda agent_key, context_id: SQLiteSession(f"{agent_key}-{context_id}")
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    context_id = factory.context_manager.get_current_context_id()

    (tmp_path / "tools" / "pkg" / "impl.py").write_text("x = 2", encoding="utf-8")
    factory.config = Config(path)
    factory._agent_sessions = {}
    runner = ScriptedRunner("Second answer.")
    with use(runner):
        await factory.run_agent("worker", "Second", context_id=context_id)
    assert runner.calls
    assert [
        entry["code"] for entry in factory.context_manager.revision_history(context_id)
    ] == [revisions.SYSTEM_CONFIG_CHANGED]


def test_external_sources_are_hashed_but_never_stored(tmp_path):
    path = _config_with_external_sources(tmp_path)
    snapshot = revisions.system_snapshot(Config(path))
    text = json.dumps(snapshot)
    assert "Skill v1" not in text
    assert "sk-live-should-never-be-read" not in text
    assert "x = 1" not in text
    assert snapshot["status"] == "resolved"


# -- regression: the web gate runs before routing, warm-up and quota ----------


class _Socket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, event: dict) -> None:
        self.sent.append(event)


def _web_space(manager, config):
    from types import SimpleNamespace

    from web_chat.turns import TurnBoard

    calls = {"quota": 0, "warm": 0, "metadata": 0, "resolve": 0}
    registry = SimpleNamespace(
        selection_is_valid=lambda system, agent: True,
        agent_label=lambda system, agent: agent,
        config=lambda key: config,
        default_key=lambda: "worker",
        agent_issues=lambda system, agent: [],
    )

    async def resolve_turn(*args, **kwargs):
        calls["resolve"] += 1
        return SimpleNamespace(
            system="worker", agent="worker", config=config,
            factory=SimpleNamespace(), routed_system=True, routed_agent=True, warning="",
        )

    async def warm_agent(*args, **kwargs):
        calls["warm"] += 1

    space = SimpleNamespace(
        restart_pending=False,
        context_manager=lambda: manager,
        turns=TurnBoard(),
        registry=registry,
        registry_of=lambda context_id: registry,
        resolve_turn=resolve_turn,
        warm_agent=warm_agent,
        admit_turn=lambda: calls.__setitem__("quota", calls["quota"] + 1),
        update_conversation_metadata=lambda *a, **kw: calls.__setitem__("metadata", calls["metadata"] + 1),
        workspace_label_of=lambda context_id: ".",
        user_id="test",
    )
    return space, calls


async def test_the_web_gate_sends_a_notice_and_still_refuses_incompatibility(tmp_path):
    """C2: a drifted conversation starts with a typed notice, not an error.

    A real incompatibility is still refused on the same path, before routing,
    warm-up, the daily-turn quota and any metadata write.
    """
    import asyncio
    import contextlib

    from web_chat.session import ChatSession

    factory = make_factory(tmp_path)
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    manager = factory.context_manager
    context_id = manager.get_current_context_id()
    pinned_before = manager.revision_manifest(context_id)
    # A config that differs from the pinned one, without a model call.
    factory.config.config.settings.max_turns = 6
    changed = factory.config

    space, calls = _web_space(manager, changed)
    socket = _Socket()
    session = ChatSession(space, socket, context_id)

    # The turn starts: the conversation is admitted, and the client is told
    # with a non-error event carrying the machine code.
    started = await session._start("Second", None, None)
    try:
        assert started is True
        assert not [event for event in socket.sent if event["type"] == "error"]
        notice = next(event for event in socket.sent if event["type"] == "revision_notice")
        assert notice["code"] == revisions.SYSTEM_CONFIG_CHANGED
        assert notice["context_id"] == context_id
        assert notice["content"]
        assert notice["details"]["revision_status"] == "pinned"
        assert notice["details"]["previous_digest"] == pinned_before["digest"]
        # Admission was decided before any accounted work, and the turn runs.
        assert calls["quota"] == 1
        assert calls["warm"] == 0  # the background turn warms, not the gate
    finally:
        running = session._turns.running(context_id)
        if running is not None:
            _turn, task = running
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # A really incompatible conversation is refused on this path, before any
    # routing, warm-up, quota or metadata write.
    contracts = {
        **{name: revisions.contract_section(name) for name in revisions.CONTRACTS if name != "history"},
        "history": {**revisions.contract_section("history"), "digest": "sha256:" + "0" * 64},
    }
    factory.context_manager._contexts[context_id]["revision"] = _resigned(
        _stored_manifest(factory, context_id), contracts=contracts
    )
    space, calls = _web_space(manager, factory.config)
    socket = _Socket()
    session = ChatSession(space, socket, context_id)

    assert await session._start("Third", None, None) is False
    error = next(event for event in socket.sent if event["type"] == "error")
    assert error["code"] == revisions.CONTRACT_CHANGED
    assert error["context_id"] == context_id
    assert error["details"]["revision_status"] == "pinned"
    assert not [event for event in socket.sent if event["type"] == "revision_notice"]
    assert calls == {"quota": 0, "warm": 0, "metadata": 0, "resolve": 0}


def test_the_web_gate_precedes_quota_and_the_turn_in_the_source():
    """C2: ordering is structural, so a later edit cannot quietly move it."""
    import inspect

    from web_chat.session import AgentTurn, ChatSession

    source = inspect.getsource(ChatSession._start)
    assert source.index("_check_revision") < source.index("space.admit_turn()")
    assert source.index("_check_revision") < source.index("AgentTurn(")
    produce = inspect.getsource(AgentTurn._produce)
    assert produce.index("revisions.compatible") < produce.index("warm_agent(")


def test_the_history_view_reports_the_revision(factory):
    from web_chat.server import _revision_view

    manager = factory.context_manager
    context_id = manager.start_new_context()
    assert _revision_view(manager, context_id)["status"] == "new"
    assert _revision_view(manager, "ctx-missing")["status"] == "unknown"


# -- regression: the validator answers in JSON whatever it is given -----------


@pytest.mark.parametrize(
    "document",
    [
        ["not", "an", "object"],
        {"schema": "grid.release.manifest", "schema_version": 1, "releases": [{"version": ["1", "0", "0"]}], "current": "1.0.0"},
        {"schema": "grid.release.manifest", "schema_version": 1, "releases": [{"version": "one"}], "current": "one"},
    ],
)
def test_the_release_validator_answers_json_for_bad_input(tmp_path, document):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "release_check.py"), "--json", "--manifest", str(path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert report["ok"] is False
    assert report["failed"]


async def test_fork_does_not_repair_a_tampered_manifest(factory):
    with use(ScriptedRunner("One.")):
        await factory.run_agent("worker", "First")
    manager = factory.context_manager
    parent = manager.get_current_context_id()
    message = next(m for m in manager._contexts[parent]["conversation"] if m.role == "user")
    manager._contexts[parent]["revision"]["pinned_at"] = "tampered"
    branch, _ = manager.fork_context(parent, message.metadata["message_id"])
    assert not revisions.manifest_digest_ok(manager.revision_manifest(branch))
    with pytest.raises(revisions.ContextCompatibilityError):
        revisions.admit_turn(manager, branch, factory.config)


def test_large_source_same_size_edit_changes_digest(tmp_path):
    path = tmp_path / "large.py"
    path.write_bytes(b"a" * (2 * 1024 * 1024 + 1))
    before = revisions._source_digest(path, tmp_path)
    path.write_bytes(b"b" * (2 * 1024 * 1024 + 1))
    after = revisions._source_digest(path, tmp_path)
    assert before["status"] == after["status"] == "hashed"
    assert before["sha256"] != after["sha256"]
