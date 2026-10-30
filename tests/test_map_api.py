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
import logging
import os
import shutil
import stat
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
    """One bd issue, with the secret fields the map must never pass on."""
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


def make_app(spaces, build=None):
    """An app whose space is chosen by the ``x-user`` header (one per user)."""
    app = FastAPI()
    api = APIRouter()

    def current_space(request: Request):
        user = request.headers.get("x-user", "u1")
        if user not in spaces:
            raise HTTPException(status_code=401, detail="Sign in")
        return spaces[user]

    register_map_routes(api, current_space, build=build)
    app.include_router(api)
    return app


def state(root: Path) -> dict[str, tuple]:
    """Every path of *root* with its size and moment: a tree, byte for byte."""
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns, path.is_dir())
        for path in sorted(root.rglob("*"))
    }


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


def test_the_map_names_the_running_commit_and_nothing_more(space, bd):
    root = project(space)
    full = "abcdef0" + "1" * 33
    build = {"version": "1.2.3", "commit": "abcdef0", "commit_full": full, "source": "git"}
    app = make_app({"u1": space}, build=lambda: build)

    response = TestClient(app).get(f"/api/projects/{root.id}/map")

    assert response.status_code == 200
    # A website-only operator sees which code is live: the short commit, not a path.
    assert response.json()["server"] == {
        "version": "1.2.3", "commit": "abcdef0", "source": "git",
        "reason_code": None, "reason": None,
    }
    assert full not in response.text


def test_an_unknown_build_carries_a_typed_reason(space, bd):
    root = project(space)
    build = {"version": "1.2.3", "commit": "unknown", "commit_full": "unknown", "source": "unknown"}
    app = make_app({"u1": space}, build=lambda: build)

    server = TestClient(app).get(f"/api/projects/{root.id}/map").json()["server"]

    assert server["commit"] is None and server["source"] == "unknown"
    assert server["reason_code"] == map_api.BUILD_UNKNOWN
    assert server["reason"] == map_api.BUILD_UNKNOWN_TEXT
    assert server["version"] == "1.2.3"


def test_a_build_value_that_is_not_a_commit_is_never_shown(space, bd):
    """An environment stamp is not trusted as a hash: no paths or secrets leak."""
    root = project(space)
    build = {"version": "1.2.3", "commit": "/srv/secret", "commit_full": "/srv/secret", "source": "env"}
    body = TestClient(make_app({"u1": space}, build=lambda: build)).get(
        f"/api/projects/{root.id}/map"
    ).json()

    assert body["server"]["commit"] is None
    assert body["server"]["reason_code"] == map_api.BUILD_UNKNOWN
    assert "/srv/secret" not in json.dumps(body)


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
    assert body["tasks"] == map_api._no_tasks(map_api.ReasonCode.NO_TRACKER)
    assert bd.calls == [], "Reject unsafe discovery BEFORE executing any bd command"
    if kind == "missing-space":
        assert not directory.exists(), "GET must not create a space"


def test_foreign_or_mixed_ids_are_not_exposed(client, space, bd):
    root = project(space)
    for issues in ([issue("foreign-secret")], [issue(f"{root.id}-own"), issue("foreign-secret")]):
        bd.answer = {"success": True, "data": issues}
        tasks = client.get(f"/api/projects/{root.id}/map").json()["tasks"]
        assert tasks == map_api._no_tasks(map_api.ReasonCode.FOREIGN_TRACKER)


def test_tracker_lstat_failure_is_closed(space, monkeypatch):
    root = project(space)
    def denied(path):
        raise PermissionError("unreadable")
    monkeypatch.setattr(map_api.os, "lstat", denied)
    assert map_api._own_tracker(space.layout.project_spaces / root.id) is None


def test_tracker_process_pins_a_copy_and_strips_ambient_selectors(space, monkeypatch):
    root = project(space)
    directory = space.layout.project_spaces / root.id
    monkeypatch.setenv("BEADS_DIR", "/not-the-project")
    monkeypatch.setenv("BEADS_DOLT_SERVER_DATABASE", "foreign")
    monkeypatch.setenv("BD_DB", "/foreign")
    monkeypatch.setattr(map_api, "_bd_executable", lambda: "/usr/bin/bd")
    seen = {}
    def run(command, **kwargs):
        copied = Path(kwargs["cwd"])
        database = copied / ".beads" / "beads.db"
        seen["copied"] = copied
        assert command[0] == "/usr/bin/bd"
        assert command[1:3] == ["--db", str(database)]
        # The database read is a copy of the project's, never the project's own.
        assert database.is_file()
        assert directory not in database.parents
        env = kwargs["env"]
        assert env["BEADS_DIR"] == str(database.parent)
        assert env["BEADS_DB"] == str(database)
        assert "BD_DB" not in env and "BEADS_DOLT_SERVER_DATABASE" not in env
        # Every home bd and Dolt may write to is the temporary tree, not the project.
        home = Path(env["HOME"])
        assert home == Path(env["XDG_CONFIG_HOME"]) == Path(env["APPDATA"]) == Path(env["USERPROFILE"])
        assert not home.is_relative_to(directory) and not home.is_relative_to(copied)
        return SimpleNamespace(returncode=0, stdout="[]")
    monkeypatch.setattr(map_api.subprocess, "run", run)
    assert map_api._read_tracker(directory, list(map_api.BD_LIST_ARGS)) == {"success": True, "data": []}
    assert not seen["copied"].exists(), "the copy is temporary and goes with the read"


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
        assert client.get(f"/api/projects/{root.id}/map").json()["tasks"] == map_api._no_tasks(
            map_api.ReasonCode.NO_TRACKER
        )
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


def test_a_bd_refusal_answers_generically(client, space, bd):
    root = project(space)
    bd.answer = {"success": False, "code": map_api.ReasonCode.BD_FAILED,
                 "error": "SECRET /srv/private: token=abc"}

    response = client.get(f"/api/projects/{root.id}/map")

    assert response.status_code == 200
    assert response.json()["tasks"] == map_api._no_tasks(map_api.ReasonCode.BD_FAILED)
    # bd's own message may name paths or credentials: it never reaches the browser.
    assert "SECRET" not in response.text and "token" not in response.text


def test_a_docker_read_mounts_the_project_read_only_and_pins_the_copy(space, monkeypatch):
    root = project(space)
    directory = space.layout.project_spaces / root.id
    monkeypatch.setattr(map_api, "_bd_executable", lambda: None)
    monkeypatch.setattr(map_api, "_docker_bd_available", lambda: True)
    monkeypatch.setattr(map_api, "_get_isolation_image", lambda context=None: "grid-agent:test")
    seen = {}

    def run(command, **kwargs):
        seen["command"], seen["kwargs"] = list(command), kwargs
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(map_api.subprocess, "run", run)
    assert map_api._read_tracker(directory, list(map_api.BD_LIST_ARGS)) == {"success": True, "data": []}

    command = seen["command"]
    assert command[:4] == ["docker", "run", "--rm", "-i"]
    assert "grid-agent:test" in command
    project_mount = f"{directory}:{map_api.CONTAINER_PROJECT_DIR}:ro"
    assert project_mount in command
    copy_mount = command[command.index(project_mount) + 2]  # past the "-v" before it
    assert copy_mount.endswith(f":{map_api.CONTAINER_TRACKER_DIR}")
    assert "-w" in command and command[command.index("-w") + 1] == map_api.CONTAINER_TRACKER_DIR
    pinned = command[command.index("--db") + 1]
    assert pinned == f"{map_api.CONTAINER_TRACKER_DIR}/.beads/beads.db"
    # The command carries only explicit values; the server's environment stays home.
    assert "env" not in seen["kwargs"] and "cwd" not in seen["kwargs"]


def _copy_mount_source(command):
    """The host directory the read mounts at /tracker, from the built command."""
    mount = next(arg for arg in command if arg.endswith(f":{map_api.CONTAINER_TRACKER_DIR}"))
    return Path(mount.rsplit(":", 1)[0])


def _assert_owner_only(copied, uid):
    """Every entry of the copy belongs to *uid* and to nobody else."""
    for path in [copied, *copied.rglob("*")]:
        info = path.lstat()
        assert info.st_uid == uid, f"{path} is not owned by {uid}"
        expected = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
        assert stat.S_IMODE(info.st_mode) == expected, f"{path} is not {expected:o}"


def test_the_docker_copy_is_the_image_users_owner_only(space, monkeypatch):
    """The copy is handed to the uid bd runs as; the server's temp tree stays 0700.

    A bind mount keeps host owners: a root server's copy is root's, and uid 1000
    cannot open it. Here the image's uid is this process's uid (the one the test
    may chown to), which is enough to prove the copy is given to the image and
    narrowed to owner-only rather than widened to the world.
    """
    root = project(space)
    directory = space.layout.project_spaces / root.id
    uid = os.geteuid()
    monkeypatch.setattr(map_api, "_bd_executable", lambda: None)
    monkeypatch.setattr(map_api, "_docker_bd_available", lambda: True)
    monkeypatch.setattr(map_api, "_get_isolation_image", lambda context=None: "grid-agent:test")
    monkeypatch.setattr(map_api, "_container_uid", lambda image: uid)
    seen = {}

    def run(command, **kwargs):
        copied = _copy_mount_source(command)
        seen["copied"] = copied
        # Production-like temp tree: the server's temporary root is owner-only.
        assert stat.S_IMODE(copied.parents[1].stat().st_mode) == 0o700
        assert copied.is_dir() and (copied / ".beads" / "beads.db").is_file()
        _assert_owner_only(copied, uid)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(map_api.subprocess, "run", run)
    assert map_api._read_tracker(directory, list(map_api.BD_LIST_ARGS)) == {"success": True, "data": []}
    assert seen["copied"].parents[1].name.startswith("grid-map-")


def test_a_copy_that_cannot_be_handed_over_runs_as_its_owner(space, monkeypatch):
    """A non-root server cannot chown to uid 1000: bd runs as the copy's owner.

    Owner-only modes still admit that uid, and the copy is never world-writable.
    """
    if os.geteuid() == 0:
        pytest.skip("as root the copy can be handed to the image user instead")
    root = project(space)
    directory = space.layout.project_spaces / root.id
    monkeypatch.setattr(map_api, "_bd_executable", lambda: None)
    monkeypatch.setattr(map_api, "_docker_bd_available", lambda: True)
    monkeypatch.setattr(map_api, "_get_isolation_image", lambda context=None: "grid-agent:test")
    monkeypatch.setattr(map_api, "_container_uid", lambda image: os.geteuid() + 1)
    seen = {}

    def run(command, **kwargs):
        seen["command"] = list(command)
        copied = _copy_mount_source(command)
        # The copy keeps its owner; only that owner's bits are set.
        _assert_owner_only(copied, os.geteuid())
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(map_api.subprocess, "run", run)
    assert map_api._read_tracker(directory, list(map_api.BD_LIST_ARGS)) == {"success": True, "data": []}
    command = seen["command"]
    assert command[command.index("--user") + 1] == f"{os.geteuid()}:{os.getegid()}"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not available here")
def test_a_real_isolation_image_reads_the_ephemeral_copy(tmp_path, monkeypatch):
    """End to end through Docker: build a tracker, then read its temporary copy.

    This is the regression itself where Docker exists: the host may run as root
    while the image runs as uid 1000, and the read must still answer.
    """
    image = map_api._get_isolation_image(None)
    if not image or map_api._bd_executable() is None:
        pytest.skip("needs the isolation image and a host bd to build a tracker")
    if map_api._container_uid(image) is None:
        pytest.skip(f"the isolation image {image} is not available locally")
    directory = tmp_path / "prjreal"
    directory.mkdir()
    env = _bd_env(tmp_path)
    created = _real_tracker(directory, "prjreal", env, ["Docker copy read"])

    monkeypatch.setattr(map_api, "_bd_executable", lambda: None)  # force the docker runner
    monkeypatch.setattr(map_api, "_docker_bd_available", lambda: True)
    monkeypatch.setattr(map_api, "_get_isolation_image", lambda context=None: image)
    answer = map_api._read_tracker(directory, list(map_api.BD_LIST_ARGS))

    assert answer.get("success") is True, answer
    assert {issue["id"] for issue in answer["data"]} == {task["id"] for task in created}


def test_a_bd_failure_is_logged_but_never_returned(space, monkeypatch, caplog):
    root = project(space)
    directory = space.layout.project_spaces / root.id
    monkeypatch.setattr(map_api, "_bd_executable", lambda: "/usr/bin/bd")

    def run(command, **kwargs):
        return SimpleNamespace(returncode=3, stdout="", stderr="Error: /home/secret/tracker: token=abc\n")

    monkeypatch.setattr(map_api.subprocess, "run", run)
    with caplog.at_level(logging.WARNING, logger="web_chat.map_api"):
        answer = map_api._read_tracker(directory, list(map_api.BD_LIST_ARGS))

    assert answer["code"] is map_api.ReasonCode.BD_FAILED
    assert answer["error"] == map_api.REASON_TEXT[map_api.ReasonCode.BD_FAILED]
    assert "secret" not in json.dumps(answer, default=str).lower()
    logged = [record.getMessage() for record in caplog.records]
    assert any("exited 3" in line and "/home/secret/tracker" in line for line in logged)


def test_a_read_on_this_host_writes_nothing_into_the_project(tmp_path):
    """The real CLI, on a synthetic tracker: the project's tree is untouched."""
    bd = map_api._bd_executable()
    if bd is None:
        pytest.skip("bd is not installed on this host")
    directory = tmp_path / "synthetic" / "prjtest"
    directory.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "SYSTEMROOT", "WINDIR"}}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), BD_DISABLE_METRICS="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(["git", "init", str(directory)], env=env, check=True, capture_output=True)
    subprocess.run([bd, "init", "--prefix", "prjtest", "--skip-agents", "--skip-hooks"], cwd=directory,
                   env=env, check=True, capture_output=True)
    created = subprocess.run([bd, "create", "ONLY THIS TASK", "--json"], cwd=directory, env=env,
                             check=True, capture_output=True, text=True)
    assert json.loads(created.stdout)["id"].startswith("prjtest-")
    before = state(directory)

    answer = map_api._read_tracker(directory, list(map_api.BD_LIST_ARGS))

    assert answer["success"] is True, answer
    assert [item["title"] for item in answer["data"]] == ["ONLY THIS TASK"]
    assert state(directory) == before, "the map changed the project it read"
    # The read used to put Dolt's own config here, through HOME=<project>.
    assert not (directory / ".dolt").exists()


def test_no_bd_and_no_docker_says_the_runtime_is_missing(space, monkeypatch):
    root = project(space)
    monkeypatch.setattr(map_api, "_bd_executable", lambda: None)
    monkeypatch.setattr(map_api, "_docker_bd_available", lambda: False)

    def forbidden(*args, **kwargs):
        raise AssertionError("bd must not run when this server has no runtime for it")

    monkeypatch.setattr(map_api.subprocess, "run", forbidden)
    tasks = TestClient(make_app({"u1": space})).get(f"/api/projects/{root.id}/map").json()["tasks"]

    assert tasks == map_api._no_tasks(map_api.ReasonCode.BD_UNAVAILABLE)
    assert "runtime" in tasks["reason"]


def test_a_tracker_too_large_to_copy_is_not_read(space, monkeypatch):
    root = project(space)
    directory = space.layout.project_spaces / root.id
    monkeypatch.setattr(map_api, "MAX_TRACKER_COPY_BYTES", 0)

    def forbidden(*args, **kwargs):
        raise AssertionError("bd must not run on a tracker the map refuses to copy")

    monkeypatch.setattr(map_api.subprocess, "run", forbidden)
    answer = map_api._read_tracker(directory, list(map_api.BD_LIST_ARGS))

    assert answer["code"] is map_api.ReasonCode.TRACKER_TOO_LARGE


def test_a_tracker_that_does_not_answer_in_time(monkeypatch, client, space):
    root = project(space)

    async def slow(directory, args):
        await asyncio.sleep(5)
        return {"success": True, "data": []}

    monkeypatch.setattr(map_api, "_bd_in", slow)
    monkeypatch.setattr(map_api, "BD_TIMEOUT_SECONDS", 0.01)

    tasks = client.get(f"/api/projects/{root.id}/map").json()["tasks"]

    assert tasks == map_api._no_tasks(map_api.ReasonCode.TIMEOUT)


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


# -- typed reasons ---------------------------------------------------------------


def test_map_identifies_the_commit_the_server_loaded(space):
    root = project(space)
    build = {"version": "1.2.3", "commit": "abcdef0123456789", "source": "git",
             "commit_full": "abcdef0123456789"}

    body = TestClient(make_app({"u1": space}, build=lambda: build)).get(
        f"/api/projects/{root.id}/map"
    ).json()

    assert body["server"] == {
        "version": "1.2.3", "commit": "abcdef0", "source": "git",
        "reason_code": None, "reason": None,
    }
    assert "commit_full" not in json.dumps(body)


def test_map_names_unknown_or_untrusted_commit_without_leaking_it(space):
    root = project(space)
    build = {"version": "1.2.3", "commit": "../private/path", "source": "env"}

    body = TestClient(make_app({"u1": space}, build=lambda: build)).get(
        f"/api/projects/{root.id}/map"
    ).json()

    assert body["server"]["commit"] is None
    assert body["server"]["reason_code"] == map_api.BUILD_UNKNOWN
    assert body["server"]["reason"] == map_api.BUILD_UNKNOWN_TEXT
    assert "private" not in json.dumps(body)


def test_each_unavailable_section_carries_the_code_of_its_sentence(tmp_path, bd):
    """A client branches on ``reason_code``; the sentence is only for a person."""
    space = FakeSpace(tmp_path / "u1", project_spaces=False)
    root = space.project_store.create("No space")

    body = TestClient(make_app({"u1": space})).get(f"/api/projects/{root.id}/map").json()

    for section in ("tasks", "files"):
        assert body[section]["available"] is False
        assert body[section]["reason_code"] == map_api.ReasonCode.NO_SPACE.value
        assert body[section]["reason"] == map_api.REASON_TEXT[map_api.ReasonCode.NO_SPACE]
    # An available section says so with a null code, never with an absent key.
    assert body["processes"]["available"] is True
    assert body["processes"]["reason_code"] is None


def test_processes_that_cannot_be_listed_name_their_code(tmp_path, bd):
    space = FakeSpace(tmp_path / "u1")
    space.context_manager = None
    root = project(space)

    processes = TestClient(make_app({"u1": space})).get(f"/api/projects/{root.id}/map").json()["processes"]

    assert processes["available"] is False
    assert processes["reason_code"] == map_api.ReasonCode.NO_CHATS.value
    assert processes["reason"] == map_api.REASON_TEXT[map_api.ReasonCode.NO_CHATS]
    assert processes["items"] == [] and processes["total"] == 0


def test_files_that_are_not_a_folder_name_their_code(tmp_path, bd):
    space = FakeSpace(tmp_path / "u1")
    root = space.project_store.create("Not a folder")
    space.layout.project_spaces.mkdir(parents=True, exist_ok=True)
    (space.layout.project_spaces / root.id).write_text("not a directory", encoding="utf-8")

    files = TestClient(make_app({"u1": space})).get(f"/api/projects/{root.id}/map").json()["files"]

    assert files["available"] is False
    assert files["reason_code"] == map_api.ReasonCode.NOT_A_FOLDER.value
    assert files["reason"] == map_api.REASON_TEXT[map_api.ReasonCode.NOT_A_FOLDER]


# -- end to end: the server's own app, a real tracker ----------------------------


def _bd_env(tmp_path: Path) -> dict:
    """A bd environment of its own: no inherited tracker selector or user config."""
    home = tmp_path / "bd-home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "SYSTEMROOT", "WINDIR"}}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), BD_DISABLE_METRICS="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return env


def _bd_run(directory: Path, env: dict, *args: str, timeout: int = 60) -> str:
    """One successful bd command in *directory*: its stdout."""
    executable = map_api._bd_executable()
    assert executable is not None
    result = subprocess.run([executable, *args], cwd=directory, env=env, check=False,
                            capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _real_tracker(directory: Path, project_id: str, env: dict, tasks: list[str]) -> list[dict]:
    """A real local tracker with *tasks*, the second depending on the first."""
    directory.mkdir(parents=True, exist_ok=True)
    # A git boundary of its own: bd must not adopt an ancestor's tracker.
    subprocess.run(["git", "init", str(directory)], env=env, check=True, capture_output=True)
    _bd_run(directory, env, "init", "--prefix", project_id, "--skip-agents", "--skip-hooks")
    created = []
    for title in tasks:
        created.append(json.loads(_bd_run(directory, env, "create", title, "--json")))
    if len(created) > 1:
        _bd_run(directory, env, "dep", "add", created[1]["id"], created[0]["id"])
    return created


def test_the_map_reads_a_real_tracker_through_the_servers_own_app(tmp_path):
    """bd init, two tasks and a dependency, read by the app the server builds."""
    if map_api._bd_executable() is None:
        pytest.skip("bd is not installed on this host: the end-to-end map test needs the real bd")
    from tests.test_web_ui_routes import _DummySpace, _server

    root = tmp_path / "server"
    store = ProjectStore(root / "projects.json")
    record = store.create("End to end")
    space = _DummySpace()
    space.project_store = store
    space.project_exists = store.exists
    space.chat_spaces = True
    space.layout = SimpleNamespace(project_spaces=root / "spaces")
    space.context_manager = lambda: FakeManager([view("chat-e2e", record.id, title="Map chat")])
    space.awaiting_approval = lambda: {}

    env = _bd_env(tmp_path)
    directory = root / "spaces" / record.id
    created = _real_tracker(directory, record.id, env, ["First task", "Second task"])
    (directory / "notes.md").write_text("a file of the project", encoding="utf-8")
    before = state(directory)

    response = TestClient(_server(space).app).get(f"/api/projects/{record.id}/map")

    assert response.status_code == 200
    body = response.json()
    tasks = body["tasks"]
    assert tasks["available"] is True, tasks
    assert tasks["reason"] is None and tasks["reason_code"] is None
    # The map's own rule: oldest first, ties by id. bd keeps created_at to the
    # second, so two tasks made in one second are ordered by their (random) ids.
    assert sorted(item["id"] for item in tasks["items"]) == sorted(task["id"] for task in created)
    order = [(item["created_at"] or "", item["id"]) for item in tasks["items"]]
    assert order == sorted(order), "the map must keep tasks oldest first, ties by id"
    dependent = next(item for item in tasks["items"] if item["id"] == created[1]["id"])
    assert dependent["depends_on"] == [created[0]["id"]]
    assert [item["name"] for item in body["files"]["items"]] == ["notes.md"]
    assert [item["chat_id"] for item in body["processes"]["items"]] == ["chat-e2e"]
    # The read went through a temporary copy: the project is as it was.
    assert state(directory) == before, "the map changed the project it read"
    assert not (directory / ".dolt").exists()


def test_without_a_host_bd_the_map_takes_the_isolation_image_path(space, monkeypatch):
    """No bd on PATH and docker mocked: the fallback runs, with the project read-only."""
    root = project(space)
    monkeypatch.setattr(map_api, "_bd_executable", lambda: None)
    monkeypatch.setattr(map_api, "_docker_bd_available", lambda: True)
    monkeypatch.setattr(map_api, "_get_isolation_image", lambda context=None: "grid-agent:test")
    answer = [issue(f"{root.id}-1", status="open")]
    seen = {}

    def run(command, **kwargs):
        seen["command"] = list(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(answer), stderr="")

    monkeypatch.setattr(map_api.subprocess, "run", run)
    body = TestClient(make_app({"u1": space})).get(f"/api/projects/{root.id}/map").json()

    assert body["tasks"]["available"] is True, body["tasks"]
    assert body["tasks"]["reason_code"] is None
    assert [item["id"] for item in body["tasks"]["items"]] == [f"{root.id}-1"]
    assert seen["command"][:3] == ["docker", "run", "--rm"]
    assert f"{space.layout.project_spaces / root.id}:{map_api.CONTAINER_PROJECT_DIR}:ro" in seen["command"]


# -- the diagnostic command ------------------------------------------------------


def test_map_check_reads_a_copy_and_leaves_it_alone(tmp_path, capsys):
    """The operator's command: the server's path, a report, and no write."""
    from web_chat import map_check

    if map_api._bd_executable() is None:
        pytest.skip("bd is not installed on this host: map_check needs the real bd")
    directory = tmp_path / "copy" / "prjmapcheck"
    _real_tracker(directory, "prjmapcheck", _bd_env(tmp_path), ["Only task", "Second task"])
    before = state(directory)

    code = map_check.main([str(directory), "--json"])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["runner"]["kind"] == "host"
    assert report["runner"]["executable"] == map_api._bd_executable()
    assert report["reason_code"] is None
    assert report["tasks"] == 2
    assert report["project_tree"]["changed"] is False
    assert state(directory) == before


def test_map_check_fails_loudly_when_there_is_no_tracker(tmp_path, capsys):
    from web_chat import map_check

    directory = tmp_path / "prjempty"
    directory.mkdir()
    (directory / "notes.md").write_text("kept", encoding="utf-8")

    code = map_check.main([str(directory)])

    assert code == 1
    out = capsys.readouterr().out
    assert "reason_code:  no_tracker" in out
    assert "project_tree: unchanged" in out
    assert (directory / "notes.md").read_text(encoding="utf-8") == "kept"
