from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.decisions import DecisionsModel
from web_chat.voice_turns import register_turn_routes


@pytest.mark.parametrize("action,replacement", [("wait", False), ("respond", False), ("interrupt", True), ("ignore", False)])
def test_semantic_decision_uses_real_transport_schema(action, replacement):
    import json
    def handle(request):
        body = json.loads(request.content)
        assert body["model"] == "typesafe/jev-1.13"
        assert body["state"]["accumulated_user_speech"] == "Я хотел бы…"
        assert body["state"]["agent_busy"] is True
        assert set(body["questions"]["turn"]["criteria"]) == {"wait", "respond", "interrupt", "ignore"}
        return httpx.Response(200, json={"answers": {"turn": {"choice": action}, "after_interrupt": {"choice": "replace" if replacement else "stop"}}})
    model = DecisionsModel("https://example.test/alpha/decisions", "test", "typesafe/jev-1.13")
    runtime = SimpleNamespace(config=object(), config_dict=lambda: {"voice": {"decision_model": "jev"}})
    app = FastAPI()
    register_turn_routes(app, runtime)
    with patch.object(DecisionsModel, "from_config", return_value=model), patch.object(DecisionsModel, "http_client", lambda *a, **k: httpx.AsyncClient(transport=httpx.MockTransport(handle))):
        response = TestClient(app).post("/api/voice/decide", json={"text": "Я хотел бы…", "context_id": "test", "agent_busy": True})
    assert response.status_code == 200
    assert response.json()["action"] == action
    assert response.json()["replacement"] == replacement


@pytest.mark.parametrize("outcome", ["error", "invalid"])
def test_decision_failure_never_falls_back_to_dispatch(outcome):
    def handle(request):
        if outcome == "error":
            raise httpx.ReadTimeout("late")
        return httpx.Response(200, json={"answers": {"turn": {"choice": "execute_everything"}}})
    model = DecisionsModel("https://example.test", "test", "jev")
    app = FastAPI()
    register_turn_routes(app, SimpleNamespace(config=object(), config_dict=lambda: {"voice": {"decision_model": "jev"}}))
    with patch.object(DecisionsModel, "from_config", return_value=model), patch.object(DecisionsModel, "http_client", lambda *a, **k: httpx.AsyncClient(transport=httpx.MockTransport(handle))):
        response = TestClient(app).post("/api/voice/decide", json={"text": "Сделай", "context_id": "test"})
    assert response.status_code == 503
    assert "action" not in response.json()
