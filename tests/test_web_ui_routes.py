from types import SimpleNamespace
from pathlib import Path

from fastapi.testclient import TestClient

from timeline.server import create_app as create_timeline_app
from web_chat.server import WebChatServer
from web_chat.systems import SystemInfo


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


class _DummyRuntime:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            config=SimpleNamespace(
                settings=SimpleNamespace(default_agent="test_agent")
            )
        )
        self.workspace_path = "workspace/test"
        self.config_path = Path("config.yaml")
        self.persist_path = "data/test"
        self.container_id = None
        self.action_review_token = "operator-token"
        self.registry = _DummyRegistry()
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

    async def warm_default_agent(self) -> None:
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


def test_timeline_index_serves_dashboard_html():
    client = TestClient(create_timeline_app())

    response = client.get("/")

    assert response.status_code == 200
    assert "Agent Timeline" in response.text


def test_web_chat_index_and_bootstrap_are_available():
    client = TestClient(WebChatServer(_DummyRuntime()).app)

    index_response = client.get("/")
    bootstrap_response = client.get("/api/chat/bootstrap")

    assert index_response.status_code == 200
    assert "Grid Chat" in index_response.text
    assert bootstrap_response.status_code == 200
    assert bootstrap_response.json()["default_system"] == "test_system"


def test_action_review_routes_list_and_resolve_pending_review():
    runtime = _DummyRuntime()
    client = TestClient(WebChatServer(runtime).app)

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
    assert runtime.review_resolution == ("review-1", True)


def test_action_review_route_rejects_unknown_or_expired_review():
    client = TestClient(WebChatServer(_DummyRuntime()).app)

    response = client.post(
        "/api/action-policy/reviews/missing",
        json={"decision": "deny"},
        headers={"X-Grid-Action-Review-Token": "operator-token"},
    )

    assert response.status_code == 404


def test_action_review_routes_require_the_operator_token():
    client = TestClient(WebChatServer(_DummyRuntime()).app)

    assert client.get("/api/action-policy/reviews").status_code == 403


def _chat_server_with_contexts():
    from core.context import ContextManager

    runtime = _DummyRuntime()
    manager = ContextManager()
    runtime._context_manager = manager
    for context_id, text in (("ctx-a", "first chat"), ("ctx-b", "second chat")):
        manager.start_new_context(context_id)
        manager.add_message("user", text)
    manager.add_tool_result_as_message("Agent", "sub-agent report")
    server = WebChatServer(runtime)
    return server, manager, TestClient(server.app)


def test_web_chat_renames_a_conversation_and_keeps_the_title():
    server, manager, client = _chat_server_with_contexts()

    response = client.patch("/api/chat/conversations/ctx-a", json={"title": "  My   chat "})

    assert response.status_code == 200
    assert response.json()["title"] == "My chat"
    listed = {item["id"]: item for item in client.get("/api/chat/conversations").json()}
    assert listed["ctx-a"]["title"] == "My chat"
    assert client.patch("/api/chat/conversations/missing", json={"title": "x"}).status_code == 404


def test_web_chat_deletes_a_conversation_but_not_a_running_one():
    server, manager, client = _chat_server_with_contexts()

    class Running:
        def done(self):
            return False

    server._chat_turns["ctx-a"] = (SimpleNamespace(message="m", elapsed_ms=5), Running())
    assert client.delete("/api/chat/conversations/ctx-a").status_code == 409
    listed = {item["id"]: item for item in client.get("/api/chat/conversations").json()}
    assert listed["ctx-a"]["active"] is True and listed["ctx-b"]["active"] is False
    # The sub-agent report is context for the model, not a chat message.
    assert listed["ctx-b"]["message_count"] == 1

    assert client.delete("/api/chat/conversations/ctx-b").status_code == 200
    assert "ctx-b" not in manager._contexts
    assert manager._current_context_id != "ctx-b"
    assert client.delete("/api/chat/conversations/ctx-b").status_code == 404
