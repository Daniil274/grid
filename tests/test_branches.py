"""Editing a message forks the conversation; the agent remembers exactly the past."""

from unittest.mock import AsyncMock, patch

import pytest

from core.context import ContextManager
from tests.test_run_agent import ScriptedRunner, factory, summarized, use  # noqa: F401 - fixture

#: What the chat's own pages send with a request that changes something.
SAME_SITE = {"Origin": "http://testserver"}


def texts(manager, context_id):
    return [m.content for m in manager.conversation_view(context_id)["messages"]]


def ids(manager, context_id):
    return [m.metadata["message_id"] for m in manager.conversation_view(context_id)["messages"]]


def user_turns(manager, context_id, *messages):
    for message in messages:
        manager.append_message_to(context_id, "user", message, {"type": "user_input"})
        manager.append_message_to(context_id, "assistant", f"answer to {message}")


# -- the context manager -------------------------------------------------------


def test_a_branch_holds_the_messages_before_the_edited_one_with_their_ids():
    manager = ContextManager()
    root = manager.start_new_context()
    manager.update_context_metadata(root, {"title": "Build", "routed_agent": "worker"})
    user_turns(manager, root, "first", "second", "third")
    second = ids(manager, root)[2]

    branch, slot = manager.fork_context(root, second)

    assert texts(manager, branch) == ["first", "answer to first"]
    assert ids(manager, branch) == ids(manager, root)[:2]
    assert slot == second
    metadata = manager.get_context_metadata(branch)
    assert (metadata["branch_root"], metadata["branch_parent"], metadata["branch_slot"]) == (root, root, second)
    assert metadata["title"] == "Build" and metadata["routed_agent"] == "worker"
    assert manager.open_branch(root) == branch
    assert texts(manager, root)[2] == "second"  # the original is untouched


def test_only_a_user_message_of_the_conversation_can_be_edited():
    manager = ContextManager()
    root = manager.start_new_context()
    user_turns(manager, root, "first")
    with pytest.raises(ValueError):
        manager.fork_context(root, ids(manager, root)[1])  # the answer
    with pytest.raises(KeyError):
        manager.fork_context(root, "no-such-message")


def test_versions_of_a_message_are_listed_in_every_branch_that_has_one():
    manager = ContextManager()
    root = manager.start_new_context()
    user_turns(manager, root, "first", "second")
    original = ids(manager, root)[2]

    edit, slot = manager.fork_context(root, original)
    manager.append_message_to(edit, "user", "second, better", {"type": "user_input", "edit_of": slot})
    edited = ids(manager, edit)[-1]
    # An edit of the edit is a third version of the same message.
    again, slot_again = manager.fork_context(edit, edited)
    assert slot_again == slot
    manager.append_message_to(again, "user", "second, best", {"type": "user_input", "edit_of": slot})

    in_root = manager.message_versions(root)[original]
    in_again = manager.message_versions(again)[ids(manager, again)[-1]]
    assert (in_root["index"], in_root["total"]) == (0, 3)
    assert (in_again["index"], in_again["total"]) == (2, 3)
    assert in_root["targets"] == in_again["targets"] == [root, edit, again]
    # A message that was never edited has one version: nothing to switch.
    assert ids(manager, root)[0] not in manager.message_versions(root)


def test_deleting_a_conversation_deletes_its_branches():
    manager = ContextManager()
    root = manager.start_new_context()
    user_turns(manager, root, "first")
    branch, _ = manager.fork_context(root, ids(manager, root)[0])
    other = manager.start_new_context()

    assert set(manager.delete_family(branch)) == {root, branch}
    assert set(manager.list_context_ids()) >= {other}
    assert root not in manager.list_context_ids()


# -- the factory: the agent's session is cut where the edited turn began --------


async def three_turns(factory):
    context_id = factory.context_manager.start_new_context()
    with use(ScriptedRunner("One.", "Two.", "Three.")):
        for text in ("first", "second", "third"):
            await factory.run_agent("worker", text, context_id=context_id)
    return context_id


def user_message_id(factory, context_id, text):
    return next(
        m.metadata["message_id"]
        for m in factory.context_manager.conversation_view(context_id)["messages"]
        if m.role == "user" and m.content == text
    )


async def test_the_branchs_agent_remembers_every_turn_before_the_edit_and_none_after(factory):
    context_id = await three_turns(factory)
    second = user_message_id(factory, context_id, "second")

    branch = await factory.fork_conversation(context_id, second)

    items = await factory.sessions[("worker", branch)].get_items()
    assert [item["content"] for item in items] == ["first", "One."]
    # The original keeps its whole session.
    assert len(await factory.sessions[("worker", context_id)].get_items()) == 6

    runner = ScriptedRunner("Two, again.")
    with use(runner):
        await factory.run_agent("worker", "second, better", context_id=branch, edit_of=second)
    assert runner.calls[0].session is factory.sessions[("worker", branch)]
    stored = factory.context_manager.conversation_view(branch)["messages"][-2]
    assert stored.content == "second, better" and stored.metadata["edit_of"] == second
    assert factory.context_manager.message_versions(branch)[stored.metadata["message_id"]]["total"] == 2


async def test_editing_the_first_message_starts_the_agent_fresh(factory):
    context_id = await three_turns(factory)
    branch = await factory.fork_conversation(context_id, user_message_id(factory, context_id, "first"))
    assert await factory._get_agent_session("worker", branch).get_items() == []
    assert factory.context_manager.conversation_view(branch)["messages"] == []


async def test_a_session_summarized_since_the_edit_point_is_not_cut(factory):
    context_id = await three_turns(factory)
    second = user_message_id(factory, context_id, "second")
    with patch("core.factory.sessions.compact_conversation", new=AsyncMock(return_value=summarized())), patch.object(
        factory.models, "compact_client_and_model", return_value=(object(), "m")
    ):
        assert await factory.compact_session("worker", context_id, force=True)

    branch = await factory.fork_conversation(context_id, second)

    # No exact cut exists any more: the agent reads the copied messages instead.
    assert await factory._get_agent_session("worker", branch).get_items() == []
    assert [m.content for m in factory.context_manager.conversation_view(branch)["messages"]] == ["first", "One.\n\nContext ID: " + context_id]


async def test_a_compaction_marker_does_not_hide_an_interruption_from_continue(factory):
    from core.interruption import INTERRUPTED_TYPE, Interruption, StopReason

    context_id = await three_turns(factory)
    manager = factory.context_manager
    record = Interruption(reason=StopReason.USER_STOP, task="third", agent="worker")
    manager.append_message_to(
        context_id, "assistant", "stopped", {"type": INTERRUPTED_TYPE, "interruption": record.to_dict()}
    )
    assert manager.pending_interruption(context_id) is not None

    with patch("core.factory.sessions.compact_conversation", new=AsyncMock(return_value=summarized())), patch.object(
        factory.models, "compact_client_and_model", return_value=(object(), "m")
    ):
        assert await factory.compact_session("worker", context_id, force=True)

    last = manager.conversation_view(context_id)["messages"][-1]
    assert last.metadata["type"] == "context_compacted"
    assert manager.pending_interruption(context_id) is not None
    assert manager.take_interruption(context_id) is not None


# -- the web API ------------------------------------------------------------------


def web_client(factory):
    """The chat server over the real factory and its context manager."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from tests.test_web_ui_routes import _DummySpace, _server

    space = _DummySpace()
    space._context_manager = factory.context_manager
    space.registry = SimpleNamespace(
        default_key=lambda: "s", factory=lambda system: factory, keys=lambda: ["s"],
        systems=lambda: [], can_route=False, has_catalog=False,
    )
    return TestClient(_server(space).app, headers=SAME_SITE)


async def test_the_web_api_forks_lists_one_row_and_deletes_the_family(factory):
    context_id = await three_turns(factory)
    factory.context_manager.update_context_metadata(context_id, {"created_by_web": True})
    second = user_message_id(factory, context_id, "second")
    client = web_client(factory)

    branch = client.post(f"/api/chat/conversations/{context_id}/branches", json={"message_id": second}).json()
    assert branch["edit_of"] == second and branch["root"] == context_id
    with use(ScriptedRunner("Two, again.")):
        await factory.run_agent("worker", "second, better", context_id=branch["id"], edit_of=second)

    rows = [row for row in client.get("/api/chat/conversations").json() if row["id"] == context_id]
    assert len(rows) == 1 and rows[0]["open_id"] == branch["id"] and rows[0]["branches"] == 2
    assert not any(row["id"] == branch["id"] for row in client.get("/api/chat/conversations").json())

    shown = client.get(f"/api/chat/conversations/{branch['id']}").json()
    assert shown["root"] == context_id
    edited = next(m for m in shown["messages"] if m["content"] == "second, better")
    assert edited["versions"] == {"index": 1, "total": 2, "targets": [context_id, branch["id"]]}

    # Switching back makes the original the one the row opens.
    assert client.post(f"/api/chat/conversations/{context_id}/activate").status_code == 200
    row = next(row for row in client.get("/api/chat/conversations").json() if row["id"] == context_id)
    assert row["open_id"] == context_id

    assert client.post(f"/api/chat/conversations/{context_id}/branches", json={"message_id": "nope"}).status_code == 404
    answer = next(m for m in shown["messages"] if m["role"] == "assistant")
    assert client.post(f"/api/chat/conversations/{context_id}/branches", json={"message_id": answer["id"]}).status_code in (400, 404)

    deleted = client.delete(f"/api/chat/conversations/{branch['id']}").json()
    assert set(deleted["contexts"]) == {context_id, branch["id"]}
