"""Chat spaces: every new chat works in a space of its own, with a container of its
own, and reaches its project's space at /project and its user's at /user."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from core.managers import container_manager as containers_module
from core.managers.container_manager import CONTAINER_WORKDIR, ContainerManager
from core.memory import MemoryRecord
from schemas.schemas import IsolationConfig, UploadsPolicy
from tests.test_container_manager import FakeClient
from tests.test_web_space import MINIMAL_CONFIG
from utils import confined_fs
from utils.path_utils import (
    container_path,
    display_agent_path,
    factory_path_context,
    resolve_agent_path,
    sanitize_text_for_agent,
)
from web_chat import space as space_module
from web_chat.deployment import Deployment
from web_chat.explorer_api import register_explorer_routes
from web_chat.space import CHAT_SPACE_KEY, SpaceLayout, UserSpace
from web_chat.uploads import register_upload_routes


# -- paths ------------------------------------------------------------------------
class _Config:
    def __init__(self, working_directory):
        self._working_directory = str(working_directory)

    def get_working_directory(self):
        return self._working_directory


class _Factory:
    def __init__(self, chat, project, user, container_id="c-1"):
        self.config = _Config(chat)
        self.container_id = container_id
        self.extra_roots = {"/project": str(project), "/user": str(user)}


@pytest.fixture
def spaces(tmp_path):
    chat, project, user = tmp_path / "chats" / "ctx-1", tmp_path / "projects" / "prj-1", tmp_path / "workspace"
    for directory in (chat, project, user):
        directory.mkdir(parents=True)
    return chat.resolve(), project.resolve(), user.resolve()


def test_a_chat_reaches_its_project_and_user_spaces_by_their_paths(spaces):
    chat, project, user = spaces
    factory = _Factory(chat, project, user)

    assert resolve_agent_path("notes.md", factory) == str(chat / "notes.md")
    assert resolve_agent_path("/workspace/notes.md", factory) == str(chat / "notes.md")
    assert resolve_agent_path("/project/src/app.py", factory) == str(project / "src" / "app.py")
    assert resolve_agent_path("/project", factory) == str(project)
    assert resolve_agent_path("/user/data.csv", factory) == str(user / "data.csv")
    # A leaked host path of another space stays in it.
    assert resolve_agent_path(str(project / "a"), factory) == str(project / "a")
    # "/projects" is not "/project": a plain absolute path is the chat's own, as before.
    assert resolve_agent_path("/projects/x", factory) == str(chat / "projects" / "x")


@pytest.mark.parametrize("path", ["/project/../workspace/secret", "/user/../../etc/passwd", "../outside"])
def test_no_space_is_left_upwards(spaces, path):
    chat, project, user = spaces
    with pytest.raises(ValueError, match="escapes"):
        resolve_agent_path(path, _Factory(chat, project, user))


def test_the_other_spaces_read_and_run_as_their_container_paths(spaces):
    chat, project, user = spaces
    factory = _Factory(chat, project, user)

    assert display_agent_path("/project/src/app.py", factory) == "/project/src/app.py"
    assert display_agent_path(str(user / "a" / "b.txt"), factory) == "/user/a/b.txt"
    assert display_agent_path("src/app.py", factory) == "src/app.py"
    assert container_path(str(project / "src"), factory) == "/project/src"
    assert container_path(str(user), factory) == "/user"
    assert container_path(str(chat / "out"), factory) == f"{CONTAINER_WORKDIR}/out"
    text = f"wrote {project}/src/app.py and {user}/x and {chat}/y"
    assert sanitize_text_for_agent(text, factory) == "wrote /project/src/app.py and /user/x and ./y"


def test_files_of_the_other_spaces_are_opened_without_following_links(spaces, monkeypatch):
    chat, project, user = spaces
    monkeypatch.setattr(confined_fs, "SUPPORTED", True)
    factory = _Factory(chat, project, user)
    (project / "docs").mkdir()
    with factory_path_context(factory):
        confined_fs.write_text(resolve_agent_path("/project/docs/a.md", factory), "hello")
        assert (project / "docs" / "a.md").read_text() == "hello"
        os.symlink("/etc", project / "etc-link")
        with pytest.raises(confined_fs.LinkRefused):
            confined_fs.read_text(str(project / "etc-link" / "hostname"))


def test_the_policy_names_another_spaces_file_as_its_links_do(spaces):
    from core.action_policy import ActionGate

    chat, project, user = spaces
    locate = ActionGate._locator(SimpleNamespace(factory=_Factory(chat, project, user)))

    assert locate("/project/.env") == "project/.env"
    assert locate(str(user / "notes.md")) == "user/notes.md"
    assert locate("/workspace/out.txt") == "out.txt"
    assert locate("/project/../../etc/passwd") is None
    assert locate("/etc/passwd") is None


def test_a_bash_command_runs_in_the_space_it_names():
    from examples.coder.tools.bash_tool import _container_command

    assert _container_command("ls", "c-1", "/project/src", 10)[4] == "/project/src"
    assert _container_command("ls", "c-1", "src", 10)[4] == "/workspace/src"
    assert _container_command("ls", "c-1", ".", 10)[4] == "/workspace"


# -- containers -------------------------------------------------------------------
@pytest.fixture
def docker(monkeypatch):
    client = FakeClient()
    original = client.run

    def run(image, command=None, **kwargs):
        container = original(image, command, **kwargs)
        if command is None:
            container.attrs["Mounts"] = [
                {"Type": "bind", "Destination": spec["bind"], "Source": source}
                for source, spec in kwargs["volumes"].items()
            ]
        return container

    client.run = run
    monkeypatch.setattr(containers_module, "docker", SimpleNamespace(from_env=lambda: client))
    monkeypatch.setattr(containers_module, "_AGENT_IDS", {})
    config = SimpleNamespace(config=SimpleNamespace(isolation=IsolationConfig(enabled=True)))
    return ContainerManager(config), client


def test_a_chats_container_mounts_its_spaces_and_is_recreated_when_they_change(docker, spaces):
    manager, client = docker
    chat, project, user = spaces

    first = manager.get_or_create_container("u1", chat, mounts={"/project": project, "/user": user})
    [(_, run)] = client.runs
    assert run["volumes"] == {
        str(chat): {"bind": CONTAINER_WORKDIR, "mode": "rw"},
        str(project): {"bind": "/project", "mode": "rw"},
        str(user): {"bind": "/user", "mode": "rw"},
    }
    assert manager.get_or_create_container("u1", chat, mounts={"/project": project, "/user": user}) is first

    # The chat left its project: the container without /project replaces it.
    second = manager.get_or_create_container("u1", chat, mounts={"/user": user})
    assert first.removed and second is not first
    assert len(client.runs) == 2

    manager.remove_container("u1", second.id)
    assert second.removed


# -- the space --------------------------------------------------------------------
class _Containers:
    made = []
    removed = []

    def __init__(self, config, enabled=None):
        self.enabled = True

    def get_or_create_container(self, user_id, workspace=None, mounts=None):
        _Containers.made.append((Path(workspace), dict(mounts or {})))
        return SimpleNamespace(id=f"c-{Path(workspace).name}", name=f"grid-agent-{Path(workspace).name}")

    def stop_container(self, user_id, container_id):
        pass

    def remove_container(self, user_id, container_id):
        _Containers.removed.append(container_id)


@pytest.fixture
def user_space(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    monkeypatch.setattr("web_chat.space.ContainerManager", _Containers)
    _Containers.made, _Containers.removed = [], []
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    root = tmp_path / "users" / "u1"
    space = UserSpace(
        Deployment(config_path=str(config), routing_path=None),
        user_id="u1",
        layout=SpaceLayout.under(root),
        require_isolation=True,
        chat_spaces=True,
    )
    return space, root


def _new_chat(space, project_id=None):
    chat = space.context_manager().start_new_context()
    if project_id:
        space.update_conversation_metadata(chat, project_id=project_id)
    space.assign_chat_space(chat)
    return chat


@pytest.mark.asyncio
async def test_every_new_chat_works_in_a_space_and_container_of_its_own(user_space):
    space, root = user_space
    project = space.project_store.create("Atlas")
    one, two = _new_chat(space, project.id), _new_chat(space)

    assert space.conversation_workspace(one) == (root / "chats" / one).resolve()
    first, second = await space.registry_for(one), await space.registry_for(two)
    assert first is not second and first is not space.registry
    assert await space.registry_for(one) is first

    factory = first.factory(first.default_key())
    assert factory.config.get_working_directory() == str((root / "chats" / one).resolve())
    assert factory.container_id == f"c-{one}"
    assert factory.extra_roots == {
        "/user": str(space.workspace_path),
        "/project": str((root / "projects" / project.id).resolve()),
    }
    # The project's chats plan in its tracker; memory is kept beside the user's space.
    assert factory.tracker_directory == str((root / "projects" / project.id).resolve())
    assert factory.memory_root == str(space.workspace_path)
    assert factory.memory_db == str(root / "memory.sqlite")
    assert second.factory(second.default_key()).tracker_directory == str(space.workspace_path)
    keys = [section.key for section in factory.instructions_builder.platform_sections]
    assert keys == ["workspace_file_links", "chat_spaces"]
    assert "Atlas" in factory.instructions_builder.platform_sections[1].content

    # The chat without a project reaches only its own space and the user's.
    assert second.factory(second.default_key()).extra_roots == {"/user": str(space.workspace_path)}
    assert [mounts for _, mounts in _Containers.made[1:]] == [
        {"/user": space.workspace_path, "/project": (root / "projects" / project.id).resolve()},
        {"/user": space.workspace_path},
    ]
    await space.close()
    assert sorted(_Containers.removed) == sorted([f"c-{one}", f"c-{two}"])


@pytest.mark.asyncio
async def test_a_chat_from_before_chat_spaces_keeps_the_users_workspace(user_space):
    space, _ = user_space
    old = space.context_manager().start_new_context()  # never given a space

    assert space.chat_roots(old) is None
    assert space.conversation_workspace(old) == space.workspace_path
    assert await space.registry_for(old) is space.registry


@pytest.mark.asyncio
async def test_branches_and_forks_keep_their_chats_space(user_space):
    space, _ = user_space
    manager = space.context_manager()
    chat = _new_chat(space)
    manager.add_message("user", "hello")
    message_id = manager.conversation_view(chat)["messages"][0].metadata["message_id"]

    branch, _ = manager.fork_context(chat, message_id)
    fork = manager.copy_context(chat, 1)

    assert space.conversation_workspace(branch) == space.conversation_workspace(chat)
    assert space.conversation_workspace(fork) == space.conversation_workspace(chat)
    # A continuation in a new chat keeps working on the same files.
    continued = manager.start_new_context()
    space.assign_chat_space(continued, like=chat)
    assert manager.get_context_metadata(continued)[CHAT_SPACE_KEY] == chat


@pytest.mark.asyncio
async def test_a_chat_moved_to_a_project_gets_its_space_next_turn(user_space):
    space, root = user_space
    chat = _new_chat(space)
    before = await space.registry_for(chat)
    project = space.project_store.create("Atlas")
    space.update_conversation_metadata(chat, project_id=project.id)

    after = await space.registry_for(chat)

    assert after is not before
    assert after.factory(after.default_key()).extra_roots["/project"] == str((root / "projects" / project.id).resolve())
    await space.close()


@pytest.mark.asyncio
async def test_only_so_many_chats_stay_loaded_and_idle_ones_go(user_space, monkeypatch):
    space, _ = user_space
    monkeypatch.setattr(space_module, "MAX_CHAT_RUNTIMES", 2)
    chats = [_new_chat(space) for _ in range(3)]
    space.turns.claim(chats[0])  # a turn is starting there: never dropped

    for chat in chats:
        await space.registry_for(chat)

    assert len(space._chat_runtimes) == 2
    assert _Containers.removed == [f"c-{chats[1]}"]
    space.turns.release(chats[0])
    assert await space.sweep_chat_runtimes(idle_seconds=0) == 2
    assert not space._chat_runtimes


def test_a_turn_checkpoints_and_restores_every_space_of_its_chat(user_space):
    space, root = user_space
    project = space.project_store.create("Atlas")
    chat = _new_chat(space, project.id)
    own, _ = space.file_spaces(chat)
    project_dir = root / "projects" / project.id
    (own / "draft.md").write_text("draft")
    (project_dir / "spec.md").write_text("v1")

    before = space.snapshot(chat)
    (project_dir / "spec.md").write_text("v2")
    (space.workspace_path / "new.txt").write_text("new")
    (own / "draft.md").unlink()
    after = space.snapshot(chat)

    changes = sorted((change["path"], change["status"]) for change in space.checkpoint_changes(before, after))
    assert changes == [("draft.md", "deleted"), ("project/spec.md", "modified"), ("user/new.txt", "added")]
    space.restore_checkpoint(chat, before, [{"before": before, "after": after}])
    assert (project_dir / "spec.md").read_text() == "v1"
    assert (own / "draft.md").read_text() == "draft"
    assert not (space.workspace_path / "new.txt").exists()


def test_restoring_a_chat_leaves_what_other_chats_changed_in_shared_spaces(user_space):
    space, root = user_space
    project = space.project_store.create("Atlas")
    chat = _new_chat(space, project.id)
    space.file_spaces(chat)
    project_dir = root / "projects" / project.id
    for name in ("mine.md", "theirs.md", "later.md"):
        (project_dir / name).write_text("v1")

    before = space.snapshot(chat)
    (project_dir / "mine.md").write_text("by this chat")
    after = space.snapshot(chat)
    # Another chat of the project, after this chat's turn: a file of its own, and one this chat changed too.
    (project_dir / "theirs.md").write_text("by another chat")
    (project_dir / "later.md").write_text("by another chat")
    space.restore_checkpoint(chat, before, [{"before": before, "after": after}])

    assert (project_dir / "mine.md").read_text() == "v1"
    assert (project_dir / "theirs.md").read_text() == "by another chat"
    assert (project_dir / "later.md").read_text() == "by another chat"

    # Changed by this chat, then by another one: the other chat's version stays.
    before = space.snapshot(chat)
    (project_dir / "mine.md").write_text("again by this chat")
    after = space.snapshot(chat)
    (project_dir / "mine.md").write_text("then by another chat")
    space.restore_checkpoint(chat, before, [{"before": before, "after": after}])
    assert (project_dir / "mine.md").read_text() == "then by another chat"
    # A turn kept without what it ended with puts nothing of a shared space back.
    space.restore_checkpoint(chat, before, [{"before": before}])
    assert (project_dir / "mine.md").read_text() == "then by another chat"


# -- files over the web -------------------------------------------------------------
@pytest.fixture
def web(user_space):
    space, root = user_space

    async def current(request: Request):
        if request.headers.get("x-user") != "u1":
            raise HTTPException(status_code=401)
        return space

    api = APIRouter()
    register_explorer_routes(api, current)
    register_upload_routes(api, current, lambda: UploadsPolicy(max_file_mb=1, max_files=3, quota_mb=2))
    app = FastAPI()
    app.include_router(api)
    return TestClient(app, headers={"x-user": "u1"}), space, root


def test_the_files_page_shows_a_chats_spaces_and_hands_files_between_them(web):
    client, space, root = web
    project = space.project_store.create("Atlas")
    chat = _new_chat(space, project.id)
    scope = {"context": chat}

    top = client.get("/api/workspace/tree", params=scope).json()
    assert top["root_label"] == "This chat"
    assert [(entry["name"], entry.get("space"), entry.get("label")) for entry in top["entries"]] == [
        ("project", "project", "Project · Atlas"),
        ("user", "user", "Your files"),
    ]

    # An upload before the chat's first turn lands in its own space.
    upload = client.post("/api/workspace/uploads", params=scope, files={"files": ("data.csv", b"1,2")})
    assert upload.status_code == 200, upload.text
    assert upload.json()["files"][0]["path"] == "uploads/data.csv"
    assert (root / "chats" / chat / "uploads" / "data.csv").read_bytes() == b"1,2"

    # Into the project's space: the result outlives the chat.
    moved = client.post("/api/workspace/move", params=scope, json={"source": "uploads/data.csv", "target": "project/data.csv"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["path"] == "project/data.csv"
    assert (root / "projects" / project.id / "data.csv").read_bytes() == b"1,2"
    assert client.get("/api/workspace/files/project/data.csv", params=scope).content == b"1,2"
    listing = client.get("/api/workspace/tree", params={**scope, "path": "project"}).json()
    assert [entry["path"] for entry in listing["entries"]] == ["project/data.csv"]

    upload = client.post("/api/workspace/uploads", params={**scope, "dir": "user"}, files={"files": ("n.txt", b"n")})
    assert upload.json()["files"][0]["path"] == "user/n.txt"
    assert (space.workspace_path / "n.txt").read_bytes() == b"n"

    # A space itself is not a file to delete.
    assert client.delete("/api/workspace/entry", params={**scope, "path": "project"}).status_code == 400
    assert (root / "projects" / project.id).is_dir()


def test_a_chat_from_before_sees_the_users_workspace_as_always(web):
    client, space, _ = web
    old = space.context_manager().start_new_context()
    (space.workspace_path / "report.md").write_text("# r")

    listing = client.get("/api/workspace/tree", params={"context": old}).json()
    assert listing["root_label"] == "Workspace"
    assert [entry["name"] for entry in listing["entries"]] == ["report.md"]
    assert client.get("/api/workspace/files/report.md", params={"context": old}).content == b"# r"


# -- memory levels over the web ------------------------------------------------------
def test_the_memory_panel_shows_and_moves_records_between_a_chats_levels(user_space):
    from web_chat.memory_api import register_memory_routes

    space, _ = user_space

    async def current():
        return space

    api = APIRouter()
    register_memory_routes(api, current)
    app = FastAPI()
    app.include_router(api)
    client = TestClient(app)
    project = space.project_store.create("Atlas")
    one, two, solo = _new_chat(space, project.id), _new_chat(space, project.id), _new_chat(space)

    made = client.post("/api/memory/records", json={"content": "Atlas собирается make dist", "context_id": one})
    assert made.status_code == 201, made.text
    assert made.json()["level"] == "project"
    mine = client.post("/api/memory/records", json={"content": "Только в этом чате", "level": "chat", "context_id": one})
    assert mine.json()["level"] == "chat"

    overview = client.get("/api/memory", params={"context_id": two}).json()
    assert [(item["level"], item["count"]) for item in overview["levels"]] == [("chat", 0), ("project", 1), ("user", 0)]
    assert [record["content"] for record in overview["records"]] == ["Atlas собирается make dist"]
    assert client.get("/api/memory", params={"context_id": solo}).json()["records"] == []

    # Moved to the user's level: every chat now has it, the project no longer.
    moved = client.put(f"/api/memory/records/{made.json()['id']}", json={"level": "user", "context_id": one})
    assert moved.status_code == 200, moved.text
    assert moved.json()["level"] == "user"
    solo_records = client.get("/api/memory", params={"context_id": solo}).json()["records"]
    assert [(record["content"], record["level"]) for record in solo_records] == [("Atlas собирается make dist", "user")]
    assert client.get("/api/memory", params={"context_id": two, "level": "project"}).json()["records"] == []
    assert client.delete(f"/api/memory/records/{mine.json()['id']}", params={"context_id": one}).status_code == 200
    assert client.post("/api/memory/records", json={"content": "x", "level": "project", "context_id": solo}).status_code == 400


# -- memory beside the workspace -------------------------------------------------------
def test_memory_moves_out_of_the_workspace_where_the_agents_reached_it(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    root = tmp_path / "users" / "u1"
    old = root / "workspace" / ".grid" / "memory.sqlite"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"sqlite")
    space = UserSpace(Deployment(config_path=str(config), routing_path=None), user_id="u1",
                      layout=SpaceLayout.under(root), chat_spaces=True)

    assert not old.exists() and (root / "memory.sqlite").read_bytes() == b"sqlite"
    assert space.registry.factory(space.registry.default_key()).memory_db == str(root / "memory.sqlite")


# -- trackers ----------------------------------------------------------------------------
def test_a_projects_tasks_are_in_its_own_tracker_and_found_by_their_ids(user_space):
    from web_chat import tasks_api

    space, root = user_space
    project = space.project_store.create("Atlas")
    assert space.tracker_dir(None) == space.workspace_path
    assert space.tracker_dir(project.id) == (root / "projects" / project.id).resolve()
    assert space.tracker_dir("prj-00000000") == space.workspace_path  # no such project
    assert tasks_api._tracker(space, task_id=f"{project.id}-x7q") == space.tracker_dir(project.id)
    assert tasks_api._tracker(space, task_id="workspace-abc") == space.workspace_path
    assert space.project_trackers() == {}
    (root / "projects" / project.id / ".beads").mkdir()
    assert space.project_trackers() == {project.id: (root / "projects" / project.id).resolve()}


def test_the_task_list_gathers_every_tracker_and_writes_go_to_the_tasks_own(user_space):
    from web_chat import tasks_api
    from web_chat.tasks_api import register_task_routes

    space, root = user_space
    project = space.project_store.create("Atlas")
    project_dir = space.tracker_dir(project.id)
    (project_dir / ".beads").mkdir()
    calls = []

    async def bd(args, cwd=None):
        calls.append((args[0], cwd))
        owner = f"{project.id}-a1" if cwd == str(project_dir) else "workspace-b2"
        return {"success": True, "data": [{"id": owner, "title": "t", "status": "open"}]}

    tasks_api._bd_problem, tasks_api.run_bd = (lambda: None), bd

    async def current():
        return space

    api = APIRouter()
    register_task_routes(api, current)
    app = FastAPI()
    app.include_router(api)
    client = TestClient(app)

    listed = client.get("/api/tasks").json()
    assert sorted((task["id"], task.get("project_id")) for task in listed["tasks"]) == [
        (f"{project.id}-a1", project.id), ("workspace-b2", None)]
    calls.clear()
    assert client.post(f"/api/tasks/{project.id}-a1/close", json={"reason": "done"}).status_code == 200
    assert client.post("/api/tasks", json={"title": "new", "project_id": project.id}).status_code == 201
    assert client.post("/api/tasks", json={"title": "mine"}).status_code == 201
    assert [cwd for _, cwd in calls] == [str(project_dir), str(project_dir), str(space.workspace_path)]
    refused = client.post(f"/api/tasks/{project.id}-a1/deps", json={"action": "add", "parent_id": "workspace-b2"})
    assert refused.status_code == 400


# -- what goes with a chat or a project ---------------------------------------------------
@pytest.mark.asyncio
async def test_a_deleted_chats_space_goes_to_the_trash_with_its_memory_and_container(user_space):
    space, root = user_space
    chat = _new_chat(space)
    keeper = _new_chat(space)
    fork = space.context_manager().start_new_context()
    space.assign_chat_space(fork, like=keeper)  # works in keeper's space
    own, _ = space.file_spaces(chat)
    (own / "draft.md").write_text("x")
    await space.registry_for(chat)
    factory = space.registry.factory(space.registry.default_key())
    memory = factory.level_memory_manager("chat", chat, force=True)
    memory.remember(MemoryRecord("Только этот чат помнит это"))

    spaces = space.chat_space_ids([chat])
    deleted = space.context_manager().delete_family(chat)
    assert await space.release_chats(deleted, spaces) == [chat]

    assert not own.exists()
    assert [entry.name.startswith(chat) for entry in (root / "trash" / "chats").iterdir()] == [True]
    assert memory.live_records() == []
    assert _Containers.removed == [f"c-{chat}"]
    # A space another chat still works in stays.
    spaces = space.chat_space_ids([keeper])
    deleted = space.context_manager().delete_family(keeper)
    assert await space.release_chats(deleted, spaces) == []
    # The trash keeps a space a while, then lets it go.
    assert space.empty_trash() == 0
    assert space.empty_trash(older_than=0) == 1
    await space.close()


@pytest.mark.asyncio
async def test_a_deleted_projects_space_and_memory_go(user_space):
    space, root = user_space
    project = space.project_store.create("Atlas")
    chat = _new_chat(space, project.id)
    space.file_spaces(chat)
    await space.registry_for(chat)
    factory = space.registry.factory(space.registry.default_key())
    memory = factory.level_memory_manager("project", project.id, force=True)
    memory.remember(MemoryRecord("Atlas собирается make dist"))

    space.project_store.delete(project.id)
    await space.release_project(project.id)

    assert not (root / "projects" / project.id).exists()
    assert memory.live_records() == []
    assert _Containers.removed == [f"c-{chat}"]
    await space.close()


@pytest.mark.asyncio
async def test_no_turn_starts_once_the_users_files_pass_the_storage_limit(user_space, monkeypatch):
    import asyncio

    from web_chat.limits import TurnLimits

    space, _ = user_space
    limits = space.deployment.user_limits.model_copy(update={"storage_mb": 1})
    space.limits = TurnLimits("u1", lambda: limits, storage=space.storage_used)
    assert space.admit_turn() is None  # not measured yet: the measurement starts
    await asyncio.sleep(0.2)
    (space.workspace_path / "big.bin").write_bytes(b"x" * 2 * 1024 * 1024)
    space._storage_at = None
    space.storage_used()
    for _ in range(50):
        if not space._measuring:
            break
        await asyncio.sleep(0.05)
    refusal = space.admit_turn()
    assert refusal and "1 MB" in refusal


def test_chat_containers_left_by_an_earlier_run_are_removed_at_start(docker, tmp_path):
    manager, client = docker
    users = tmp_path / "users"
    chat = users / "u1" / "chats" / "ctx-1"
    chat.mkdir(parents=True)
    user = users / "u1" / "workspace"
    left = manager.get_or_create_container("u1", chat, mounts={"/user": user})
    kept = manager.get_or_create_container("u1", user)
    client.containers.list = lambda all=False, filters=None: [left, kept]

    assert manager.prune_chat_containers(users) == 1
    assert left.removed and not kept.removed


# -- no empty chats piling up ----------------------------------------------------------
def test_starting_a_space_makes_no_chat_and_deleting_the_open_one_makes_none(user_space, tmp_path, monkeypatch):
    space, _ = user_space
    assert space.context_manager().conversation_views() == []
    manager = space.context_manager()
    chat = manager.start_new_context()
    manager.delete_context(chat)
    assert manager.conversation_views() == []
    assert manager.get_current_context_id() is None


@pytest.mark.asyncio
async def test_old_empty_chats_go_and_everything_else_stays(user_space):
    from datetime import datetime, timedelta

    space, _ = user_space
    manager = space.context_manager()
    old = (datetime.now() - timedelta(days=2)).isoformat()

    def chat(messages=0, **metadata):
        context_id = manager.start_new_context()
        for index in range(messages):
            manager.append_message_to(context_id, "user", f"m{index}")
        if metadata:
            manager.update_context_metadata(context_id, metadata)
        manager._contexts[context_id]["updated_at"] = old
        return context_id

    abandoned = chat(created_by_web=True)
    made_by_server = manager.start_new_context()  # just now, but never anyone's
    written = chat(messages=1)
    with_files = _new_chat(space)
    own, _ = space.file_spaces(with_files)
    (own / "uploads").mkdir()
    (own / "uploads" / "data.csv").write_text("1")
    manager._contexts[with_files]["updated_at"] = old
    fresh = manager.start_new_context()  # empty and new: a "New chat" being opened
    manager.update_context_metadata(fresh, {"created_by_web": True})
    busy = chat()
    space.turns.claim(busy)

    assert sorted(await space.prune_empty_chats()) == sorted([abandoned, made_by_server])
    remaining = {view["id"] for view in manager.conversation_views()}
    assert remaining == {written, with_files, fresh, busy}
