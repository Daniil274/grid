"""The server's own door: which Host it answers, which Origin may act, what
``/static`` serves, and that the CLI never starts unauthenticated by default.

Each check is the regression of an audit finding (S2, S3, S5 of
grid-security-audit-2026-11-06.md): the package's Python sources were public
under ``/static``; Host was trusted only because Origin repeated it; and a
bare ``grid-web-chat`` silently served one admin to anyone.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from web_chat.__main__ import parse_args, serve_mode, trusted_hosts_for
from web_chat.identity import User
from web_chat.security import OriginGuard, TrustedHosts
from web_chat.server import PUBLIC_STATIC_FILES, WebChatServer, is_public_static
from web_chat.spaces import SpacePool
from web_chat.turns import TurnBoard


class FakeSpace:
    """A space with a TurnBoard and real conversations, as the server lends it."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.turns = TurnBoard()
        from core.context import ContextManager

        self.conversations = ContextManager()

    @property
    def idle(self) -> bool:
        return self.turns.idle

    async def close(self) -> None:
        pass

    def context_manager(self):
        return self.conversations

    def update_conversation_metadata(self, context_id, **updates):
        self.conversations.update_context_metadata(
            context_id, {key: value for key, value in updates.items() if value is not None}
        )

    def schedule_warmup(self) -> None:
        pass

    async def registry_for(self, context_id):
        return self.registry


async def _identify(connection):
    return User(id="alice", username="alice", role="user")


def build_server(**options):
    from types import SimpleNamespace

    deployment = SimpleNamespace(voice_source=lambda: ("voice.yaml", None), voice_config_dict=lambda: {})
    # TestClient's default address is http://testserver; a programmatic server
    # answers loopback names only until the names it is published under are named.
    options.setdefault("trusted_hosts", ("testserver",))
    return WebChatServer(
        deployment,
        SpacePool(lambda user_id: FakeSpace(user_id)),
        identify=_identify,
        warm_user=None,
        **options)


# -- S3: /static serves the client, not the package -----------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(build_server().app) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    [
        "/static/server.py",
        "/static/security.py",
        "/static/__main__.py",
        "/static/identity.py",
        "/static/accounts/__init__.py",
        "/static/accounts/http.py",
        "/static/server.pyc",
        "/static/js/../server.py",
        "/static/styles/../../server.py",
        "/static/.env",
        "/static/js/lib/katex/LICENSE/../../../../../server.py",
    ],
)
def test_server_sources_have_no_public_name(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/static/js/main.js",
        "/static/js/grid/main.js",
        "/static/js/lib/katex/katex.min.css",
        "/static/styles/base.css",
        "/static/voice.js",
        "/static/voice-capture.js",
    ],
)
def test_the_clients_own_files_are_served(client, path):
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.headers["Cache-Control"] == "no-cache"


def test_the_static_allowlist_is_paths_not_one_extension():
    for path in ("server.py", "accounts/http.py", "js/../server.py", "../server.py", "data/records.db", ".env"):
        assert not is_public_static(path), path
    for path in ("js/main.js", "styles/base.css", *PUBLIC_STATIC_FILES):
        assert is_public_static(path), path
    # A server file hidden in a client directory is still not a client file.
    assert not is_public_static("js/server.py")
    assert not is_public_static("styles/records.sqlite3")


# -- S5: the Host the server answers, and the full Origin that may act ----------


class Connection:
    """The part of a connection the guard reads, for the unit checks."""

    def __init__(self, method="POST", kind="http", scheme=None, **headers):
        self.scope = {"type": kind, "method": method}
        if scheme is not None:
            self.scope["scheme"] = scheme
        self.headers = {key.replace("_", "-"): value for key, value in headers.items()}


def test_only_loopback_and_configured_hosts_are_trusted():
    hosts = TrustedHosts(["chat.example.com", "internal.example.com:8443"])

    assert hosts.allows("chat.example.com")
    assert hosts.allows("CHAT.EXAMPLE.COM:443")
    assert hosts.allows("chat.example.com:80")
    assert hosts.allows("internal.example.com:8443")
    assert hosts.allows("localhost:8000") and hosts.allows("127.0.0.1:9000") and hosts.allows("[::1]:1234")
    assert not hosts.allows("internal.example.com:80")
    assert not hosts.allows("evil.example")
    assert not hosts.allows("chat.example.com.evil.example")
    assert not hosts.allows("")


def test_a_request_to_another_host_is_refused_before_any_route():
    server = build_server(trusted_hosts=("chat.example.com",))
    with TestClient(server.app) as client:
        for path in ("/", "/static/js/main.js", "/api/chat/bootstrap"):
            assert client.get(path, headers={"Host": "evil.example"}).status_code == 400, path
        assert client.get("/", headers={"Host": "chat.example.com"}).status_code == 200
        # Loopback names are the machine's own and always answered.
        assert client.get("/", headers={"Host": "127.0.0.1:8000"}).status_code == 200
        assert client.get("/", headers={"Host": "localhost:8000"}).status_code == 200


def test_a_socket_to_another_host_is_closed():
    server = build_server(trusted_hosts=("chat.example.com",))
    with TestClient(server.app) as client:
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/api/chat/ws/ctx", headers={"Host": "evil.example"}):
                pass
        assert refused.value.code == 1008


def test_the_public_names_of_allowed_origins_are_trusted_hosts():
    server = build_server(allowed_origins=("https://chat.example.com",), trusted_hosts=())
    with TestClient(server.app) as client:
        assert client.get("/", headers={"Host": "chat.example.com"}).status_code == 200
        assert client.get("/", headers={"Host": "other.example"}).status_code == 400


def test_a_page_of_another_site_cannot_act_even_when_it_repeats_its_host():
    """The old guard accepted any Origin whose netloc equalled Host."""
    server = build_server(trusted_hosts=())
    with TestClient(server.app) as client:
        forged = client.post(
            "/api/chat/conversations",
            headers={"Host": "evil.example", "Origin": "http://evil.example"},
        )
        assert forged.status_code == 400


def test_the_origin_is_compared_whole():
    guard = OriginGuard(["https://chat.example.com"])

    assert guard.allows(Connection(scheme="https", origin="https://chat.example.com", host="127.0.0.1:8000"))
    assert not guard.allows(Connection(scheme="http", origin="http://chat.example.com", host="127.0.0.1:8000"))
    assert not guard.allows(Connection(scheme="https", origin="http://chat.example.com", host="chat.example.com"))
    assert not guard.allows(Connection(scheme="https", origin="null", host="chat.example.com"))


def test_a_server_page_acts_over_the_scheme_it_arrived_on():
    server = build_server(trusted_hosts=("chat.example.com",))
    with TestClient(server.app, base_url="https://chat.example.com") as client:
        same = client.post(
            "/api/chat/conversations",
            headers={"Origin": "https://chat.example.com"},
        )
        downgraded = client.post(
            "/api/chat/conversations",
            headers={"Origin": "http://chat.example.com"},
        )
    assert same.status_code == 200
    assert downgraded.status_code == 403


def test_a_configured_origin_may_act():
    server = build_server(allowed_origins=("https://chat.example.com",), trusted_hosts=("chat.example.com",))
    with TestClient(server.app) as client:
        response = client.post(
            "/api/chat/conversations",
            headers={"Host": "chat.example.com", "Origin": "https://chat.example.com"},
        )
    assert response.status_code == 200


# -- S2: the CLI never starts unauthenticated by guessing -----------------------


def test_the_cli_refuses_to_choose_no_authentication_by_default():
    with pytest.raises(SystemExit) as refused:
        serve_mode(parse_args([]))

    assert "--accounts" in str(refused.value) and "--unsafe-single-user" in str(refused.value)


def test_both_ways_to_start_are_explicit():
    assert serve_mode(parse_args(["--accounts"])) == "accounts"
    assert serve_mode(parse_args(["--unsafe-single-user"])) == "single-user"
    # With accounts asked for, that is the mode even if the flag is also there.
    assert serve_mode(parse_args(["--accounts", "--unsafe-single-user"])) == "accounts"


def test_the_host_allowlist_comes_from_the_command_line():
    assert "127.0.0.1:9000" in trusted_hosts_for(
        parse_args(["--unsafe-single-user", "--host", "127.0.0.1", "--port", "9000"])
    )
    assert trusted_hosts_for(parse_args(["--accounts", "--host", "0.0.0.0"])) == ()
    hosts = trusted_hosts_for(parse_args(["--accounts", "--trusted-host", "chat.example.com"]))
    assert "chat.example.com" in hosts
    # The concrete bind address is the deployment's own name.
    assert "127.0.0.1:8000" in hosts
