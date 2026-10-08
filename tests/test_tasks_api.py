"""The tracker page's API (web_chat/tasks_api.py): its routes over bd, and the
scope each route acts in - the asking user's own workspace, never another's.

bd itself is stubbed: these are the routes' own behaviour - the commands they
shape, the answers they make of bd's, and the auth/scope they hold.
"""

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import web_chat.tasks_api as tasks_api
from web_chat.tasks_api import register_task_routes


def issue(task_id, *, status="open", **extra):
    return {"id": task_id, "title": f"Task {task_id}", "status": status, **extra}


class Bd:
    """A stand-in for ``run_bd``: canned answers, and what was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def __call__(self, args, cwd=None):
        self.calls.append((list(args), cwd))
        return self.answer(args, cwd) if callable(self.answer) else self.answer


def make_app(bd, *, require_user=False):
    """An app with the tracker routes over *bd*, and a scoped current_space."""
    tasks_api._bd_problem = lambda: None
    tasks_api.run_bd = bd
    app = FastAPI()
    api = APIRouter()

    def current_space(request: Request):
        user = request.headers.get("x-user")
        if require_user and user is None:
            raise HTTPException(status_code=401, detail="Sign in")
        return SimpleNamespace(workspace_path=f"/tmp/{user or 'anon'}")

    register_task_routes(api, current_space)
    app.include_router(api)
    return app


def test_list_maps_bd_issues_into_the_pages_tasks():
    bd = Bd(lambda args, cwd: {
        "success": True,
        "data": [
            issue("p-1", priority=0, issue_type="feature"),
            issue("p-2", status="closed", close_reason="done"),
        ],
    })
    client = TestClient(make_app(bd))
    body = client.get("/api/tasks").json()

    assert body["available"] is True and body["total"] == 2
    first, second = body["tasks"]
    assert (first["id"], first["state"], first["priority"], first["type"]) == ("p-1", "ready", 0, "feature")
    assert (second["state"], second["close_reason"]) == ("closed", "done")
    assert body["counts"] == {"ready": 1, "waiting": 0, "in_progress": 0, "blocked": 0, "closed": 1}
    # The read ran bd in the asking user's workspace, all tasks, newest first.
    args, cwd = bd.calls[0]
    assert args[0] == "list" and "--all" in args and "--json" in args and cwd == "/tmp/anon"


def test_a_workspace_without_a_tracker_still_answers_the_page():
    bd = Bd({"success": False, "output": "no beads database", "error": ""})
    client = TestClient(make_app(bd))
    body = client.get("/api/tasks").json()
    assert body == {"available": False, "reason": "no beads database", "tasks": [], "total": 0}


def test_create_shapes_the_command_from_the_body():
    bd = Bd({"success": True, "data": [issue("p-9", status="open")]})
    client = TestClient(make_app(bd))
    response = client.post("/api/tasks", json={
        "title": "Fix the build", "priority": 0, "type": "bug",
        "description": "make it green", "depends_on": ["p-1", "bad id!"],
    })

    assert response.status_code == 201 and response.json()["id"] == "p-9"
    args, cwd = bd.calls[0]
    assert args[0] == "create" and args[-2:] == ["--", "Fix the build"]
    assert "--priority=0" in args and "--type=bug" in args and "--description=make it green" in args
    # Only real ids become dependencies; an invalid one never reaches bd.
    assert "--deps=p-1" in args


def test_a_title_or_value_that_looks_like_a_flag_stays_a_value():
    bd = Bd({"success": True, "data": [issue("p-9", status="open")]})
    client = TestClient(make_app(bd))
    client.post("/api/tasks", json={"title": "--db=/elsewhere", "description": "-x"})
    args, _ = bd.calls[0]
    # The title follows "--"; every other value rides inside its own flag.
    assert args[-2:] == ["--", "--db=/elsewhere"] and "--description=-x" in args
    assert "--db=/elsewhere" not in args[:-1]


def test_a_refused_write_is_400_with_bds_reason():
    bd = Bd({"success": False, "output": "", "error": "cycle detected"})
    client = TestClient(make_app(bd))
    response = client.post("/api/tasks", json={"title": "X"})
    assert response.status_code == 400 and "cycle detected" in response.json()["detail"]


def test_show_answers_404_when_bd_does_not_find_the_task():
    bd = Bd({"success": False, "output": "issue not found", "error": ""})
    client = TestClient(make_app(bd))
    assert client.get("/api/tasks/p-1").status_code == 404


def test_update_with_nothing_to_change_is_400():
    bd = Bd({"success": True, "data": []})
    client = TestClient(make_app(bd))
    assert client.patch("/api/tasks/p-1", json={}).status_code == 400
    assert bd.calls == []  # nothing was asked of bd for an empty patch


def test_update_patches_and_answers_with_the_fresh_task():
    def answer(args, cwd):
        if args[0] == "show":
            return {"success": True, "data": [issue("p-1", notes="now noted")]}
        return {"success": True, "data": []}

    client = TestClient(make_app(Bd(answer)))
    body = client.patch("/api/tasks/p-1", json={"notes": "now noted"}).json()
    assert body["id"] == "p-1" and body["notes"] == "now noted"


def test_close_carries_the_reason_and_reports_it():
    bd = Bd({"success": True, "output": "closed"})
    client = TestClient(make_app(bd))
    body = client.post("/api/tasks/p-1/close", json={"reason": "listed"}).json()
    assert body == {"ok": True, "id": "p-1", "close_reason": "listed"}
    assert bd.calls[0][0] == ["close", "p-1", "--reason=listed"]


def test_a_dependency_add_names_the_child_and_the_parent():
    bd = Bd({"success": True})
    client = TestClient(make_app(bd))
    body = client.post("/api/tasks/p-2/deps", json={"action": "add", "parent_id": "p-1"}).json()
    assert body["ok"] is True
    assert bd.calls[0][0] == ["dep", "add", "p-2", "p-1"]


def test_init_starts_a_tracker_and_tolerates_one_that_already_is():
    # A first init succeeds; a second reports it is already there and is not refused.
    answers = iter([
        {"success": True, "output": "initialized"},
        {"success": False, "output": "already a beads database", "error": ""},
    ])
    client = TestClient(make_app(Bd(lambda args, cwd: next(answers))))
    assert client.post("/api/tasks/init").status_code == 200
    assert client.post("/api/tasks/init").status_code == 200


def test_a_bad_task_id_never_reaches_bd():
    bd = Bd({"success": True, "data": []})
    client = TestClient(make_app(bd))
    assert client.get("/api/tasks/%20..%2f").status_code in (400, 404)
    assert client.post("/api/tasks/not valid/close", json={"reason": "x"}).status_code == 400
    # An id that would read as a flag never reaches bd either.
    assert client.post("/api/tasks/--db/close", json={"reason": "x"}).status_code == 400
    assert bd.calls == []


def test_a_child_task_id_is_a_task_id():
    bd = Bd({"success": True, "output": "closed"})
    client = TestClient(make_app(bd))
    assert client.post("/api/tasks/p-1.2/close", json={"reason": "x"}).status_code == 200


def test_every_route_needs_a_signed_in_user():
    bd = Bd({"success": True, "data": []})
    client = TestClient(make_app(bd, require_user=True))
    assert client.get("/api/tasks").status_code == 401
    assert client.post("/api/tasks", json={"title": "X"}).status_code == 401
    assert client.post("/api/tasks/init").status_code == 401
    assert bd.calls == []


def test_each_user_asks_bd_in_their_own_workspace():
    bd = Bd({"success": True, "data": []})
    client = TestClient(make_app(bd, require_user=True))
    client.get("/api/tasks", headers={"x-user": "alice"})
    client.get("/api/tasks", headers={"x-user": "bob"})
    # Same route, two workspaces: one user never reads another's tracker.
    assert [cwd for _, cwd in bd.calls] == ["/tmp/alice", "/tmp/bob"]


def test_a_host_without_bd_answers_that_the_tracker_is_unavailable():
    bd = Bd({"success": True, "data": []})
    app = make_app(bd)
    tasks_api._bd_problem = lambda: "bd is not installed"
    response = TestClient(app).get("/api/tasks")
    assert response.status_code == 503 and "unavailable" in response.json()["detail"]
    assert bd.calls == []  # bd was never asked to run
