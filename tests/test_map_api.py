"""The visual GRID's map API (web_chat/map_api.py) and its page route.

The tracker is stubbed where a test is about the route's own behaviour (the
items it makes of bd's issues, the bounds it holds, what it does when bd is
gone); the project's space is a real directory, so the files are listed by
the same code the files page uses. Two spaces stand behind one app for the
isolation test: a project of one is not found by the other.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from web_chat import map_api
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
    """A space with a real project store and a real folder of files."""

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
    """A stand-in for bd execution: canned answers, what was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def __call__(self, directory, args):
        self.calls.append((list(args), Path(directory)))
        return self.answer(args, Path(directory)) if callable(self.answer) else self.answer


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
    monkeypatch.setattr(map_api, "_bd_in", stub)
    return stub


@pytest.fixture
def space(tmp_path):
    return FakeSpace(tmp_path / "u1")


@pytest.fixture
def client(space, bd):
    return TestClient(make_app({"u1": space}))


def project(space, title="Grid", parent_id=None, description=""):
    result = space.project_store.create(title, parent_id=parent_id, description=description)
    tracker = space.layout.project_spaces / result.id / ".beads"
    tracker.mkdir(parents=True, exist_ok=True)
    (tracker / "beads.db").touch()  # local database witness; bd itself is stubbed
    (tracker / "config.yaml").write_text("# Local config blocks ancestor lookup\n")
    return result


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
    bd.answer = {"success": True, "data": [issue(f"{root.id}-a", status="in_progress", dependencies=[
        {"depends_on_id": f"{root.id}-b", "type": "blocks", "title": "Other"},
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
    assert (task["id"], task["state"], task["depends_on"]) == (f"{root.id}-a", "in_progress", [f"{root.id}-b"])
    # The map shows where a task is, never what it says.
    assert "SECRET" not in json.dumps(body)
    assert tasks["counts"]["in_progress"] == 1

    # Newest chat first; the other project's chat never appears.
    assert [item["chat_id"] for item in body["processes"]["items"]] == ["chat-1", "chat-2"]
    assert body["processes"]["total"] == 2

    files = body["files"]
    assert files["available"] is True
    assert [item["name"] for item in files["items"]] == ["notes.md"]
    assert files["items"][0]["kind"] == "file" and files["items"][0]["size"] == 2

    # bd ran in the project's tracker, not the user's.
    assert bd.calls[0][1] == space.layout.project_spaces / root.id
    assert bd.calls[0][0][0] == "--readonly"
    assert body["files_url"] == "/files?context=chat-1&path=project"


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
        "data": [issue(f"{root.id}-t{index:03d}", created_at=f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z")
                 for index in range(map_api.TASKS_LIMIT + 5)],
    }

    body = client.get(f"/api/projects/{root.id}/map").json()

    tasks = body["tasks"]
    assert tasks["total"] == map_api.TASKS_LIMIT + 5
    assert tasks["truncated"] is True
    assert len(tasks["items"]) == map_api.TASKS_LIMIT
    assert tasks["items"][0]["id"] == f"{root.id}-t000"
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


def test_files_are_bounded_hidden_entries_excluded(client, space, bd):
    root = project(space)
    directory = space.space_dir(root.id)
    for index in range(map_api.FILES_LIMIT + 1):
        (directory / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")

    files = client.get(f"/api/projects/{root.id}/map").json()["files"]

    assert files["available"] is True
    assert files["total"] == map_api.FILES_LIMIT + 1
    assert files["truncated"] is True
    assert len(files["items"]) == map_api.FILES_LIMIT


def test_files_are_the_projects_space_without_tool_state(client, space, bd):
    root = project(space)
    directory = space.space_dir(root.id)
    for name in (".beads", ".git", ".codegraph", ".grid"):
        (directory / name).mkdir(exist_ok=True)
    (directory / "docs").mkdir()
    (directory / "report.md").write_text("done", encoding="utf-8")

    body = client.get(f"/api/projects/{root.id}/map").json()
    files = body["files"]

    assert [item["name"] for item in files["items"]] == ["docs", "report.md"]
    assert [item["kind"] for item in files["items"]] == ["dir", "file"]
    assert files["items"][0]["size"] is None
    # No absolute path anywhere in the answer.
    assert str(directory) not in json.dumps(body)
    assert all(not str(item["path"]).startswith("/") for item in files["items"])


def test_a_project_without_a_space_answers_no_files(tmp_path, bd):
    space = FakeSpace(tmp_path / "u1", project_spaces=False)
    root = project(space)
    client = TestClient(make_app({"u1": space}))

    body = client.get(f"/api/projects/{root.id}/map").json()

    assert body["files"]["available"] is False
    assert body["files"]["reason"] and body["files"]["items"] == []
    assert body["files_url"] is None


@pytest.mark.parametrize("kind", [
    "missing-space", "missing", "empty", "marker", "beads-link", "space-link",
    "db-link", "redirect", "env", "config", "metadata", "remote", "dolt-link",
    "missing-config", "local-config", "legacy-config", "wal-link",
])
def test_unsafe_or_absent_tracker_never_invokes_bd(space, client, bd, tmp_path, kind):
    root = space.project_store.create("No safe local tracker")
    directory = space.layout.project_spaces / root.id
    target = tmp_path / "foreign"
    target.mkdir()
    (target / "beads.db").touch()
    if kind != "missing-space":
        directory.mkdir(parents=True)
    beads = directory / ".beads"
    if kind == "space-link":
        directory.rmdir()
        directory.symlink_to(target, target_is_directory=True)
    elif kind == "beads-link":
        beads.symlink_to(target, target_is_directory=True)
    elif kind not in {"missing", "missing-space"}:
        beads.mkdir()
        if kind not in {"empty", "marker"}:
            (beads / "config.yaml").write_text("# Local config\n")
        if kind == "marker":
            (beads / "marker").touch()
        elif kind == "db-link":
            (beads / "beads.db").symlink_to(target / "beads.db")
        elif kind not in {"empty"}:
            (beads / "beads.db").touch()
            if kind == "redirect":
                (beads / "redirect").write_text(str(target))
            elif kind == "env":
                (beads / ".env").write_text(f"BEADS_DIR={target}\n")
            elif kind == "config":
                (beads / "config.yaml").write_text(f"db: {target}\n")
            elif kind == "metadata":
                (beads / "metadata.json").write_text("not json")
            elif kind in {"remote", "dolt-link"}:
                (beads / "metadata.json").write_text(json.dumps({
                    "backend": "dolt", "database": "dolt", "dolt_database": root.id,
                    "dolt_mode": "server" if kind == "remote" else "embedded",
                }))
                if kind == "dolt-link":
                    (beads / "embeddeddolt").symlink_to(target, target_is_directory=True)
            elif kind == "missing-config":
                (beads / "config.yaml").unlink()
            elif kind == "local-config":
                (beads / "config.local.yaml").write_text(f"db: {target}\n")
            elif kind == "legacy-config":
                (beads / "config.json").write_text(json.dumps({"database": str(target)}))
            elif kind == "wal-link":
                (beads / "beads.db-wal").symlink_to(target / "beads.db")

    body = client.get(f"/api/projects/{root.id}/map").json()
    assert body["tasks"] == map_api._no_tasks(map_api.NO_TRACKER)
    assert bd.calls == [], "Reject unsafe discovery BEFORE executing any bd command"
    if kind == "missing-space":
        assert not directory.exists(), "GET must not create a space"


def test_foreign_or_mixed_ids_are_not_exposed(client, space, bd):
    root = project(space)
    for issues in ([issue("foreign-secret")], [issue(f"{root.id}-own"), issue("foreign-secret")]):
        bd.answer = {"success": True, "data": issues}
        tasks = client.get(f"/api/projects/{root.id}/map").json()["tasks"]
        assert tasks == map_api._no_tasks(map_api.FOREIGN_TRACKER)


def test_tracker_lstat_failure_is_closed(space, monkeypatch):
    root = project(space)
    def denied(path):
        raise PermissionError("unreadable")
    monkeypatch.setattr(map_api.os, "lstat", denied)
    assert map_api._own_tracker(space.layout.project_spaces / root.id) is None


def test_tracker_process_pins_existing_db_and_strips_ambient_selectors(space, monkeypatch):
    root = project(space)
    directory = space.layout.project_spaces / root.id
    monkeypatch.setenv("BEADS_DIR", "/not-the-project")
    monkeypatch.setenv("BEADS_DOLT_SERVER_DATABASE", "foreign")
    monkeypatch.setenv("BD_DB", "/foreign")
    def run(command, **kwargs):
        database = directory / ".beads" / "beads.db"
        assert command[1:3] == ["--db", str(database)]
        assert kwargs["cwd"] == directory
        env = kwargs["env"]
        assert env["BEADS_DIR"] == str(database.parent)
        assert env["BEADS_DB"] == str(database)
        assert "BD_DB" not in env and "BEADS_DOLT_SERVER_DATABASE" not in env
        assert env["HOME"] == str(directory)
        assert env["XDG_CONFIG_HOME"] == str(directory / ".beads")
        return SimpleNamespace(returncode=0, stdout="[]")
    monkeypatch.setattr(map_api.subprocess, "run", run)
    assert map_api._read_tracker(directory, list(map_api.BD_LIST_ARGS)) == {"success": True, "data": []}


def test_real_bd_discovery_and_project_isolation(tmp_path, monkeypatch):
    """Exercise the installed CLI, exclusively within a synthetic git boundary."""
    parent = tmp_path / "synthetic-repo"
    parent.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "SYSTEMROOT", "WINDIR"}}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), BD_DISABLE_METRICS="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(["git", "init", str(parent)], env=env, check=True, capture_output=True)
    def run(directory, *args):
        # Pin config discovery during init too: no inherited ancestor config/remote.
        selected_env = env | {"BEADS_DIR": str(directory / ".beads")} if args[0] == "init" else env
        result = subprocess.run([map_api._find_bd(), *args], cwd=directory, env=selected_env, check=False,
                                capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stderr
        return result.stdout
    def init(directory, prefix):
        run(directory, "init", "--prefix", prefix, "--skip-agents", "--skip-hooks")
    init(parent, "synthetic")
    run(parent, "create", "SYNTHETIC PARENT ONLY", "--json")
    space = FakeSpace(parent / "space")
    root = space.project_store.create("Synthetic project")
    directory = space.space_dir(root.id)
    # Prove the real danger: a plain child cwd discovers the synthetic parent.
    discovered = json.loads(run(directory, "--readonly", "list", "--json"))
    assert discovered[0]["title"] == "SYNTHETIC PARENT ONLY"
    client = TestClient(make_app({"u1": space}))
    for marker in (None, "empty", "marker"):
        if marker:
            (directory / ".beads").mkdir(exist_ok=True)
        if marker == "marker":
            (directory / ".beads" / "marker").touch()
        assert client.get(f"/api/projects/{root.id}/map").json()["tasks"] == map_api._no_tasks(map_api.NO_TRACKER)
    # A project git boundary lets bd init create a new tracker, not reuse its parent.
    subprocess.run(["git", "init", str(directory)], env=env, check=True, capture_output=True)
    init(directory, root.id)
    created = json.loads(run(directory, "create", "SYNTHETIC PROJECT ONLY", "--json"))
    monkeypatch.setenv("BEADS_DIR", str(parent / ".beads"))
    monkeypatch.setenv("BEADS_DOLT_SERVER_DATABASE", "synthetic")
    tasks = client.get(f"/api/projects/{root.id}/map").json()["tasks"]
    assert tasks["available"] is True, tasks
    assert [item["id"] for item in tasks["items"]] == [created["id"]]
    assert tasks["items"][0]["title"] == "SYNTHETIC PROJECT ONLY"


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


def test_a_missing_bd_degrades_instead_of_failing(monkeypatch, client, space, bd):
    root = project(space)

    async def missing(directory, args):
        raise HTTPException(status_code=503, detail="The tracker is unavailable: bd not found")

    monkeypatch.setattr(map_api, "_bd_in", missing)
    response = client.get(f"/api/projects/{root.id}/map")

    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert tasks["available"] is False
    assert "bd not found" in tasks["reason"]


def test_a_tracker_that_does_not_answer_in_time(monkeypatch, client, space):
    root = project(space)

    async def slow(directory, args):
        await asyncio.sleep(5)
        return {"success": True, "data": []}

    monkeypatch.setattr(map_api, "_bd_in", slow)
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
    assert first.headers["cache-control"] == "private, no-cache"

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

    page = TestClient(_server().app).get("/map")

    assert page.status_code == 200
    assert "GRID" in page.text
    assert page.headers["cache-control"] == "no-store"


def test_the_grid_page_says_so_when_its_file_is_missing(tmp_path, monkeypatch, space):
    from web_chat import server as server_module
    from tests.test_web_ui_routes import _server

    monkeypatch.setattr(server_module, "GRID_HTML", tmp_path / "not-written-yet.html")

    page = TestClient(_server().app).get("/map")

    assert page.status_code == 503
    assert "not-written-yet.html" in page.text
