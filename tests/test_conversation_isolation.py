"""Turns of different conversations running at once never write into each other.

A web chat runs every conversation of a user in one ContextManager. Before
turns bound their conversation, whatever a turn wrote without naming one -
its run record, its execution, a sub-agent's report - went into whichever
conversation another turn had activated last.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import agents
import pytest
import yaml
from agents import SQLiteSession

from core.agent_factory import AgentFactory
from core.config import Config
from core.context import ContextManager
from core.conversation_store import ConversationStore
from schemas import AgentExecution


def _texts(manager, context_id):
    return [m.content for m in manager.conversation_view(context_id)["messages"]]


async def _turn(manager, context_id, label, rounds, other):
    """A turn's writes through the methods that name no conversation, while
    another party keeps switching the manager's active one."""
    with manager.bound(context_id):
        for i in range(rounds):
            manager.add_message("user", f"{label}-{i}")
            manager.set_metadata("pending_agent_run", {"agent": label, "round": i})
            manager.add_execution(AgentExecution(agent_name=label, start_time=float(i), input_message=label))
            manager.add_tool_result_as_message("call_worker", f"report {label}-{i}")
            manager.activate_context(other)  # what another chat's turn does
            await asyncio.sleep(0)
            assert manager.get_current_context_id() == context_id


@pytest.fixture
def manager(tmp_path):
    manager = ContextManager(
        max_history=500,
        persist_path=str(tmp_path / "conversations.json"),
        create_initial_context=False,
        store=ConversationStore(tmp_path / "conversations"),
    )
    manager.ensure_context("ctx-aaaaaaaa")
    manager.ensure_context("ctx-bbbbbbbb")
    return manager


async def test_concurrent_turns_write_only_into_their_own_conversation(manager, tmp_path):
    a, b = "ctx-aaaaaaaa", "ctx-bbbbbbbb"
    await asyncio.gather(_turn(manager, a, "A", 20, b), _turn(manager, b, "B", 20, a))

    for context_id, label in ((a, "A"), (b, "B")):
        texts = _texts(manager, context_id)
        assert len(texts) == 40
        assert all(label in text for text in texts), texts
        metadata = manager.get_context_metadata(context_id)
        assert metadata["pending_agent_run"] == {"agent": label, "round": 19}
        executions = manager.get_context_executions(context_id)["executions"]
        assert {e["agent_name"] for e in executions} == {label}
    # The binding ended with each turn.
    assert manager.bound_context_id() is None

    # What was written is what is saved.
    manager.save(strict=True)
    reloaded = ContextManager(
        max_history=500,
        persist_path=str(tmp_path / "conversations.json"),
        create_initial_context=False,
        store=ConversationStore(tmp_path / "conversations"),
    )
    assert _texts(reloaded, a) == _texts(manager, a)
    assert _texts(reloaded, b) == _texts(manager, b)


async def test_tasks_started_by_a_turn_inherit_its_conversation(manager):
    a, b = "ctx-aaaaaaaa", "ctx-bbbbbbbb"

    async def tool_call():  # a tool call: the SDK runs it as a task of its own
        await asyncio.sleep(0)
        manager.add_tool_result_as_message("call_worker", "from the tool")

    with manager.bound(a):
        task = asyncio.create_task(tool_call())
    manager.activate_context(b)
    await task

    assert _texts(manager, a) == ["Tool result of call_worker: from the tool"]
    assert _texts(manager, b) == []


def test_named_conversation_wins_over_the_binding(manager):
    a, b = "ctx-aaaaaaaa", "ctx-bbbbbbbb"
    with manager.bound(a):
        manager.add_message("user", "for b", context_id=b)
        manager.set_metadata("k", "v", context_id=b)
        assert manager.get_metadata("k") is None
        assert manager.get_metadata("k", context_id=b) == "v"
    assert _texts(manager, b) == ["for b"]
    assert _texts(manager, a) == []


def test_a_conversation_deleted_mid_turn_is_not_replaced_by_another(manager):
    a, b = "ctx-aaaaaaaa", "ctx-bbbbbbbb"
    manager.activate_context(b)
    with manager.bound(a):
        manager.delete_context(a)
        manager.add_message("user", "late write")
        manager.set_metadata("pending_agent_run", {"status": "running"})
    assert not manager.has_context(a)
    assert _texts(manager, b) == []
    assert "pending_agent_run" not in manager.get_context_metadata(b)


def test_without_a_binding_the_active_conversation_is_used(tmp_path):
    """The CLI binds nothing: it works in the active conversation, as before."""
    manager = ContextManager(persist_path=str(tmp_path / "c.json"))
    first = manager.get_current_context_id()
    manager.add_message("user", "hello")
    second = manager.start_new_context()
    manager.add_message("user", "again")
    assert _texts(manager, first) == ["hello"]
    assert _texts(manager, second) == ["again"]


# -- through the factory: two turns of one manager at the same time ----------


@pytest.fixture
def factory(tmp_path):
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
        "providers": {"p": {"name": "p", "base_url": "https://llm.test/v1", "api_key": "test"}},
        "models": {"m": {"name": "m", "provider": "p", "context_window": 100000}},
        "prompt_templates": {"base": "You help."},
        "agents": {"worker": {"name": "Worker", "model": "m", "tools": [], "base_prompt": "base"}},
    }
    (tmp_path / "work").mkdir()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    factory = AgentFactory(Config(str(path)), tracing_level=None)
    sessions = {}
    factory._get_agent_session = lambda agent_key, context_id: sessions.setdefault(
        (agent_key, context_id), SQLiteSession(f"{agent_key}-{context_id}")
    )
    return factory


class InterleavingRunner:
    """Each run waits until both turns are inside the model call, then writes
    like a sub-agent tool does - into no named conversation."""

    def __init__(self, factory, parties=2):
        self.factory = factory
        self.inside = 0
        self.both_inside = asyncio.Event()
        self.parties = parties

    async def run(self, agent, run_input, *, context, max_turns, session, run_config=None):
        self.inside += 1
        if self.inside == self.parties:
            self.both_inside.set()
        await self.both_inside.wait()
        await asyncio.sleep(0)
        self.factory.context_manager.add_tool_result_as_message("call_worker", f"report for {run_input}")
        self.factory.journal.record_event(event_type="tool_output", tool_name="call_worker")
        reply = f"answer to {run_input}"
        await session.add_items([{"role": "user", "content": str(run_input)}, {"role": "assistant", "content": reply}])
        return SimpleNamespace(final_output=reply, new_items=[])


async def test_concurrent_turns_through_the_factory(factory):
    manager = factory.context_manager
    a = manager.start_new_context("ctx-11111111")
    b = manager.start_new_context("ctx-22222222")
    runner = InterleavingRunner(factory)
    with patch.object(agents, "Runner", runner):
        answers = await asyncio.gather(
            factory.run_agent("worker", "question A", context_id=a),
            factory.run_agent("worker", "question B", context_id=b),
        )
    assert answers == ["answer to question A", "answer to question B"]

    for context_id, label in ((a, "A"), (b, "B")):
        texts = _texts(manager, context_id)
        assert texts == [f"question {label}", f"Tool result of call_worker: report for question {label}", f"answer to question {label}"]
        pending = manager.get_context_metadata(context_id)["pending_agent_run"]
        assert pending["context_id"] == context_id
        assert pending["status"] == "completed"
        assert pending["task"] == f"question {label}"
        assert any(e.get("tool_name") == "call_worker" for e in pending["tool_events"])
        executions = manager.get_context_executions(context_id)["executions"]
        assert [e["context_id"] for e in executions] == [context_id]
