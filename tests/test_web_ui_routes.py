from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from schemas.schemas import UploadsPolicy
from timeline.server import create_app as create_timeline_app
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool
from web_chat.systems import SystemInfo
from web_chat.turns import TurnBoard

#: What the chat's own pages send with a request that changes something.
SAME_SITE = {"Origin": "http://testserver"}


class _DummyContextManager:
    def __init__(self) -> None:
        self._lock = None
        self._contexts = {}

    def get_current_context_id(self):
        return None


class _DummyRegistry:
    """One system, one agent - enough for the route contract."""

    AGENTS = {
        "test_agent": SimpleNamespace(
            name="Test Agent", description="Testing agent", model="gpt-4",
            tools=[], mcp_enabled=False, routable=True,
        )
    }

    can_route = False
    has_catalog = False

    def keys(self):
        return ["test_system"]

    def default_key(self):
        return "test_system"

    def systems(self):
        return [SystemInfo("test_system", "Test System", "A test system", Path("config.yaml"))]

    def config(self, system_key):
        return SimpleNamespace(
            config=SimpleNamespace(models={"gpt-4": SimpleNamespace(name="gpt-4", description="Test model")}),
            get_default_agent=lambda: "test_agent",
        )

    def agents(self, system_key):
        return dict(self.AGENTS)

    def has_agent(self, system_key, agent_key):
        return agent_key in self.AGENTS


class _DummySpace:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            config=SimpleNamespace(
                settings=SimpleNamespace(default_agent="test_agent")
            )
        )
        self.user_id = "default_user"
        self.workspace_path = "workspace/test"
        self.conversations_path = "data/test/context.json"
        self.container_id = None
        self.registry = _DummyRegistry()
        self.turns = TurnBoard()
        self.personal_agents = None
        self._context_manager = _DummyContextManager()
        self._reviews = [
            {
                "approval_id": "review-1",
                "run_id": "run-1",
                "action_sha256": "abc",
                "tool": "file_delete",
                "kind": "function",
                "created_at": 1,
                "expires_at": 2,
                "system_key": "test_system",
            }
        ]

    @property
    def idle(self) -> bool:
        return self.turns.idle

    async def close(self) -> None:
        return None

    def schedule_warmup(self) -> None:
        return None

    async def warm_agent(self, agent_key: str, system_key: str | None = None) -> None:
        return None

    def context_manager(self):
        return self._context_manager

    def update_conversation_metadata(self, context_id: str, **updates):
        return None

    def pending_action_reviews(self):
        return list(self._reviews)

    def resolve_action_review(self, approval_id: str, *, approve: bool):
        if approval_id != "review-1":
            return False
        self._reviews = []
        self.review_resolution = (approval_id, approve)
        return True


class _DummyDeployment:
    config_path = Path("config.yaml")
    uploads_policy = UploadsPolicy()

    def voice_source(self):
        return self.config_path, None

    def voice_config_dict(self):
        return {}

    def voice_enabled(self):
        return False

    def config_dict(self):
        return {
            "agents": {
                "test_agent": {
                    "name": "Test Agent",
                    "description": "Testing agent",
                    "model": "gpt-4",
                    "tools": [],
                    "mcp_enabled": False,
                }
            },
            "models": {
                "gpt-4": {
                    "name": "gpt-4",
                    "description": "Test model",
                }
            },
            "tools": {},
            "prompt_templates": {},
        }


def _server(space=None) -> WebChatServer:
    """A single-user server lending *space* to every request."""
    space = space or _DummySpace()
    return WebChatServer(
        _DummyDeployment(),
        SpacePool(lambda user_id: space),
        action_review_token="operator-token",
    )


def test_timeline_index_serves_dashboard_html():
    client = TestClient(create_timeline_app())

    response = client.get("/")

    assert response.status_code == 200
    assert "Agent Timeline" in response.text


def test_web_chat_index_and_bootstrap_are_available():
    client = TestClient(_server().app, headers=SAME_SITE)

    index_response = client.get("/")
    bootstrap_response = client.get("/api/chat/bootstrap")

    assert index_response.status_code == 200
    assert "Grid Chat" in index_response.text
    assert bootstrap_response.status_code == 200
    assert bootstrap_response.json()["default_system"] == "test_system"
    assert bootstrap_response.json()["voice"] is False


@pytest.mark.asyncio
async def test_bootstrap_survives_optional_tool_system_exit(tmp_path, sample_config):
    """Listing a catalog must not exit when a desktop dependency calls sys.exit."""
    from copy import deepcopy

    import yaml
    from httpx import ASGITransport, AsyncClient

    from core.config import Config
    from core.managers.project_tools_loader import get_project_loader, set_project_loader
    from web_chat.systems import SystemRegistry

    base_path = tmp_path / "base.yaml"
    base_path.write_text(yaml.safe_dump(sample_config), encoding="utf-8")
    desktop = deepcopy(sample_config)
    desktop["settings"]["project_tools"] = {"enabled": True, "tools_directory": "./tools"}
    desktop["agents"]["test_agent"]["tools"].append("desktop_tool")
    desktop["tools"]["desktop_tool"] = {"type": "function", "description": "Desktop tool"}
    desktop_path = tmp_path / "desktop.yaml"
    desktop_path.write_text(yaml.safe_dump(desktop), encoding="utf-8")
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "desktop_tool.py").write_text(
        "from utils.tool_requirements import Requires\n"
        "TOOL_REQUIREMENTS = {'desktop_tool': Requires(platform='win32', hint='Run on Windows')}\n"
        "raise SystemExit('Tkinter is unavailable')\n",
        encoding="utf-8",
    )
    catalog_data = deepcopy(sample_config)
    catalog_data["routing"] = {
        "model": "gpt-4",
        "default_system": "engineering",
        "systems": {
            "engineering": {"config": "base.yaml", "description": "Coding"},
            "desktop": {"config": "desktop.yaml", "description": "Windows desktop"},
        },
    }
    catalog_path = tmp_path / "routing.yaml"
    catalog_path.write_text(yaml.safe_dump(catalog_data), encoding="utf-8")
    previous_loader = get_project_loader()
    try:
        space = _DummySpace()
        space.registry = SystemRegistry(
            base_config=Config(str(base_path)),
            catalog=Config(str(catalog_path)),
            build_factory=lambda config: None,
            working_directory=str(tmp_path),
        )
        server = _server(space)

        async def current_space():
            return space

        # This test covers config loading during bootstrap, not the pool's
        # threaded workspace/container construction.
        server.app.dependency_overrides[server.current_space] = current_space
        async with AsyncClient(
            transport=ASGITransport(app=server.app),
            base_url="http://testserver",
            headers=SAME_SITE,
        ) as client:
            for _ in range(2):
                response = await client.get("/api/chat/bootstrap")
                assert response.status_code == 200
                systems = {system["key"]: system for system in response.json()["systems"]}
                assert systems["engineering"]["agents"]
                assert systems["desktop"]["error"] == ""
                [agent] = systems["desktop"]["agents"]
                [issue] = agent["issues"]["items"]
                assert issue["tool"] == "desktop_tool"
                assert issue["kind"] == "environment"
            assert (await client.get("/api/server/status")).status_code == 200
        loader = space.registry.config("desktop").project_tools_loader
        assert loader.load_errors["desktop_tool.py"] == "SystemExit: Tkinter is unavailable"
        assert not loader.has_tool("desktop_tool")
    finally:
        set_project_loader(previous_loader)


def test_a_single_user_server_closes_its_voice_when_it_stops():
    server = WebChatServer(_DummyDeployment(), SpacePool(lambda user_id: _DummySpace()), warm_user=None)

    with TestClient(server.app, headers=SAME_SITE):
        assert server.voice is not None and not server.voice._closed

    assert server.voice._closed


def test_action_review_routes_list_and_resolve_pending_review():
    space = _DummySpace()
    client = TestClient(_server(space).app, headers=SAME_SITE)

    headers = {"X-Grid-Action-Review-Token": "operator-token"}
    pending = client.get("/api/action-policy/reviews", headers=headers)
    approved = client.post(
        "/api/action-policy/reviews/review-1",
        json={"decision": "approve"},
        headers=headers,
    )

    assert pending.status_code == 200
    assert pending.json()[0]["approval_id"] == "review-1"
    assert "arguments" not in pending.json()[0]
    assert approved.status_code == 200
    assert space.review_resolution == ("review-1", True)


def test_action_review_route_rejects_unknown_or_expired_review():
    client = TestClient(_server().app, headers=SAME_SITE)

    response = client.post(
        "/api/action-policy/reviews/missing",
        json={"decision": "deny"},
        headers={"X-Grid-Action-Review-Token": "operator-token"},
    )

    assert response.status_code == 404


def test_action_review_routes_require_the_operator_token():
    client = TestClient(_server().app, headers=SAME_SITE)

    assert client.get("/api/action-policy/reviews").status_code == 403


def _chat_server_with_contexts():
    """(space, its conversations, a client of a server lending the space)."""
    from core.context import ContextManager

    space = _DummySpace()
    manager = ContextManager()
    space._context_manager = manager
    for context_id, text in (("ctx-a", "first chat"), ("ctx-b", "second chat")):
        manager.start_new_context(context_id)
        manager.add_message("user", text)
    manager.add_tool_result_as_message("Agent", "sub-agent report")
    server = _server(space)
    return space, manager, TestClient(server.app, headers=SAME_SITE)


def test_web_chat_renames_a_conversation_and_keeps_the_title():
    space, manager, client = _chat_server_with_contexts()

    response = client.patch("/api/chat/conversations/ctx-a", json={"title": "  My   chat "})

    assert response.status_code == 200
    assert response.json()["title"] == "My chat"
    listed = {item["id"]: item for item in client.get("/api/chat/conversations").json()}
    assert listed["ctx-a"]["title"] == "My chat"
    assert client.patch("/api/chat/conversations/missing", json={"title": "x"}).status_code == 404


def test_web_chat_deletes_a_conversation_but_not_a_running_one():
    space, manager, client = _chat_server_with_contexts()

    class Running:
        def done(self):
            return False

    space.turns.register("ctx-a", SimpleNamespace(message="m", elapsed_ms=5), Running())
    assert client.delete("/api/chat/conversations/ctx-a").status_code == 409
    listed = {item["id"]: item for item in client.get("/api/chat/conversations").json()}
    assert listed["ctx-a"]["active"] is True and listed["ctx-b"]["active"] is False
    # The sub-agent report is context for the model, not a chat message.
    assert listed["ctx-b"]["message_count"] == 1

    assert client.delete("/api/chat/conversations/ctx-b").status_code == 200
    assert "ctx-b" not in manager._contexts
    assert manager._current_context_id != "ctx-b"
    assert client.delete("/api/chat/conversations/ctx-b").status_code == 404


def test_the_timeline_takes_changes_only_from_its_own_page():
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(create_timeline_app())
    rerun = "/api/traces/t/nodes/n/rerun"

    assert client.post(rerun, json={"messages": []}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(rerun, json={"messages": []}).status_code == 403  # no origin at all
    assert client.post(rerun, json={"messages": []}, headers=SAME_SITE).status_code != 403
    assert client.get("/").status_code == 200
    try:
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}):
            raise AssertionError("a cross-site socket was accepted")
    except WebSocketDisconnect as closed:
        assert closed.code == 1008
