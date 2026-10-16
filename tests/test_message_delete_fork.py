"""Deleting a message reaches the agent's memory; a fork is a new chat up to a turn."""

from unittest.mock import AsyncMock, patch

from tests.test_branches import three_turns, user_message_id, web_client
from tests.test_run_agent import ScriptedRunner, factory, summarized, use  # noqa: F401 - fixture


def texts(factory, context_id):
    return [m.content for m in factory.context_manager.conversation_view(context_id)["messages"]]


def answer_id(factory, context_id, text):
    return next(
        m.metadata["message_id"]
        for m in factory.context_manager.conversation_view(context_id)["messages"]
        if m.role == "assistant" and m.content.startswith(text)
    )


async def session_texts(factory, context_id):
    return [item["content"] for item in await factory._get_agent_session("worker", context_id).get_items()]


async def test_deleting_a_request_removes_its_turn_from_the_chat_and_the_session(factory):
    context_id = await three_turns(factory)

    result = await factory.delete_message(context_id, user_message_id(factory, context_id, "second"))

    assert result["context"] is True and len(result["removed"]) == 2
    assert [t.split("\n")[0] for t in texts(factory, context_id)] == ["first", "One.", "third", "Three."]
    assert await session_texts(factory, context_id) == ["first", "One.", "third", "Three."]
    # The later turn's mark follows its items, so a fork or a delete still cuts right.
    third = next(m for m in factory.context_manager.conversation_view(context_id)["messages"] if m.content == "third")
    assert third.metadata["session_mark"]["items"] == 2
    await factory.delete_message(context_id, user_message_id(factory, context_id, "third"))
    assert await session_texts(factory, context_id) == ["first", "One."]


async def test_deleting_an_answer_keeps_the_request(factory):
    context_id = await three_turns(factory)
    answer = answer_id(factory, context_id, "Two.")

    result = await factory.delete_message(context_id, answer)

    assert result == {"removed": [answer], "context": True}
    assert "second" in texts(factory, context_id)
    assert not any(t.startswith("Two.") for t in texts(factory, context_id))
    assert await session_texts(factory, context_id) == ["first", "One.", "second", "third", "Three."]


async def test_a_summarized_session_is_left_alone_and_says_so(factory):
    context_id = await three_turns(factory)
    with patch("core.factory.sessions.compact_conversation", new=AsyncMock(return_value=summarized())), patch.object(
        factory.models, "compact_client_and_model", return_value=(object(), "m")
    ):
        assert await factory.compact_session("worker", context_id, force=True)
    before = await session_texts(factory, context_id)

    result = await factory.delete_message(context_id, user_message_id(factory, context_id, "second"))

    assert result["context"] is False
    assert "second" not in texts(factory, context_id)
    assert await session_texts(factory, context_id) == before


async def test_a_fork_is_a_new_chat_up_to_the_end_of_the_turn(factory):
    context_id = await three_turns(factory)
    factory.context_manager.update_context_metadata(context_id, {"title": "Build"})

    fork = await factory.fork_to_new(context_id, answer_id(factory, context_id, "Two."))

    assert [t.split("\n")[0] for t in texts(factory, fork)] == ["first", "One.", "second", "Two."]
    assert await session_texts(factory, fork) == ["first", "One.", "second", "Two."]
    metadata = factory.context_manager.get_context_metadata(fork)
    assert metadata["title"] == "Build (fork)" and metadata["forked_from"] == context_id
    assert "branch_root" not in metadata
    assert len(texts(factory, context_id)) == 6  # the original is untouched

    with use(ScriptedRunner("Four.")):
        await factory.run_agent("worker", "fourth", context_id=fork)
    assert (await session_texts(factory, fork))[-2:] == ["fourth", "Four."]


async def test_the_web_api_deletes_and_forks(factory):
    context_id = await three_turns(factory)
    factory.context_manager.update_context_metadata(context_id, {"created_by_web": True})
    client = web_client(factory)

    fork = client.post(f"/api/chat/conversations/{context_id}/forks", json={"message_id": answer_id(factory, context_id, "One.")})
    assert fork.status_code == 200
    assert any(row["id"] == fork.json()["id"] for row in client.get("/api/chat/conversations").json())

    second = user_message_id(factory, context_id, "second")
    deleted = client.delete(f"/api/chat/conversations/{context_id}/messages/{second}").json()
    assert deleted["context"] is True and len(deleted["removed"]) == 2
    assert client.delete(f"/api/chat/conversations/{context_id}/messages/nope").status_code == 404
    assert client.post(f"/api/chat/conversations/{context_id}/forks", json={"message_id": "nope"}).status_code == 404
