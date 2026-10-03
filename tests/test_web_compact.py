"""Manual compaction keeps history and reserves the context while summarizing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.test_web_spaces import FakeSpace
from core.compact import COMPACTED_TYPE
from web_chat.views import serialize_message
from web_chat.server import WebChatServer
from web_chat.session import ChatSession
from web_chat.spaces import SpacePool


@pytest.fixture
def compact_server():
    space = FakeSpace("default_user")
    manager = space.context_manager()
    manager.ensure_context("ctx")
    manager.update_context_metadata("ctx", {"routed_system": "s", "routed_agent": "a"})
    manager.append_message_to("ctx", "user", "Keep the visible conversation.")
    factory = SimpleNamespace(compact_session=AsyncMock(return_value={"tokens_before": 12000, "tokens_after": 800}))
    space.registry = SimpleNamespace(factory=lambda key: factory, default_key=lambda: "s")
    server = WebChatServer(SimpleNamespace(), SpacePool(lambda user_id: space), warm_user=None)
    return server, space, factory


def api_client(server):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://testserver",
                             headers={"Origin": "http://testserver"})


async def test_compaction_returns_counts_and_does_not_touch_stored_messages(compact_server):
    server, space, factory = compact_server
    before = space.context_manager().conversation_view("ctx")
    async with api_client(server) as client:
        response = await client.post("/api/chat/conversations/ctx/compact")
    assert response.status_code == 200
    assert response.json() == {"id": "ctx", "tokens_before": 12000, "tokens_after": 800}
    factory.compact_session.assert_awaited_once_with("a", "ctx", force=True)
    # The marker is added by the real factory (tests/test_run_agent.py); the route itself writes nothing.
    assert space.context_manager().conversation_view("ctx") == before
    assert not space.turns.is_claimed("ctx")


async def test_compaction_reserves_context_against_turns_edits_deletion_and_duplicate_requests(compact_server):
    server, space, factory = compact_server
    entered, finish = asyncio.Event(), asyncio.Event()

    async def compact(*args, **kwargs):
        entered.set()
        await finish.wait()
        return {"tokens_before": 12000, "tokens_after": 800}

    factory.compact_session.side_effect = compact
    async with api_client(server) as client:
        first = asyncio.create_task(client.post("/api/chat/conversations/ctx/compact"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert space.turns.is_claimed("ctx")
            assert (await client.post("/api/chat/conversations/ctx/compact")).status_code == 409
            assert (await client.delete("/api/chat/conversations/ctx")).status_code == 409
            assert (await client.post("/api/chat/conversations/ctx/branches", json={"message_id": "m"})).status_code == 409
            socket = SimpleNamespace(send_json=AsyncMock())
            session = ChatSession(space, socket, "ctx")
            assert not await session._start("hello", None, None)
            assert socket.send_json.await_args.args[0]["type"] == "busy"
        finally:
            finish.set()
            await first
    assert factory.compact_session.await_count == 1
    assert not space.turns.is_claimed("ctx")


@pytest.mark.parametrize("fails", [False, True])
async def test_failed_compaction_releases_context_for_retry(compact_server, fails):
    server, space, factory = compact_server
    if fails:
        factory.compact_session.side_effect = OSError("storage failed")
    else:
        factory.compact_session.return_value = None
    async with api_client(server) as client:
        if fails:
            with pytest.raises(OSError):
                await client.post("/api/chat/conversations/ctx/compact")
        else:
            assert (await client.post("/api/chat/conversations/ctx/compact")).status_code == 422
        assert not space.turns.is_claimed("ctx")
        factory.compact_session.side_effect = None
        factory.compact_session.return_value = {"tokens_before": 100, "tokens_after": 10}
        assert (await client.post("/api/chat/conversations/ctx/compact")).status_code == 200


async def test_compaction_does_not_call_a_model_for_missing_or_empty_context(compact_server):
    server, space, factory = compact_server
    async with api_client(server) as client:
        assert (await client.post("/api/chat/conversations/missing/compact")).status_code == 404
        space.context_manager().update_context_metadata("ctx", {"routed_agent": None})
        assert (await client.post("/api/chat/conversations/ctx/compact")).status_code == 400
    factory.compact_session.assert_not_awaited()


def test_a_compaction_marker_serializes_with_its_token_counts():
    message = SimpleNamespace(
        role="assistant",
        content="",
        timestamp="2026-01-02T03:04:05Z",
        metadata={"type": COMPACTED_TYPE, "tokens_before": 12000, "tokens_after": 800},
    )
    view = serialize_message(message)
    assert view["kind"] == "compaction"
    assert view["compaction"] == {"tokens_before": 12000, "tokens_after": 800}


def test_an_ordinary_message_is_not_a_compaction_marker():
    message = SimpleNamespace(role="user", content="hello", metadata={})
    view = serialize_message(message)
    assert view["kind"] is None
    assert view["compaction"] is None
