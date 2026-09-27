"""Voice control: the decision model names a chat command and picks its argument."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.context import ContextManager
from core.decisions import DecisionsModel
from web_chat.voice_turns import COMMANDS, endings, register_turn_routes


def voice_space(config, source=None, **extra):
    """A space whose deployment's voice settings are *config*; *source* resolves model keys."""
    source = source if source is not None else object()
    # Voice is off unless configured: these tests turn it on unless they say otherwise.
    config = {**config, "voice": {"enabled": True, **(config.get("voice") or {})}}
    deployment = SimpleNamespace(voice_config_dict=lambda: config, voice_source=lambda: ("voice.yaml", source))
    return SimpleNamespace(deployment=deployment, **extra)


def turn_app(space):
    """An app with the decide route, serving *space* to every request."""
    app = FastAPI()
    register_turn_routes(app, lambda: space)
    return app


def decide(space, answer, payload):
    """POST a phrase; *answer(question_key, request_body)* scripts the decision model."""
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        (key,) = body["questions"]
        return httpx.Response(200, json={"answers": {key: {"choice": answer(key, body)}}})

    model = DecisionsModel("https://example.test/alpha/decisions", "test", "typesafe/jev-1.13")
    app = turn_app(space)
    with patch.object(DecisionsModel, "from_config", return_value=model), patch.object(
        DecisionsModel, "http_client", lambda *a, **k: httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ):
        response = TestClient(app).post("/api/voice/decide", json=payload)
    return response, requests


SPACE = voice_space({"voice": {"decision_model": "jev"}})


@pytest.mark.parametrize("command", ["wait", "ignore", "message", "stop", "stop_now", "continue", "compact"])
def test_the_model_names_one_command_with_the_chat_state(command):
    response, requests = decide(
        SPACE,
        lambda key, body: command,
        {"text": "Я хотел бы…", "context_id": "c", "agent_busy": True, "state": {"resumable": True, "queue_count": 2}},
    )
    assert response.status_code == 200
    assert response.json() == {
        "action": command, "argument": None, "needs_confirmation": False, "awaiting_text": False, "model": "jev",
    }
    body = requests[0]
    assert set(body["questions"]["command"]["criteria"]) == set(COMMANDS)
    assert body["state"]["agent_busy"] is True
    assert body["state"]["chat"]["resumable"] is True and body["state"]["chat"]["queue_count"] == 2


def test_an_edit_takes_its_new_text_from_the_phrase_itself():
    text = "исправь последнее сообщение на открой блокнот и напиши привет"
    candidates = endings(text)

    def answer(key, body):
        if key == "command":
            return "edit_last"
        criteria = body["questions"]["span"]["criteria"]
        assert {k: v for k, v in criteria.items() if k != "none"} == candidates and "none" in criteria
        return next(k for k, v in candidates.items() if v == "открой блокнот и напиши привет")

    response, _ = decide(SPACE, answer, {"text": text, "context_id": "c"})
    assert response.json()["argument"] == {"text": "открой блокнот и напиши привет"}


def test_a_chat_is_opened_by_its_title_from_the_real_list():
    manager = ContextManager()
    sales = manager.start_new_context()
    manager.append_message_to(sales, "user", "sales report")
    manager.update_context_metadata(sales, {"title": "Отчёт по продажам"})
    other = manager.start_new_context()
    manager.append_message_to(other, "user", "fix the build")
    space = voice_space({"voice": {"decision_model": "jev"}}, context_manager=lambda: manager)

    def answer(key, body):
        if key == "command":
            return "open_chat"
        return next(k for k, v in body["questions"]["chat"]["criteria"].items() if v == "Отчёт по продажам")

    response, _ = decide(space, answer, {"text": "открой чат про продажи", "context_id": other})
    assert response.json()["argument"] == {"context_id": sales, "title": "Отчёт по продажам"}


def test_an_agent_is_chosen_from_the_configured_ones_or_automatic():
    registry = SimpleNamespace(
        systems=lambda: [SimpleNamespace(key="engineering", name="Engineering")],
        agents=lambda key: {"engineer": SimpleNamespace(name="Engineer", description="Writes code")},
    )
    space = voice_space({"voice": {"decision_model": "jev"}}, registry=registry)
    response, _ = decide(
        space, lambda key, body: "choose_agent" if key == "command" else "a1",
        {"text": "переключись на инженера", "context_id": "c"},
    )
    argument = response.json()["argument"]
    assert (argument["system_key"], argument["agent_key"]) == ("engineering", "engineer")
    response, _ = decide(
        space, lambda key, body: "choose_agent" if key == "command" else "a0",
        {"text": "выбирай сам", "context_id": "c"},
    )
    assert response.json()["argument"]["agent_key"] is None


def test_deleting_a_chat_asks_for_a_spoken_yes():
    response, _ = decide(SPACE, lambda key, body: "delete_chat", {"text": "удали этот чат", "context_id": "c"})
    assert response.json()["needs_confirmation"] is True
    response, requests = decide(
        SPACE, lambda key, body: "confirm",
        {"text": "да", "context_id": "c", "state": {"confirmation": "delete_chat"}},
    )
    assert response.json()["action"] == "confirm"
    assert requests[0]["state"]["chat"]["confirmation"] == "delete_chat"


def test_endings_are_every_word_boundary_and_bounded():
    assert endings("a b c") == {"e0": "a b c", "e1": "b c", "e2": "c"}
    assert len(endings(" ".join(["w"] * 100))) == 40


@pytest.mark.parametrize("outcome", ["error", "invalid"])
def test_decision_failure_never_falls_back_to_dispatch(outcome):
    def handle(request):
        if outcome == "error":
            raise httpx.ReadTimeout("late")
        return httpx.Response(200, json={"answers": {"command": {"choice": "execute_everything"}}})

    model = DecisionsModel("https://example.test", "test", "jev")
    app = turn_app(SPACE)
    with patch.object(DecisionsModel, "from_config", return_value=model), patch.object(
        DecisionsModel, "http_client", lambda *a, **k: httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ):
        response = TestClient(app).post("/api/voice/decide", json={"text": "Сделай", "context_id": "test"})
    assert response.status_code == 503
    assert "action" not in response.json()


def test_the_routing_model_controls_the_conversation_when_voice_names_none():
    """Without voice.decision_model, the catalog's routing model is used - from the catalog."""
    source = object()
    seen = []

    def from_config(config, key):
        seen.append((config, key))
        raise RuntimeError("stop here")

    app = turn_app(voice_space({"routing": {"model": "router"}}, source))
    with patch.object(DecisionsModel, "from_config", side_effect=from_config):
        response = TestClient(app).post("/api/voice/decide", json={"text": "Привет", "context_id": "test"})
    assert seen == [(source, "router")]
    assert response.status_code == 503


def test_a_missing_decision_model_is_reported():
    app = turn_app(voice_space({}))
    response = TestClient(app).post("/api/voice/decide", json={"text": "Привет", "context_id": "test"})
    assert response.status_code == 503
    assert "voice.decision_model" in response.json()["detail"]


def test_a_command_without_its_text_asks_for_it_instead_of_sending_the_rest():
    """ "Измени сообщение" said alone must not send "сообщение" as the new text."""
    def answer(key, body):
        if key == "command":
            return "edit_last"
        assert "none" in body["questions"]["span"]["criteria"]
        return "none"

    response, _ = decide(SPACE, answer, {"text": "Измени сообщение.", "context_id": "c"})
    assert response.json()["action"] == "edit_last"
    assert response.json()["awaiting_text"] is True
    assert response.json()["argument"] is None


@pytest.mark.parametrize("reply, action, argument", [
    ("text", "edit_last", {"text": "открой блокнот"}),
    ("cancel", "decline", None),
    ("wait", "wait", None),
])
def test_the_next_phrase_is_the_awaited_text_a_cancel_or_unfinished(reply, action, argument):
    response, requests = decide(
        SPACE, lambda key, body: reply,
        {"text": " открой блокнот ", "context_id": "c", "state": {"awaiting_text": "edit_last"}},
    )
    assert (response.json()["action"], response.json()["argument"]) == (action, argument)
    assert list(requests[0]["questions"]) == ["reply"]  # not a new command


def test_voice_control_is_off_unless_the_config_turns_it_on():
    space = voice_space({})
    space.deployment.voice_config_dict = lambda: {"voice": {"decision_model": "jev"}}

    response = TestClient(turn_app(space)).post("/api/voice/decide", json={"text": "Привет", "context_id": "c"})

    assert response.status_code == 403
