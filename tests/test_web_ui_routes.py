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
