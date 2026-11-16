"""The tracker page's API (web_chat/tasks_api.py): its routes over bd, and the
scope each route acts in - the asking user's own workspace, never another's.

bd itself is stubbed at the one seam every caller shares
(``tools.beads_tools._run_bd_command``): the reads are answered with canned
issues, the writes by a small stateful tracker, so the routes' own behaviour is
what these tests see - the commands they shape, the answers they make of bd's,
the journal and ownership a write is reported from, and the auth/scope they
hold. One test runs a real bd when the host has one.
"""

import json
import shutil
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import web_chat.tasks_api as tasks_api
from core import tracker_lifecycle
from tools import beads_tools
from web_chat.tasks_api import register_task_routes

_patch = None


@pytest.fixture(autouse=True)
def _stub_host_bd(monkeypatch):
    """Every test runs on a host where bd runs; a test's own stub replaces it."""
    global _patch
    _patch = monkeypatch
    monkeypatch.setattr(tasks_api, "_bd_problem", lambda: None)


def issue(task_id, *, status="open", **extra):
    return {"id": task_id, "title": f"Task {task_id}", "status": status, **extra}


def ok(data=None, output=None):
    return {
        "success": True,
        "output": output if output is not None else json.dumps(data if data is not None else ""),
        "error": "",
        "data": data,
        "exit_code": 0,
    }


class Bd:
    """A stand-in for ``_run_bd_command``: canned answers, and what was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, args, cwd=None, **kwargs):
        self.calls.append((list(args), cwd))
        return self.answer(args, cwd) if callable(self.answer) else self.answer


def flag(args, name):
    prefix = f"--{name}="
    for arg in args:
        if isinstance(arg, str) and arg.startswith(prefix):
            return arg[len(prefix):]
    if f"--{name}" in args:
        index = args.index(f"--{name}")
        if index + 1 < len(args):
            return args[index + 1]
    return None


class Tracker:
    """A stateful stand-in for bd: the commands the page's writes run.

    The real commands are bd's; what matters here is that a task is created,
    shown, updated with its metadata, closed with its marked reason and given
    an edge - so the journal, the markers and the read-back the routes rely on
    are exercised against something that keeps state.
    """

    def __init__(self, *, lose_create_answer_once=False, refuse=None):
        self.tasks = {}
        self.calls = []
        self.refuse = dict(refuse or {})
        self.lose_create_answer_once = lose_create_answer_once

    # -- what a task looks like ----------------------------------------------
    def ensure(self, task_id):
        if task_id not in self.tasks:
            self.tasks[task_id] = issue(task_id)
        return self.tasks[task_id]

    # -- the command dispatch -------------------------------------------------
    def __call__(self, args, cwd=None, **kwargs):
        args = list(args)
        self.calls.append((args, cwd))
        command = args[0] if args else ""
        if command in self.refuse:
            return self.refuse[command]
        return getattr(self, f"_{command}", self._plain)(args)

    def _plain(self, args):
        return ok(output="")

    def _init(self, args):
        return ok(output="initialized")

    def _create(self, args):
        title = args[args.index("--") + 1] if "--" in args else ""
        deps = [dep for dep in (flag(args, "deps") or "").split(",") if dep]
        task_id = f"p-{len(self.tasks) + 1}"
        self.tasks[task_id] = {
            "id": task_id,
            "title": title,
            "status": "open",
            "priority": int(flag(args, "priority") or 1),
            "issue_type": flag(args, "type") or "task",
            "description": flag(args, "description") or "",
            "acceptance_criteria": flag(args, "acceptance") or "",
            "assignee": flag(args, "assignee"),
            "external_ref": flag(args, "external-ref"),
            "dependencies": [{"type": "blocks", "depends_on_id": dep} for dep in deps],
        }
        if self.lose_create_answer_once:
            # The write reached the tracker; the answer did not come back.
            self.lose_create_answer_once = False
            return {"success": False, "output": "", "error": "", "data": None, "timed_out": True}
        return ok(dict(self.tasks[task_id]))

    def _list(self, args):
        reference = flag(args, "external-ref")
        tasks = list(self.tasks.values())
        if reference is not None:
            tasks = [task for task in tasks if task.get("external_ref") == reference]
        return ok([dict(task) for task in tasks])

    def _show(self, args):
        return ok([dict(self.ensure(args[1]))])

    def _update(self, args):
        task = self.ensure(args[1])
        metadata = dict(task.get("metadata") or {})
        for arg in args:
            if isinstance(arg, str) and arg.startswith("--set-metadata="):
                key, _, value = arg[len("--set-metadata="):].partition("=")
                metadata[key] = value
        if "--claim" in args:
            task["status"] = "in_progress"
            task["assignee"] = metadata.get("grid_run") or "grid"
        if flag(args, "status"):
            task["status"] = flag(args, "status")
        for name, field in (
            ("title", "title"), ("description", "description"),
            ("acceptance", "acceptance_criteria"), ("type", "issue_type"), ("assignee", "assignee"),
        ):
            if flag(args, name) is not None:
                task[field] = flag(args, name)
        if flag(args, "priority") is not None:
            task["priority"] = int(flag(args, "priority"))
        if flag(args, "notes"):
            task["notes"] = flag(args, "notes")
        task["metadata"] = metadata
        return ok(dict(task))

    def _close(self, args):
        task = self.ensure(args[1])
        task["status"] = "closed"
        task["close_reason"] = flag(args, "reason") or ""
        return ok(dict(task), output=json.dumps(dict(task)))

    def _dep(self, args):
        action, child, parent = args[1], args[2], args[3]
        task = self.ensure(child)
        deps = list(task.get("dependencies") or [])
        if action == "add" and not any(dep.get("depends_on_id") == parent for dep in deps):
            deps.append({"type": "blocks", "depends_on_id": parent, "title": f"Task {parent}"})
        if action == "remove":
            deps = [dep for dep in deps if dep.get("depends_on_id") != parent]
        task["dependencies"] = deps
        return ok(output="ok")


def make_app(bd, *, workspace="/tmp/anon", require_user=False, space=None):
    """An app with the tracker routes over *bd*, and a scoped current_space."""
    _patch.setattr(beads_tools, "_run_bd_command", bd)
    app = FastAPI()
    api = APIRouter()

    def current_space(request: Request):
        user = request.headers.get("x-user")
        if require_user and user is None:
            raise HTTPException(status_code=401, detail="Sign in")
        if space is not None:
            return space
        return SimpleNamespace(workspace_path=f"/tmp/{user or 'anon'}" if workspace == "/tmp/anon" else workspace)

    register_task_routes(api, current_space)
    app.include_router(api)
    return app


# -- reads --------------------------------------------------------------------


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


def test_show_answers_404_when_bd_does_not_find_the_task():
    bd = Bd({"success": False, "output": "issue not found", "error": ""})
    client = TestClient(make_app(bd))
    assert client.get("/api/tasks/p-1").status_code == 404


def test_a_bad_task_id_never_reaches_bd():
    bd = Bd({"success": True, "data": []})
    client = TestClient(make_app(bd))
    assert client.get("/api/tasks/%20..%2f").status_code in (400, 404)
    assert client.post("/api/tasks/not valid/close", json={"reason": "x"}).status_code == 400
    # An id that would read as a flag never reaches bd either.
    assert client.post("/api/tasks/--db/close", json={"reason": "x"}).status_code == 400
    assert bd.calls == []


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
    _patch.setattr(tasks_api, "_bd_problem", lambda: "bd is not installed")
    response = TestClient(app).get("/api/tasks")
    assert response.status_code == 503 and "unavailable" in response.json()["detail"]
    assert bd.calls == []  # bd was never asked to run


def test_list_names_the_chats_whose_plans_hold_each_task():
    """R5: the global board still points back at the chat that planned a task."""
    plan_task = {"id": "p-1", "key": "k", "title": "Task p-1"}

    class Manager:
        def conversation_views(self):
            return [{"id": "c-1"}, {"id": "c-2"}]

        def get_context_messages(self, context_id, limit=40):
            if context_id == "c-1":
                return {"messages": [{"metadata": {"trace": [{"kind": "plan", "plan": {"tasks": [plan_task]}}]}}]}
            return {"messages": []}

    space = SimpleNamespace(workspace_path="/tmp/anon", context_manager=lambda: Manager())
    bd = Bd({"success": True, "data": [issue("p-1")]})
    body = TestClient(make_app(bd, space=space)).get("/api/tasks").json()
    assert body["tasks"][0]["chat_ids"] == ["c-1"]


def test_a_space_without_a_plan_route_still_lists_tasks_without_chat_ids():
    """R5 is best-effort: a space that cannot answer views never fails the list."""
    space = SimpleNamespace(workspace_path="/tmp/anon")  # no context_manager at all
    bd = Bd({"success": True, "data": [issue("p-1")]})
    body = TestClient(make_app(bd, space=space)).get("/api/tasks").json()
    # No source chat is named; the list still answers, just without a chip back.
    assert body["tasks"][0].get("chat_ids") in (None, [])


def test_a_tasks_report_travels_from_the_source_to_the_page():
    """R6: what the run that took the task reported reaches the tracker."""
    report = {"status": "completed", "final": "Done.", "seconds": 3}
    bd = Bd(lambda args, cwd: {"success": True, "data": [issue("p-1", report=report), issue("p-2")]})
    body = TestClient(make_app(bd)).get("/api/tasks").json()

    assert body["tasks"][0]["report"] == report
    # An issue with no report carries None, never a made-up one.
    assert body["tasks"][1]["report"] is None


# -- writes: the shared durable service ---------------------------------------


def test_create_runs_the_journaled_service_and_shapes_the_command(tmp_path):
    tracker = Tracker()
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    response = client.post("/api/tasks", json={
        "title": "Fix the build", "priority": 0, "type": "bug",
        "description": "make it green", "acceptance": "it is green",
        "depends_on": ["p-1", "bad id!"],
    })

    assert response.status_code == 201
    created = response.json()
    assert created["title"] == "Fix the build" and created["description"] == "make it green"
    create = next(args for args, _ in tracker.calls if args[0] == "create")
    # The title is the one positional argument: after "--", a title such as
    # "--db=..." is a title, never a flag.
    assert create[-2:] == ["--", "Fix the build"]
    assert "--priority=0" in create and "--type=bug" in create and "--description=make it green" in create
    assert "--acceptance=it is green" in create
    # Only real ids become dependencies; an invalid one never reaches bd.
    assert "--deps=p-1" in create and "bad id!" not in " ".join(create)
    # The marker rides the same bd command as the task: a lost answer is
    # proven by reading back this operation's own task.
    assert flag(create, "external-ref").startswith("grid-op:")


def test_a_title_or_value_that_looks_like_a_flag_stays_a_value(tmp_path):
    tracker = Tracker()
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    assert client.post("/api/tasks", json={"title": "--db=/elsewhere", "description": "-x"}).status_code == 201
    create = next(args for args, _ in tracker.calls if args[0] == "create")
    assert create[-2:] == ["--", "--db=/elsewhere"] and "--description=-x" in create
    assert "--db=/elsewhere" not in create[:-1]


def test_a_repeated_create_answers_the_first_task(tmp_path):
    tracker = Tracker()
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    arguments = {"title": "Twice"}
    first = client.post("/api/tasks", json={**arguments, "request_id": "act-1"})
    second = client.post("/api/tasks", json={**arguments, "request_id": "act-1"})

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert [args[0] for args, _ in tracker.calls].count("create") == 1


def test_a_repeated_create_by_header_answers_the_first_task(tmp_path):
    tracker = Tracker()
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    headers = {"Idempotency-Key": "act-header"}
    first = client.post("/api/tasks", json={"title": "By header"}, headers=headers)
    second = client.post("/api/tasks", json={"title": "By header"}, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert [args[0] for args, _ in tracker.calls].count("create") == 1


def test_a_lost_create_answer_is_answered_on_repeat_without_a_second_task(tmp_path):
    tracker = Tracker(lose_create_answer_once=True)
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    arguments = {"title": "Lost answer", "request_id": "act-lost"}
    first = client.post("/api/tasks", json=arguments)
    second = client.post("/api/tasks", json=arguments)

    # The write reached the tracker; the page still gets the task it made.
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(tracker.tasks) == 1
    assert [args[0] for args, _ in tracker.calls].count("create") == 1


def test_a_refused_write_is_400_with_bds_reason(tmp_path):
    tracker = Tracker(refuse={"create": {
        "success": False, "output": "", "error": "cycle detected", "code": "tracker_rejected",
    }})
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    response = client.post("/api/tasks", json={"title": "X"})
    body = response.json()
    # A typed refusal: the page decides on isError/committed, not on the text.
    assert response.status_code == 400
    assert body["isError"] is True and body["committed"] is False
    assert body["code"] == "tracker_rejected" and "cycle detected" in body["reason"]


def test_update_with_nothing_to_change_is_400(tmp_path):
    tracker = Tracker()
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    assert client.patch("/api/tasks/p-1", json={}).status_code == 400
    assert tracker.calls == []  # nothing was asked of bd for an empty patch


def test_update_patches_the_task_and_answers_what_the_tracker_holds(tmp_path):
    tracker = Tracker()
    tracker.ensure("p-1")
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    body = client.patch("/api/tasks/p-1", json={"notes": "now noted"}).json()
    assert body["id"] == "p-1" and body["notes"] == "now noted"
    update = next(args for args, _ in tracker.calls if args[0] == "update")
    assert update[1] == "p-1" and "--notes=now noted" in update
    assert any(arg.startswith("--set-metadata=grid_op=") for arg in update)


def test_close_carries_the_reason_and_reports_it(tmp_path):
    tracker = Tracker()
    tracker.ensure("p-1")
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    body = client.post("/api/tasks/p-1/close", json={"reason": "listed"}).json()
    assert body == {
        "ok": True, "isError": False, "committed": True, "id": "p-1", "close_reason": "listed",
    }
    close = next(args for args, _ in tracker.calls if args[0] == "close")
    # The reason carries the operation's marker: a lost answer is proven by it.
    assert close[:2] == ["close", "p-1"]
    assert flag(close, "reason").startswith("listed [grid-op:")


def test_a_close_the_tracker_does_not_show_is_not_a_receipt(tmp_path):
    tracker = Tracker()
    tracker.ensure("p-1")
    # bd accepts the close, but the task keeps reading back open.
    tracker.tasks["p-1"]["status"] = "open"
    original = tracker._close
    tracker._close = lambda args: ok(output="closed")
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    response = client.post("/api/tasks/p-1/close", json={"reason": "listed"})
    tracker._close = original
    assert response.status_code == 202
    assert response.json()["committed"] is None and response.json()["code"] == "tracker_unconfirmed"


def test_a_claim_is_a_journaled_short_action_with_a_typed_receipt(tmp_path):
    tracker = Tracker()
    tracker.ensure("p-1")
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    body = client.post("/api/tasks/p-1/claim").json()

    assert body["ok"] is True and body["committed"] is True
    receipt = body["receipt"]
    assert receipt["tool"] == "beads_update" and receipt["isError"] is False
    assert receipt["bead_id"] == "p-1" and receipt["status"] == "in_progress"
    # The page's claim is not a run: it holds no lease after the answer.
    assert tracker_lifecycle.lookup(str(tmp_path), "p-1") is None


def test_a_task_a_live_run_holds_is_not_claimable_by_the_page(tmp_path):
    tracker = Tracker()
    tracker.ensure("p-1")
    tracker_lifecycle.bind(str(tmp_path), "p-1", run_id="run-of-a-chat", actor="grid:alice",
                           agent="Executor", context_id="c-1")
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    response = client.post("/api/tasks/p-1/claim")
    body = response.json()

    assert response.status_code == 400
    assert body["isError"] is True and body["committed"] is False
    assert body["code"] in ("tracker_claimed_elsewhere", "tracker_not_owner")
    # The live owner's task was not touched: no update reached bd at all.
    assert all(args[0] != "update" for args, _ in tracker.calls)
    view = tracker_lifecycle.lookup(str(tmp_path), "p-1")
    assert view["owner_run_id"] == "run-of-a-chat"


def test_a_dependency_add_names_the_child_and_the_parent(tmp_path):
    tracker = Tracker()
    tracker.ensure("p-2")
    tracker.ensure("p-1")
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    body = client.post("/api/tasks/p-2/deps", json={"action": "add", "parent_id": "p-1"}).json()
    assert body["ok"] is True and body["committed"] is True
    # The graph is read first, then the write is confirmed by a read-back.
    assert any(args[:4] == ["dep", "add", "p-2", "p-1"] for args, _ in tracker.calls)
    assert tracker.calls[-1][0][:2] == ["show", "p-2"]
    assert tracker.calls[0][0][:2] == ["list", "--limit"]


def test_a_dependency_that_would_close_a_ring_is_refused_with_its_chain(tmp_path):
    def answer(args, cwd):
        if args[0] == "list":
            # p-1 waits for p-3, which waits for p-2: p-2 → p-1 would close the ring.
            return {
                "success": True,
                "data": [
                    issue("p-1", dependencies=[{"type": "blocks", "depends_on_id": "p-3", "title": "Third"}]),
                    issue("p-3", dependencies=[{"type": "blocks", "depends_on_id": "p-2", "title": "Second"}]),
                    issue("p-2"),
                ],
            }
        return {"success": True, "output": "ok"}

    bd = Bd(answer)
    client = TestClient(make_app(bd, workspace=str(tmp_path)))
    response = client.post("/api/tasks/p-2/deps", json={"action": "add", "parent_id": "p-1"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "cycle" in detail and "Task p-2" in detail and "Task p-1" in detail
    # bd's own dep was never asked: the ring never reached the tracker.
    assert all(call[0][0] != "dep" for call in bd.calls)


def test_a_task_cannot_depend_on_itself():
    bd = Bd({"success": True, "data": []})
    client = TestClient(make_app(bd))
    response = client.post("/api/tasks/p-1/deps", json={"action": "add", "parent_id": "p-1"})
    assert response.status_code == 400 and "itself" in response.json()["detail"]
    assert bd.calls == []  # nothing was asked of bd


def test_removing_a_dependency_never_reads_the_graph_or_checks_rings(tmp_path):
    tracker = Tracker()
    tracker.ensure("p-2")
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    response = client.post("/api/tasks/p-2/deps", json={"action": "remove", "parent_id": "p-1"})
    assert response.status_code == 200
    assert [args[0] for args, _ in tracker.calls] == ["dep", "show"]


def test_a_child_task_id_is_a_task_id(tmp_path):
    tracker = Tracker()
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    assert client.post("/api/tasks/p-1.2/close", json={"reason": "x"}).status_code == 200


# -- projects: an unknown one is refused before anything is written -----------


def test_an_unknown_project_is_refused_without_a_single_write(tmp_path):
    tracker = Tracker()
    client = TestClient(make_app(tracker, workspace=str(tmp_path)))
    response = client.post("/api/tasks", json={"title": "Lost", "project_id": "prj-ffffffff-x"})
    assert response.status_code == 404 and "Unknown project" in response.json()["detail"]
    assert tracker.calls == []
    # A read that names the same project is refused too: it never opens the
    # user's own tracker in its place.
    assert client.get("/api/tasks?project=prj-ffffffff-x").status_code == 404
    assert tracker.calls == []


def test_a_task_of_a_known_project_lands_in_that_projects_tracker(tmp_path):
    project = tmp_path / "projects" / "prj-1234abcd-x"
    (project / ".beads").mkdir(parents=True)
    space = SimpleNamespace(
        workspace_path=str(tmp_path),
        chat_spaces=True,
        project_exists=lambda project_id: project_id == "prj-1234abcd-x",
        tracker_dir=lambda project_id=None: project if project_id else tmp_path,
        project_trackers=lambda: {},
    )
    tracker = Tracker()
    response = TestClient(make_app(tracker, space=space)).post(
        "/api/tasks", json={"title": "In the project", "project_id": "prj-1234abcd-x"}
    )
    assert response.status_code == 201 and response.json()["project_id"] == "prj-1234abcd-x"
    create = next(call for call in tracker.calls if call[0][0] == "create")
    assert create[1] == str(project)


# -- against a real tracker ---------------------------------------------------


def test_a_real_tracker_creates_one_task_for_a_repeated_request(tmp_path, monkeypatch):
    if shutil.which("bd") is None:
        pytest.skip("bd is not installed")
    monkeypatch.chdir(tmp_path)
    space = SimpleNamespace(workspace_path=str(tmp_path))
    client = TestClient(make_app(beads_tools._run_bd_command, space=space))
    arguments = {"title": "Real task", "request_id": "real-ui-1", "acceptance": "it is there"}

    first = client.post("/api/tasks", json=arguments)
    second = client.post("/api/tasks", json=arguments)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    listing = beads_tools._run_bd_command(
        ["list", "--json", "--limit", "0", "--all"], cwd=str(tmp_path)
    )
    [task] = listing["data"]
    assert task["id"] == first.json()["id"] and task["title"] == "Real task"
