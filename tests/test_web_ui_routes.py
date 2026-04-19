from types import SimpleNamespace

from fastapi.testclient import TestClient

from timeline.server import create_app as create_timeline_app
from web_chat.server import WebChatServer


class _DummyContextManager:
    def __init__(self) -> None:
        self._lock = None
        self._contexts = {}

    def get_current_context_id(self):
        return None


class _DummyRuntime:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            config=SimpleNamespace(
                settings=SimpleNamespace(default_agent="test_agent")
            )
        )
        self.workspace_path = "workspace/test"
        self.persist_path = "data/test"
        self.container_id = None
        self._context_manager = _DummyContextManager()

    async def warm_default_agent(self) -> None:
        return None

    async def warm_agent(self, agent_key: str) -> None:
        return None

    def context_manager(self):
        return self._context_manager

    def update_conversation_metadata(self, context_id: str, **updates):
        return None

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
    assert bootstrap_response.json()["default_agent"] == "test_agent"
