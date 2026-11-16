"""A socket does not outlive the session that opened it.

The audit's finding A: a chat websocket was identified once, at the handshake,
and then kept acting - new turns, policy approvals - after the session was
ended by sign-out, by disabling the account, by a role or password change, or
by expiry. The same holds for a live device view and the person's input.

These checks drive the real server and its actual authorization wiring;
the test never supplies a missing authorization callback on its behalf.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, WebSocketDisconnect, WebSocketException
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from tests.test_web_spaces import FakeSpace
from web_chat import session as session_module
from web_chat.accounts import passwords
from web_chat.accounts.http import COOKIE, SessionAuth
from web_chat.accounts.service import Accounts, SessionRevoked
from web_chat.accounts.store import AccountStore
from web_chat.devices_api import register_device_routes
from web_chat.identity import User
from web_chat.server import WebChatServer
from web_chat.session import ChatSession, SESSION_REVALIDATE_SECONDS
from web_chat.spaces import SpacePool

PASSWORD = "correct horse battery"
SAME_SITE = {"Origin": "http://testserver"}


class SpySpace(FakeSpace):
    """The fake space, plus what a socket could still change after revocation."""

    def __init__(self, user_id):
        super().__init__(user_id)
        self.reviews = []
        self.filters = []
        self.admissions = 0
        self.registry = SimpleNamespace(
            selection_is_valid=lambda system, agent: True,
            agent_label=lambda system, agent: "agent",
        )

    def answer_action_review(self, context_id, approval_id, *, approve, remember=False):
        self.reviews.append((context_id, approval_id, approve, remember))
        return True

    def set_policy_filter(self, context_id, name):
        self.filters.append((context_id, name))
        return True

    def admit_turn(self):
        self.admissions += 1
        return "refused-in-probe"


class NoDeployment:
    """A server with accounts reads nothing of its deployment until a turn runs."""


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "LOG2_N", 10)


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.db")
    yield Accounts(store)
    store.close()


def build_accounts_server(monkeypatch, accounts):
    """A real server, including the production chat-session authorization wiring."""
    spaces = {}

    def build(user_id):
        spaces[user_id] = SpySpace(user_id)
        return spaces[user_id]

    auth = SessionAuth(accounts)
    server = WebChatServer(
        NoDeployment(),
        SpacePool(build),
        auth=auth,
        trusted_hosts=("testserver",),
        warm_user=None,
    )
    return server, spaces


def login(server, username, password=PASSWORD):
    client = TestClient(server.app, headers=SAME_SITE)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def open_socket(server, client):
    """A chat socket holding the session cookie *client* signed in with."""
    token = client.cookies[COOKIE]
    ws_client = TestClient(server.app, headers=SAME_SITE)
    ws_client.cookies[COOKIE] = token
    return ws_client


def assert_closed_with_policy_violation(socket):
    with pytest.raises(WebSocketDisconnect) as closed:
        socket.receive_json()
    assert closed.value.code == 1008


# --------------------------------------------------------------------------- #
# The chat socket
# --------------------------------------------------------------------------- #


def test_a_socket_refuses_commands_and_approvals_after_logout(monkeypatch, accounts):
    server, spaces = build_accounts_server(monkeypatch, accounts)
    alice = accounts.create_user("alice", PASSWORD)
    client = login(server, "alice")
    token = client.cookies[COOKIE]
    ws_client = open_socket(server, client)

    with ws_client.websocket_connect("/api/chat/ws/ctx") as socket:
        socket.send_json({"action": "attach"})
        assert socket.receive_json()["type"] == "done"

        accounts.sign_out(token)
        assert client.get("/api/auth/me").status_code == 401

        socket.send_json({"action": "policy_review", "approval_id": "approval-1", "approve": True})
        assert_closed_with_policy_violation(socket)

    assert spaces[alice.id].reviews == []
    assert spaces[alice.id].admissions == 0


def test_a_socket_refuses_a_new_turn_after_logout(monkeypatch, accounts):
    server, spaces = build_accounts_server(monkeypatch, accounts)
    alice = accounts.create_user("alice", PASSWORD)
    client = login(server, "alice")
    token = client.cookies[COOKIE]
    ws_client = open_socket(server, client)

    with ws_client.websocket_connect("/api/chat/ws/ctx") as socket:
        socket.send_json({"action": "policy_filter", "filter": "trusted"})
        assert socket.receive_json()["ok"] is True

        accounts.sign_out(token)
        socket.send_json({"message": "do it after I signed out"})
        assert_closed_with_policy_violation(socket)

    assert spaces[alice.id].filters == [("ctx", "trusted")]
    assert spaces[alice.id].admissions == 0


def test_a_socket_refuses_approvals_after_the_account_is_disabled(monkeypatch, accounts):
    server, spaces = build_accounts_server(monkeypatch, accounts)
    accounts.create_user("root", PASSWORD, role="admin")
    alice = accounts.create_user("alice", PASSWORD)
    admin = login(server, "root")
    client = login(server, "alice")
    ws_client = open_socket(server, client)

    with ws_client.websocket_connect("/api/chat/ws/ctx") as socket:
        socket.send_json({"action": "attach"})
        assert socket.receive_json()["type"] == "done"

        assert admin.patch(f"/api/admin/users/{alice.id}", json={"disabled": True}).status_code == 200
        assert client.get("/api/auth/me").status_code == 401

        socket.send_json({"action": "policy_review", "approval_id": "approval-2", "approve": True})
        assert_closed_with_policy_violation(socket)

    assert spaces[alice.id].reviews == []


def test_a_socket_refuses_approvals_after_a_role_change(monkeypatch, accounts):
    server, spaces = build_accounts_server(monkeypatch, accounts)
    root = accounts.create_user("root", PASSWORD, role="admin")
    accounts.create_user("second_admin", PASSWORD, role="admin")
    accounts.create_user("alice", PASSWORD)
    admin_seat = login(server, "second_admin")
    root_client = login(server, "root")
    ws_client = open_socket(server, root_client)

    with ws_client.websocket_connect("/api/chat/ws/ctx") as socket:
        socket.send_json({"action": "attach"})
        assert socket.receive_json()["type"] == "done"

        assert admin_seat.patch(f"/api/admin/users/{root.id}", json={"role": "user"}).status_code == 200
        assert root_client.get("/api/auth/me").status_code == 401

        socket.send_json({"action": "policy_review", "approval_id": "approval-3", "approve": True})
        assert_closed_with_policy_violation(socket)

    assert spaces[root.id].reviews == []


def test_a_socket_refuses_approvals_after_a_password_change(monkeypatch, accounts):
    server, spaces = build_accounts_server(monkeypatch, accounts)
    alice = accounts.create_user("alice", PASSWORD)
    client = login(server, "alice")
    ws_client = open_socket(server, client)

    with ws_client.websocket_connect("/api/chat/ws/ctx") as socket:
        socket.send_json({"action": "attach"})
        assert socket.receive_json()["type"] == "done"

        accounts.change_password(alice, PASSWORD, "a whole new password", token="")
        assert client.get("/api/auth/me").status_code == 401

        socket.send_json({"action": "policy_review", "approval_id": "approval-4", "approve": True})
        assert_closed_with_policy_violation(socket)

    assert spaces[alice.id].reviews == []


# --------------------------------------------------------------------------- #
# An idle socket: no command arrives to trigger the check
# --------------------------------------------------------------------------- #


class BlockingSocket:
    """A socket whose reader waits until the session watcher closes it."""

    def __init__(self):
        self.closed = asyncio.Event()
        self.closed_code = None

    async def accept(self):
        pass

    async def receive_text(self):
        await self.closed.wait()
        raise WebSocketDisconnect(1008)

    async def send_json(self, event):
        pass

    async def close(self, code=1000, reason=""):
        self.closed_code = code
        self.closed.set()


class RecordingSocket:
    """A socket that only records what a refused command did."""

    def __init__(self):
        self.closed_code = None
        self.sent = []

    async def send_json(self, event):
        self.sent.append(event)

    async def close(self, code=1000, reason=""):
        self.closed_code = code


async def test_an_idle_socket_closes_when_its_session_ends():
    space = SpySpace("alice")
    socket = BlockingSocket()
    revoked = asyncio.Event()

    async def authorize():
        if revoked.is_set():
            raise SessionRevoked("The session ended")
        return User(id="alice", username="alice", role="user")

    session = ChatSession(space, socket, "ctx", authorize=authorize, revalidate_interval=0.01)
    serving = asyncio.create_task(session.serve())
    await asyncio.sleep(0.05)
    assert not socket.closed.is_set()

    revoked.set()
    await asyncio.wait_for(socket.closed.wait(), 2)
    await asyncio.wait_for(serving, 2)
    assert socket.closed_code == 1008


async def test_a_command_is_refused_before_it_reaches_the_space():
    space = SpySpace("alice")
    socket = RecordingSocket()

    async def authorize():
        raise SessionRevoked("The session ended")

    session = ChatSession(space, socket, "ctx", authorize=authorize)
    await session._dispatch(
        '{"action": "policy_review", "approval_id": "approval-1", "approve": true}'
    )
    assert space.reviews == []
    assert socket.closed_code == 1008


async def test_a_session_that_maps_to_another_user_is_treated_as_ended():
    space = SpySpace("alice")
    socket = RecordingSocket()

    async def authorize():
        return User(id="bob", username="bob", role="user")

    session = ChatSession(space, socket, "ctx", authorize=authorize)
    await session._dispatch('{"action": "policy_filter", "filter": "trusted"}')
    assert space.filters == []
    assert socket.closed_code == 1008


def test_the_idle_revalidation_interval_is_bounded():
    assert 0 < SESSION_REVALIDATE_SECONDS <= 60


# --------------------------------------------------------------------------- #
# The live device view
# --------------------------------------------------------------------------- #


class FakeHub:
    """A device hub that hands the view one device and records its input."""

    def __init__(self):
        self.inputs = []

    @asynccontextmanager
    async def watching(self, user_id, device_id, sink):
        yield SimpleNamespace(id=device_id)

    async def person_input(self, user_id, device_id, event):
        self.inputs.append((user_id, device_id, event))


def device_app(hub, allowed):
    app, api = FastAPI(), APIRouter()

    async def current_user(connection: HTTPConnection):
        if not allowed():
            raise WebSocketException(code=1008, reason="Sign in to continue")
        return SimpleNamespace(id="alice")

    register_device_routes(app, api, current_user, hub=hub, machines=None, pairing=None)
    app.include_router(api)
    return app


def test_a_live_device_view_stops_after_the_session_ends():
    hub = FakeHub()
    allowed = {"now": True}
    app = device_app(hub, lambda: allowed["now"])
    client = TestClient(app)

    with client.websocket_connect("/api/devices/desktop-1/view") as socket:
        socket.send_json({"type": "input", "x": 1})
        # The first input is passed on; then the session ends and the same view
        # is refused before the second input reaches the device.
        allowed["now"] = False
        socket.send_json({"type": "input", "x": 2})
        assert_closed_with_policy_violation(socket)

    assert [event["x"] for _, _, event in hub.inputs] == [1]


async def test_an_idle_socket_fails_closed_when_authorization_cannot_be_checked():
    socket = BlockingSocket()

    async def authorize():
        raise RuntimeError("accounts backend unavailable")

    session = ChatSession(SpySpace("alice"), socket, "ctx", authorize=authorize, revalidate_interval=0.01)
    serving = asyncio.create_task(session.serve())
    await asyncio.wait_for(socket.closed.wait(), 2)
    await asyncio.wait_for(serving, 2)
    assert socket.closed_code == 1008


async def test_a_device_view_fails_closed_on_authorization_backend_error():
    from web_chat.devices_api import _revalidated

    async def unavailable(connection):
        raise RuntimeError("accounts backend unavailable")

    with pytest.raises(SessionRevoked):
        await _revalidated(unavailable, SimpleNamespace(), SimpleNamespace(id="alice"))
