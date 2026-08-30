"""Tests for context_inspector FastAPI surface."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from context_inspector.server import create_app
from context_inspector.service import open_context_manager


def _seed(path: Path) -> str:
    context_id = "ctx-insp01"
    payload = {
        "active_context_id": context_id,
        "contexts": {
            context_id: {
                "conversation_history": [
                    {
                        "role": "user",
                        "content": "Inspect me",
                        "timestamp": "2026-02-01T10:00:00",
                        "metadata": {},
                    }
                ],
                "execution_history": [],
                "metadata": {
                    "user_id": "web-user",
                    "last_invocation": {"agent": "inspect_agent"},
                    "last_context_assembly": {
                        "context_id": context_id,
                        "history_strategy": "full",
                        "instruction_length": 15,
                        "sections": [
                            {
                                "key": "base_prompt",
                                "scope": "static",
                                "length": 15,
                                "content": "You are helpful",
                            }
                        ],
                        "metadata": {"agent_key": "inspect_agent"},
                    },
                },
                "created_at": "2026-02-01T10:00:00",
                "updated_at": "2026-02-01T10:00:00",
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return context_id


def test_open_context_manager_read_only(temp_dir):
    path = temp_dir / "context.json"
    _seed(path)
    manager = open_context_manager(path)
    assert manager.read_only is True
    assert len(manager.list_context_ids()) == 1


def test_api_endpoints(temp_dir):
    path = temp_dir / "context.json"
    context_id = _seed(path)
    app = create_app(context_path=path)
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "Context Inspector" in root.text

    status = client.get("/api/status")
    assert status.status_code == 200
    body = status.json()
    assert body["context_count"] == 1
    assert body["read_only"] is True

    listing = client.get("/api/contexts")
    assert listing.status_code == 200
    data = listing.json()
    assert data["total"] == 1
    assert data["contexts"][0]["id"] == context_id
    assert data["contexts"][0]["agent"] == "inspect_agent"

    filtered = client.get("/api/contexts", params={"q": "missing-agent"})
    assert filtered.json()["total"] == 0

    detail = client.get(f"/api/contexts/{context_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["id"] == context_id
    assert payload["assembly"]["section_count"] == 1
    assert payload["messages"][0]["preview"] == "Inspect me"

    full_detail = client.get(
        f"/api/contexts/{context_id}",
        params={"include_full_messages": True, "include_full_executions": True},
    )
    assert full_detail.json()["messages"][0]["content"] == "Inspect me"
    assert full_detail.json()["assembly"]["sections"][0]["content"] == "You are helpful"

    assembly = client.get(f"/api/contexts/{context_id}/assembly")
    assert assembly.status_code == 200
    assert assembly.json()["available"] is True

    messages = client.get(f"/api/contexts/{context_id}/messages")
    assert messages.status_code == 200
    assert messages.json()["count"] == 1

    executions = client.get(f"/api/contexts/{context_id}/executions")
    assert executions.status_code == 200
    assert executions.json()["count"] == 0

    missing = client.get("/api/contexts/ctx-nope")
    assert missing.status_code == 404

    reloaded = client.post("/api/reload")
    assert reloaded.status_code == 200
    assert reloaded.json()["ok"] is True
