"""A server with accounts: sign-in, sessions over HTTP and sockets, admins, origins."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.test_web_spaces import FakeSpace
from web_chat.accounts import passwords
from web_chat.accounts.http import COOKIE, PER_ACCOUNT_LIMIT, SessionAuth
from web_chat.accounts.service import Accounts
from web_chat.accounts.store import AccountStore
from web_chat.security import OriginGuard
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool

PASSWORD = "correct horse battery"
SAME_SITE = {"Origin": "http://testserver"}


class Deployment:
    """What the server reads of the deployment in these tests: voice settings."""

    def voice_source(self):
        return "voice.yaml", None

    def voice_config_dict(self):
        return {}


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "LOG2_N", 10)


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.db")
    yield Accounts(store)
    store.close()


@pytest.fixture
def server(accounts):
    return WebChatServer(Deployment(), SpacePool(FakeSpace), auth=SessionAuth(accounts), warm_user=None)


@pytest.fixture
def client(server):
    return TestClient(server.app, headers=SAME_SITE)


def signed_in(server, username, password=PASSWORD):
    """A client of *server* that signed in as *username*."""
    client = TestClient(server.app, headers=SAME_SITE)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


# -- pages and sign-in -----------------------------------------------------------


def test_the_chat_page_sends_strangers_to_sign_in(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.get("/login").status_code == 200


def test_signing_in_sets_a_cookie_scripts_cannot_read(server, accounts):
    accounts.create_user("alice", PASSWORD)
    client = TestClient(server.app, headers=SAME_SITE)

    response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})

    cookie = response.headers["set-cookie"]
    assert COOKIE in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in cookie  # plain http in the test; https sets it
    assert client.get("/api/auth/me").json()["username"] == "alice"
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303


def test_a_wrong_password_is_refused_and_then_rate_limited(client, accounts):
    accounts.create_user("alice", PASSWORD)
    wrong = {"username": "alice", "password": "not the password"}

    for _ in range(PER_ACCOUNT_LIMIT):
        assert client.post("/api/auth/login", json=wrong).status_code == 401
    limited = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})

    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0


def test_signing_out_ends_the_session_on_the_server(server, accounts):
    accounts.create_user("alice", PASSWORD)
    client = signed_in(server, "alice")
    token = client.cookies[COOKIE]

    client.post("/api/auth/logout")

    assert accounts.user_for_session(token) is None
    assert client.get("/api/chat/conversations").status_code == 401


def test_an_invite_link_signs_the_new_user_in(client, accounts):
    code, _ = accounts.create_invite(None)

    response = client.post("/api/auth/register", json={"invite": code, "username": "carol", "password": PASSWORD})

    assert response.status_code == 201
    assert client.get("/api/auth/me").json()["username"] == "carol"
    again = client.post("/api/auth/register", json={"invite": code, "username": "dave", "password": PASSWORD})
    assert again.status_code == 400


# -- what a session opens --------------------------------------------------------


def test_the_api_refuses_requests_without_a_session(client):
    assert client.get("/api/chat/bootstrap").status_code == 401
    assert client.post("/api/chat/conversations").status_code == 401
    assert client.get("/api/voice/status").status_code == 401


def test_each_user_sees_only_their_own_conversations(server, accounts):
    accounts.create_user("alice", PASSWORD)
    accounts.create_user("bob", PASSWORD)
    alice, bob = signed_in(server, "alice"), signed_in(server, "bob")

    chat = alice.post("/api/chat/conversations").json()["id"]

    assert [row["id"] for row in alice.get("/api/chat/conversations").json()] == [chat]
    assert bob.get("/api/chat/conversations").json() == []
    assert bob.get(f"/api/chat/conversations/{chat}").status_code == 404
    assert bob.patch(f"/api/chat/conversations/{chat}", json={"title": "mine"}).status_code == 404


def test_a_chat_socket_needs_a_session(server, accounts):
    accounts.create_user("alice", PASSWORD)
    stranger = TestClient(server.app, headers=SAME_SITE)

    with pytest.raises(WebSocketDisconnect) as refused:
        with stranger.websocket_connect("/api/chat/ws/ctx"):
            pass
    assert refused.value.code == 1008

    alice = signed_in(server, "alice")
    with alice.websocket_connect("/api/chat/ws/ctx") as socket:
        socket.send_json({"action": "attach"})
        assert socket.receive_json()["type"] == "done"


def test_a_signed_in_user_is_not_an_admin(server, accounts):
    accounts.create_user("alice", PASSWORD)
    alice = signed_in(server, "alice")

    assert alice.get("/api/admin/users").status_code == 403
    assert alice.post("/api/admin/invites", json={}).status_code == 403
    assert alice.get("/api/settings").status_code == 403
    assert alice.put("/api/settings/yaml", json={"yaml_content": "x: 1"}).status_code == 403


def test_an_admin_invites_and_disables_users(server, accounts):
    accounts.create_user("root", PASSWORD, role="admin")
    alice_user = accounts.create_user("alice", PASSWORD)
    root, alice = signed_in(server, "root"), signed_in(server, "alice")

    invite = root.post("/api/admin/invites", json={"role": "user", "days": 1, "note": "for carol"}).json()
    assert invite["code"] and invite["note"] == "for carol"
    assert "code" not in root.get("/api/admin/invites").json()[0]

    assert root.patch(f"/api/admin/users/{alice_user.id}", json={"disabled": True}).status_code == 200
    assert alice.get("/api/chat/conversations").status_code == 401
    assert root.patch(f"/api/admin/users/{root.get('/api/auth/me').json()['id']}", json={"disabled": True}).status_code == 400


def test_a_password_change_keeps_this_session_and_ends_the_others(server, accounts):
    accounts.create_user("alice", PASSWORD)
    here, elsewhere = signed_in(server, "alice"), signed_in(server, "alice")

    response = here.post("/api/auth/password", json={"current": PASSWORD, "new": "a new long password"})

    assert response.status_code == 200
    assert here.get("/api/auth/me").status_code == 200
    assert elsewhere.get("/api/auth/me").status_code == 401


# -- origins ---------------------------------------------------------------------


def test_another_site_cannot_act_with_the_users_cookie(server, accounts):
    accounts.create_user("alice", PASSWORD)
    alice = signed_in(server, "alice")

    forged = alice.post("/api/chat/conversations", headers={"Origin": "https://evil.example"})
    read = alice.get("/api/chat/conversations", headers={"Origin": "https://evil.example"})

    assert forged.status_code == 403
    assert read.status_code == 200  # reading changes nothing; CORS keeps the answer from the other site
    with pytest.raises(WebSocketDisconnect):
        with alice.websocket_connect("/api/chat/ws/ctx", headers={"Origin": "https://evil.example"}):
            pass


class Connection:
    def __init__(self, method="POST", kind="http", **headers):
        self.scope = {"type": kind, "method": method}
        self.headers = {key.replace("_", "-"): value for key, value in headers.items()}


@pytest.mark.parametrize(
    "connection, allowed",
    [
        (Connection(method="GET"), True),
        (Connection(origin="http://chat.local:8000", host="chat.local:8000"), True),
        (Connection(origin="HTTP://Chat.Local:8000", host="chat.local:8000"), True),
        (Connection(origin="http://chat.local:9000", host="chat.local:8000"), False),
        (Connection(origin="null", host="chat.local:8000"), False),
        (Connection(host="chat.local:8000", sec_fetch_site="same-origin"), True),
        (Connection(host="chat.local:8000", sec_fetch_site="cross-site"), False),
        (Connection(host="chat.local:8000"), False),
        (Connection(kind="websocket", origin="http://chat.local:8000", host="chat.local:8000"), True),
        (Connection(kind="websocket", origin="https://evil.example", host="chat.local:8000"), False),
        (Connection(origin="https://chat.example.com", host="127.0.0.1:8000"), True),
    ],
)
def test_the_origin_guard(connection, allowed):
    guard = OriginGuard(["https://chat.example.com/"])

    assert guard.allows(connection) is allowed


# -- action reviews --------------------------------------------------------------


class ReviewingSpace(FakeSpace):
    """A fake space whose policy gate holds one pending review."""

    def __init__(self, user_id):
        super().__init__(user_id)
        self.reviews = {f"review-{user_id}": {"approval_id": f"review-{user_id}", "tool": "bash_tool", "created_at": 1}}
        self.resolved = []

    def pending_action_reviews(self):
        return list(self.reviews.values())

    def resolve_action_review(self, approval_id, *, approve):
        if self.reviews.pop(approval_id, None) is None:
            return False
        self.resolved.append((approval_id, approve))
        return True


def test_admins_review_the_actions_of_every_users_agents(accounts):
    accounts.create_user("root", PASSWORD, role="admin")
    alice = accounts.create_user("alice", PASSWORD)
    spaces = {}
    pool = SpacePool(lambda user_id: spaces.setdefault(user_id, ReviewingSpace(user_id)))
    server = WebChatServer(Deployment(), pool, auth=SessionAuth(accounts), warm_user=None)
    alice_client = signed_in(server, "alice")
    alice_client.get("/api/chat/conversations")  # alice's space is loaded
    root = signed_in(server, "root")

    assert alice_client.get("/api/action-policy/reviews").status_code == 403
    pending = root.get("/api/action-policy/reviews").json()
    assert {(review["username"], review["tool"]) for review in pending} == {("root", "bash_tool"), ("alice", "bash_tool")}

    decided = root.post(f"/api/action-policy/reviews/review-{alice.id}", json={"decision": "deny"})

    assert decided.status_code == 200
    assert spaces[alice.id].resolved == [(f"review-{alice.id}", False)]
    assert root.post("/api/action-policy/reviews/missing", json={"decision": "approve"}).status_code == 404


def test_every_response_carries_the_security_headers(client):
    from web_chat.server import SECURITY_HEADERS

    for response in (client.get("/login"), client.get("/api/chat/bootstrap"), client.get("/static/styles/auth.css")):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value
