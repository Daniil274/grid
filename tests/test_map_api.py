"""The visual GRID's map API (web_chat/map_api.py) and its page route.

The tracker is stubbed where a test is about the route's own behaviour (the
items it makes of bd's issues, the bounds it holds, what it does when bd is
gone); the project's space is a real directory, so the results are listed by
the same code the files page uses. Two spaces stand behind one app for the
isolation test: a project of one is not found by the other.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import web_chat.map_api as map_api
from web_chat.map_api import register_map_routes
from web_chat.projects import ProjectStore
from web_chat.turns import TurnBoard


# -- the space -------------------------------------------------------------------


class FakeManager:
    """What the map asks of a context manager: the chats and their metadata."""

    def __init__(self, views):
        self._views = views

    def conversation_views(self):
        return list(self._views)


class FakeSpace:
    """A space with a real project store and a real folder of results."""

    def __init__(self, root: Path, *, project_spaces=True):
        root.mkdir(parents=True, exist_ok=True)
        self.project_store = ProjectStore(root / "projects.json")
        self.project_exists = self.project_store.exists
        self.chat_spaces = project_spaces
        self.layout = SimpleNamespace(project_spaces=root / "projects")
        self.turns = TurnBoard()
        self.workspace_path = str(root / "workspace")
        self._views = []
        self.waiting = {}

    def context_manager(self):
        return FakeManager(self._views)

    def awaiting_approval(self):
        return self.waiting

    def space_dir(self, project_id: str) -> Path:
        directory = self.layout.project_spaces / project_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory


def view(chat_id: str, project_id, *, title=None, updated_at="2026-01-01T00:00:00Z", messages=None):
    return {
        "id": chat_id,
        "metadata": {"project_id": project_id, "title": title or chat_id},
        "updated_at": updated_at,
        "messages": messages or [],
    }


class Bd:
    """A stand-in for ``web_chat.tasks_api._bd_for``: canned answers, what was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def __call__(self, space, args, *, project=None, task_id=None):
        self.calls.append((list(args), project))
        return self.answer(args, project) if callable(self.answer) else self.answer


def issue(task_id, *, created_at="2026-01-01T00:00:00Z", status="open", **extra):
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "status": status,
        "description": "SECRET description",
        "notes": "SECRET notes",
        "acceptance_criteria": "",
        "priority": 2,
        "issue_type": "task",
        "assignee": None,
        "created_at": created_at,
        "updated_at": created_at,
        "dependencies": [],
        **extra,
    }


def make_app(spaces):
    """An app whose space is chosen by the ``x-user`` header (one per user)."""
    app = FastAPI()
    api = APIRouter()

    def current_space(request: Request):
        user = request.headers.get("x-user", "u1")
        if user not in spaces:
            raise HTTPException(status_code=401, detail="Sign in")
        return spaces[user]

    register_map_routes(api, current_space)
    app.include_router(api)
    return app


@pytest.fixture
def bd(monkeypatch):
    stub = Bd({"success": True, "data": []})
    monkeypatch.setattr(map_api, "_bd_for", stub)
    return stub


@pytest.fixture
def space(tmp_path):
    return FakeSpace(tmp_path / "u1")


@pytest.fixture
def client(space, bd):
    return TestClient(make_app({"u1": space}))


def project(space, title="Grid", parent_id=None, description=""):
    return space.project_store.create(title, parent_id=parent_id, description=description)


# -- the map ---------------------------------------------------------------------


def test_the_map_has_the_page_contract(client, space, bd):
    root = project(space, "GRID", description="the app")
    child = project(space, "Chats", parent_id=root.id)
    archived = project(space, "Archived", parent_id=root.id)
    space.project_store.update(archived.id, archived=True)
    space._views = [
        view("chat-1", root.id, title="First chat", updated_at="2026-02-01T00:00:00Z"),
        view("chat-2", root.id, title="Second chat", updated_at="2026-01-01T00:00:00Z"),
        view("chat-other", "prj-other", title="Elsewhere"),
    ]
    bd.answer = {"success": True, "data": [issue("prj-1-a", status="in_progress", dependencies=[
        {"depends_on_id": "prj-1-b", "type": "blocks", "title": "Other"},
    ])]}
    (space.space_dir(root.id) / "notes.md").write_text("hi", encoding="utf-8")

    body = client.get(f"/api/projects/{root.id}/map").json()

    assert body["project"] == {
        "id": root.id,
        "title": "GRID",
        "parent_id": None,
        "description": "the app",
        "updated_at": body["project"]["updated_at"],
    }
    assert body["path"] == []
    assert body["children"] == [{"id": child.id, "title": "Chats"}]

    tasks = body["tasks"]
    assert tasks["available"] is True and tasks["reason"] is None
    assert tasks["total"] == 1 and tasks["truncated"] is False
    task = tasks["items"][0]
    assert set(task) == set(map_api.TASK_FIELDS)
    assert (task["id"], task["state"], task["depends_on"]) == ("prj-1-a", "in_progress", ["prj-1-b"])
    # The map shows where a task is, never what it says.
    assert "SECRET" not in json.dumps(body)
    assert tasks["counts"]["in_progress"] == 1

    # Newest chat first; the other project's chat never appears.
    assert [item["chat_id"] for item in body["processes"]["items"]] == ["chat-1", "chat-2"]
    assert body["processes"]["total"] == 2

    results = body["results"]
    assert results["available"] is True
    assert [item["name"] for item in results["items"]] == ["notes.md"]
    assert results["items"][0]["kind"] == "file" and results["items"][0]["size"] == 2

    # bd ran in the project's tracker, not the user's.
    assert bd.calls[0][1] == root.id
    assert body["files_url"] == f"/files?context=chat-1&path=project"


def test_the_path_is_the_ancestors_from_the_root(client, space):
    top = project(space, "Top")
    middle = project(space, "Middle", parent_id=top.id)
    leaf = project(space, "Leaf", parent_id=middle.id)

    body = client.get(f"/api/projects/{leaf.id}/map").json()

    assert body["path"] == [{"id": top.id, "title": "Top"}, {"id": middle.id, "title": "Middle"}]
    assert body["children"] == []


def test_an_unknown_project_is_not_found(client):
    response = client.get("/api/projects/prj-00000000-nope/map")
    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}


def test_another_users_project_is_not_found(tmp_path, bd, space):
    theirs = FakeSpace(tmp_path / "u2")
    project(theirs, "Theirs")
    client = TestClient(make_app({"u1": space, "u2": theirs}))

    mine = project(space, "Mine")
    assert client.get(f"/api/projects/{mine.id}/map", headers={"x-user": "u1"}).status_code == 200
    # The same id under the other user is another project's store: not found.
    foreign = client.get(f"/api/projects/{mine.id}/map", headers={"x-user": "u2"})
    assert foreign.status_code == 404


# -- bounds ----------------------------------------------------------------------


def test_tasks_are_bounded_and_come_oldest_first(client, space, bd):
    root = project(space)
    bd.answer = {
        "success": True,
        "data": [issue(f"prj-1-t{index:03d}", created_at=f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z")
                 for index in range(map_api.TASKS_LIMIT + 5)],
    }

    body = client.get(f"/api/projects/{root.id}/map").json()

    tasks = body["tasks"]
    assert tasks["total"] == map_api.TASKS_LIMIT + 5
    assert tasks["truncated"] is True
    assert len(tasks["items"]) == map_api.TASKS_LIMIT
    assert tasks["items"][0]["id"] == "prj-1-t000"
    created = [task["created_at"] for task in tasks["items"]]
    assert created == sorted(created)


def test_processes_are_bounded_and_running_chats_come_first(client, space, bd):
    root = project(space)
    space._views = [
        view(f"chat-{index:02d}", root.id, updated_at=f"2026-01-{index + 1:02d}T00:00:00Z")
        for index in range(map_api.PROCESSES_LIMIT + 3)
    ]

    body = client.get(f"/api/projects/{root.id}/map").json()

    processes = body["processes"]
    assert processes["total"] == map_api.PROCESSES_LIMIT + 3
    assert processes["truncated"] is True
    assert len(processes["items"]) == map_api.PROCESSES_LIMIT
    # Without a running turn the newest chat is first.
    assert processes["items"][0]["chat_id"] == f"chat-{map_api.PROCESSES_LIMIT + 2:02d}"
    # A running chat, however old, comes above the idle ones.
    space.turns.register("chat-00", object(), _Running())
    body = client.get(f"/api/projects/{root.id}/map").json()
    assert body["processes"]["items"][0]["chat_id"] == "chat-00"
    assert body["processes"]["items"][0]["active"] is True


class _Running:
    """A task that never finishes: the chat's turn is on."""

    def done(self):
        return False


def test_results_are_bounded_hidden_entries_excluded(client, space, bd):
    root = project(space)
    directory = space.space_dir(root.id)
    for index in range(map_api.RESULTS_LIMIT + 1):
        (directory / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")

    results = client.get(f"/api/projects/{root.id}/map").json()["results"]

    assert results["available"] is True
    assert results["total"] == map_api.RESULTS_LIMIT + 1
    assert results["truncated"] is True
    assert len(results["items"]) == map_api.RESULTS_LIMIT


def test_results_are_the_projects_space_without_tool_state(client, space, bd):
    root = project(space)
    directory = space.space_dir(root.id)
    for name in (".beads", ".git", ".codegraph", ".grid"):
        (directory / name).mkdir()
    (directory / "docs").mkdir()
    (directory / "report.md").write_text("done", encoding="utf-8")

    body = client.get(f"/api/projects/{root.id}/map").json()
    results = body["results"]

    assert [item["name"] for item in results["items"]] == ["docs", "report.md"]
    assert [item["kind"] for item in results["items"]] == ["dir", "file"]
    assert results["items"][0]["size"] is None
    # No absolute path anywhere in the answer.
    assert str(directory) not in json.dumps(body)
    assert all(not str(item["path"]).startswith("/") for item in results["items"])


def test_a_project_without_a_space_answers_no_results(tmp_path, bd):
    space = FakeSpace(tmp_path / "u1", project_spaces=False)
    root = project(space)
    client = TestClient(make_app({"u1": space}))

    body = client.get(f"/api/projects/{root.id}/map").json()

    assert body["results"]["available"] is False
    assert body["results"]["reason"] and body["results"]["items"] == []
    assert body["files_url"] is None


# -- the tracker's own failures --------------------------------------------------


def test_a_workspace_without_a_tracker_still_answers_the_map(client, space, bd):
    root = project(space)
    bd.answer = {"success": False, "output": "no beads database", "error": ""}

    response = client.get(f"/api/projects/{root.id}/map")

    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert tasks == {
        "available": False,
        "reason": "no beads database",
        "items": [],
        "total": 0,
        "truncated": False,
        "counts": {"ready": 0, "waiting": 0, "in_progress": 0, "blocked": 0, "closed": 0},
    }


def test_a_missing_bd_degrades_instead_of_failing(client, space, bd):
    root = project(space)

    async def missing(space, args, *, project=None, task_id=None):
        raise HTTPException(status_code=503, detail="The tracker is unavailable: bd not found")

    bd.answer = None
    map_api._bd_for = missing  # replaced for this app's module, restored by pytest
    try:
        response = client.get(f"/api/projects/{root.id}/map")
    finally:
        map_api._bd_for = bd

    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert tasks["available"] is False
    assert "bd not found" in tasks["reason"]


def test_a_tracker_that_does_not_answer_in_time(monkeypatch, client, space):
    root = project(space)

    async def slow(space, args, *, project=None, task_id=None):
        await asyncio.sleep(5)
        return {"success": True, "data": []}

    monkeypatch.setattr(map_api, "_bd_for", slow)
    monkeypatch.setattr(map_api, "BD_TIMEOUT_SECONDS", 0.01)

    tasks = client.get(f"/api/projects/{root.id}/map").json()["tasks"]

    assert tasks["available"] is False
    assert "did not answer in time" in tasks["reason"]


# -- revalidation ----------------------------------------------------------------


def test_the_etag_revalidates_the_map(client, space):
    root = project(space)
    first = client.get(f"/api/projects/{root.id}/map")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-cache"

    again = client.get(f"/api/projects/{root.id}/map")
    assert again.headers["etag"] == etag

    cached = client.get(f"/api/projects/{root.id}/map", headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""
    # A weak tag matches too.
    weak = client.get(f"/api/projects/{root.id}/map", headers={"If-None-Match": f"W/{etag}"})
    assert weak.status_code == 304
    # Another project's map is another body.
    other = project(space, "Other")
    assert client.get(f"/api/projects/{other.id}/map", headers={"If-None-Match": etag}).status_code == 200


# -- the page --------------------------------------------------------------------


def test_the_grid_page_serves_its_shell(tmp_path, monkeypatch, space):
    from web_chat import server as server_module
    from tests.test_web_ui_routes import _server

    shell = tmp_path / "grid.html"
    shell.write_text("<!doctype html><title>GRID</title>", encoding="utf-8")
    monkeypatch.setattr(server_module, "GRID_HTML", shell)

    page = TestClient(_server().app).get("/grid")

    assert page.status_code == 200
    assert "GRID" in page.text
    assert page.headers["cache-control"] == "no-store"


def test_the_grid_page_says_so_when_its_file_is_missing(tmp_path, monkeypatch, space):
    from web_chat import server as server_module
    from tests.test_web_ui_routes import _server

    monkeypatch.setattr(server_module, "GRID_HTML", tmp_path / "not-written-yet.html")

    page = TestClient(_server().app).get("/grid")

    assert page.status_code == 503
    assert "not-written-yet.html" in page.text
