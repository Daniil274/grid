"""A planned restart checkpoints once and resumes only the turns it paused."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from core.context import ContextManager
from core.interruption import Interruption, StopReason
from tests.test_web_chat_session import Socket, space_for
from tests.test_web_spaces import FakeSpace, SAME_SITE
from web_chat.delivery import MessageQueue
from web_chat.identity import User
from web_chat.restart import ServerRestart, SilentSocket
from web_chat.server import WebChatServer
from web_chat.session import ChatSession
from web_chat.spaces import SpacePool


class PersistentSpace(FakeSpace):
    def __init__(self, user_id, path):
        super().__init__(user_id)
        self.conversations = ContextManager(persist_path=str(path))


def saved_interruption(manager, turn_id="original", *, reason=StopReason.USER_STOP, in_flight=()):
    manager.ensure_context("ctx")
    record = Interruption(reason=reason, task="work", agent="a", in_flight=list(in_flight))
    manager.append_message_to("ctx", "user", "work")
    manager.append_message_to("ctx", "assistant", record.summary(), record.message_metadata(context_id="ctx", turn_id=turn_id))
    manager.update_context_metadata("ctx", {
        "pending_agent_run": {"turn_id": turn_id, "status": "interrupted"},
        "routed_system": "s", "routed_agent": "a",
    })


def resumable_space(path, continued):
    space = space_for(AsyncMock(return_value="queued answer"))
    manager = ContextManager(persist_path=str(path))
    space.context_manager = lambda: manager
    space.update_conversation_metadata = lambda key, **values: manager.update_context_metadata(key, values)
    space.admit_turn = Mock(return_value=None)

    async def resume(agent, context_id, **kwargs):
        assert manager.take_interruption(context_id) is not None
        continued.append((space.user_id, agent, context_id))
        manager.update_context_metadata(context_id, {"pending_agent_run": {"turn_id": kwargs["turn_id"], "status": "completed"}})
        manager.append_message_to(context_id, "assistant", "finished")
        return "finished"

    space.factory.continue_agent = resume
    return space


async def settled(board):
    async with asyncio.timeout(3):
        while not board.idle:
            await asyncio.sleep(0.01)


async def test_restart_waits_for_current_step_then_resumes_from_disk_once(tmp_path):
    started, finish_step, stop_requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
    history = tmp_path / "conversation.json"
    original = PersistentSpace("test", history)
    space = space_for(AsyncMock())
    manager = original.conversations
    space.context_manager = lambda: manager
    space.update_conversation_metadata = lambda key, **values: manager.update_context_metadata(key, values)

    async def run(**kwargs):
        started.set()
        await finish_step.wait()
        saved_interruption(manager, kwargs["turn_id"])
        return "paused"

    def stop(_):
        stop_requested.set()
        return True

    space.factory.run_agent = run
    space.factory.request_stop = stop
    # SpacePool and ServerRestart use the same real TurnBoard as ChatSession.
    class LiveSpace:
        def __getattr__(self, key):
            return getattr(space, key)

        @property
        def idle(self):
            return space.turns.idle

    live = LiveSpace()
    pool = SpacePool(lambda user_id: live)
    async with pool.use("test"):
        session = ChatSession(live, Socket(), "ctx")
        assert await session._start("work", "s", "a")
    await started.wait()
    stop_server = Mock()
    restart = ServerRestart(pool, tmp_path / "restart.json", stop_server)
    first = restart.request()
    task = restart._task
    assert restart.request()["instance_id"] == first["instance_id"]
    assert restart._task is task
    await asyncio.wait_for(stop_requested.wait(), 2)
    assert not stop_server.called
    assert not await session._start("new work", "s", "a")
    finish_step.set()
    await asyncio.wait_for(task, 3)
    stop_server.assert_called_once()
    assert restart.phase == "restarting"
    rows = json.loads(restart.path.read_text())["turns"]
    assert len(rows) == 1 and rows[0]["mode"] == "continue"

    continued = []
    restored = resumable_space(history, continued)
    next_pool = SpacePool(lambda user_id: restored)
    successor = ServerRestart(next_pool, restart.path, Mock())
    await successor.recover()
    await settled(restored.turns)
    await successor.recover()
    assert continued == [("test", "a", "ctx")]
    restored.admit_turn.assert_not_called()
    assert not restart.path.exists()
    reloaded = ContextManager(persist_path=str(history))
    assert reloaded.pending_interruption("ctx") is None


@pytest.mark.parametrize("reason,in_flight", [
    (StopReason.CRASH, []),
    (StopReason.USER_STOP, [{"tool": "send_email", "arguments": "{}"}]),
])
async def test_ambiguous_or_crashed_tools_are_not_automatically_repeated(tmp_path, reason, in_flight):
    history = tmp_path / "conversation.json"
    saved_interruption(ContextManager(persist_path=str(history)), reason=reason, in_flight=in_flight)
    continued = []
    space = resumable_space(history, continued)
    restart = ServerRestart(SpacePool(lambda _: space), tmp_path / "restart.json", Mock())
    restart._write([{"user_id": "test", "context_id": "ctx", "turn_id": "original", "mode": "continue"}])
    await restart.recover()
    assert continued == [] and restart.recovery_errors == 1
    assert len(json.loads(restart.path.read_text())["turns"]) == 1


async def test_disabled_account_and_superseded_turn_are_not_resumed(tmp_path):
    build = Mock()
    restart = ServerRestart(SpacePool(build), tmp_path / "restart.json", Mock(), may_resume=lambda _: False)
    row = {"user_id": "disabled", "context_id": "ctx", "turn_id": "old", "mode": "continue"}
    restart._write([row])
    await restart.recover()
    build.assert_not_called()
    manager = ContextManager(persist_path=str(tmp_path / "conversation.json"))
    saved_interruption(manager, "newer")
    space = resumable_space(manager.persist_path, [])
    restart = ServerRestart(SpacePool(lambda _: space), restart.path, Mock())
    restart._write([row])
    await restart.recover()
    assert not space.turns.is_claimed("ctx")


async def test_recovery_isolates_users_with_the_same_conversation_id(tmp_path):
    continued, spaces = [], {}
    for user_id in ("alice", "bob"):
        path = tmp_path / f"{user_id}.json"
        saved_interruption(ContextManager(persist_path=str(path)))
        spaces[user_id] = resumable_space(path, continued)
        spaces[user_id].user_id = user_id
    restart = ServerRestart(SpacePool(spaces.__getitem__), tmp_path / "restart.json", Mock())
    restart._write([
        {"user_id": user_id, "context_id": "ctx", "turn_id": "original", "mode": "continue"}
        for user_id in spaces
    ])
    await restart.recover()
    for space in spaces.values():
        await settled(space.turns)
    assert sorted(continued) == [("alice", "a", "ctx"), ("bob", "a", "ctx")]


async def test_waiting_messages_continue_after_the_restored_turn(tmp_path):
    path = tmp_path / "history.json"
    manager = ContextManager(persist_path=str(path))
    saved_interruption(manager)
    MessageQueue(manager, "ctx").add("next request", [], "after_turn")
    continued = []
    space = resumable_space(path, continued)
    restart = ServerRestart(SpacePool(lambda _: space), tmp_path / "restart.json", Mock())
    restart._write([{"user_id": "test", "context_id": "ctx", "turn_id": "original", "mode": "continue"}])
    await restart.recover()
    await settled(space.turns)
    assert len(continued) == 1
    space.factory.run_agent.assert_awaited_once()
    assert space.factory.run_agent.call_args.kwargs["message"] == "next request"
    space.admit_turn.assert_called_once()  # only the queued new request is counted
    assert MessageQueue(space.context_manager(), "ctx").items() == []


async def test_queue_of_an_already_answered_turn_is_restored(tmp_path):
    path = tmp_path / "history.json"
    manager = ContextManager(persist_path=str(path))
    manager.ensure_context("ctx")
    manager.append_message_to("ctx", "user", "first request")
    manager.append_message_to("ctx", "assistant", "answered")
    manager.update_context_metadata("ctx", {"pending_agent_run": {"turn_id": "original", "status": "completed"}})
    MessageQueue(manager, "ctx").add("next request", [], "after_turn")
    continued = []
    space = resumable_space(path, continued)
    restart = ServerRestart(SpacePool(lambda _: space), tmp_path / "restart.json", Mock())
    restart._write([{"user_id": "test", "context_id": "ctx", "turn_id": "original", "mode": "queue"}])
    await restart.recover()
    await settled(space.turns)
    assert continued == []
    space.factory.run_agent.assert_awaited_once()
    assert MessageQueue(space.context_manager(), "ctx").items() == []


async def test_failed_checkpoint_keeps_the_server_up(tmp_path):
    space = PersistentSpace("test", tmp_path / "conversation.json")
    pool = SpacePool(lambda _: space)
    async with pool.use("test"):
        pass
    space.conversations.save = Mock(side_effect=OSError("disk full"))
    stop = Mock()
    restart = ServerRestart(pool, tmp_path / "restart.json", stop)
    restart.request()
    await restart._task
    stop.assert_not_called()
    assert restart.phase == "failed" and not pool.restart_pending


async def test_background_review_or_http_work_delays_shutdown(tmp_path):
    busy = True
    stop = Mock()
    restart = ServerRestart(SpacePool(FakeSpace), tmp_path / "restart.json", stop, other_work=lambda: busy)
    restart.request()
    await asyncio.sleep(0.12)
    stop.assert_not_called()
    busy = False
    await asyncio.wait_for(restart._task, 2)
    stop.assert_called_once()


async def test_user_stop_during_restart_prevents_auto_resume(tmp_path):
    space = space_for(AsyncMock())
    session = ChatSession(space, SilentSocket(), "ctx")
    turn = SimpleNamespace(restart_paused=True, user_stopped=False, announce=AsyncMock(), request_stop=Mock())
    task = asyncio.create_task(asyncio.sleep(10))
    space.turns.register("ctx", turn, task)
    await session._stop()
    assert turn.user_stopped
    assert not task.cancelled()  # first Stop still lets the current step finish
    turn.request_stop.assert_not_called()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def server_client(tmp_path, role="admin", enabled=True):
    async def identify(_):
        return User(id="test", username="test", role=role)

    server = WebChatServer(SimpleNamespace(), SpacePool(FakeSpace), identify=identify, warm_user=None,
                           restart_path=tmp_path / "restart.json", restart_callback=Mock() if enabled else None)
    return server, TestClient(server.app, headers=SAME_SITE)


def test_restart_requires_admin_and_same_origin(tmp_path):
    server, client = server_client(tmp_path, role="user")
    assert client.post("/api/admin/server/restart").status_code == 403
    assert client.get("/api/admin/server").status_code == 403
    assert set(client.get("/api/server/status").json()) == {"phase", "instance_id"}
    server, client = server_client(tmp_path)
    assert client.post("/api/admin/server/restart", headers={"Origin": "https://evil.example"}).status_code == 403
    assert not server.restart.pending


def test_restart_api_is_idempotent_and_rejects_new_mutations(tmp_path):
    server, client = server_client(tmp_path)
    with client:
        response = client.post("/api/admin/server/restart")
        assert response.status_code == 202
        assert client.post("/api/admin/server/restart").status_code == 202
        assert client.post("/api/chat/conversations", json={}).status_code == 503
        assert client.get("/api/admin/server").status_code == 200


def test_embedded_launch_without_restart_callback_is_disabled(tmp_path):
    server, client = server_client(tmp_path, enabled=False)
    assert client.get("/api/admin/server").json()["enabled"] is False
    assert client.post("/api/admin/server/restart").status_code == 409


def test_strict_context_save_reports_failure_instead_of_hiding_it(tmp_path):
    manager = ContextManager(persist_path=str(tmp_path / "history.json"))
    manager.persist_path = tmp_path  # writing a file over a directory must fail
    with pytest.raises(OSError):
        manager.save(strict=True)
