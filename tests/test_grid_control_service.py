from fastapi.testclient import TestClient

from grid_control.controller import Controller
from grid_control.models import Check, Policy, Trial
from grid_control.service import create_app
from grid_control.store import Store


class Runtime:
    def revision(self, repo, ref):
        return ref

    def image(self, ref):
        return ref

    def build(self, *args):
        return "image"

    def trial(self, *args):
        return Trial({"web": True}, 0.1)

    def cleanup(self, *args):
        pass


def test_api_only_accepts_commits_and_keeps_host_paths_private(tmp_path):
    store = Store(tmp_path / "control.db")
    controller = Controller(store, Runtime())
    policy = Policy("runtime", "verifier", (Check("web", "/"),), repetitions=1)
    app = create_app(controller, tmp_path, policy, "t" * 32)
    auth = {"Authorization": "Bearer " + "t" * 32}
    body = {"baseline": "a" * 40, "candidate": "b" * 40}
    with TestClient(app) as client:
        assert client.post("/experiments", json=body).status_code == 401
        assert (
            client.post(
                "/experiments", headers=auth, json={**body, "command": "malicious"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/experiments", headers=auth, json={**body, "candidate": "--flag"}
            ).status_code
            == 422
        )
        response = client.post("/experiments", headers=auth, json=body)
        assert response.status_code == 202
        experiment = response.json()["id"]
        report = client.get("/experiments/" + experiment, headers=auth).json()
        assert "repository" not in report
        assert "policy" not in report
    # Service drains its accepted queue on orderly shutdown.
    assert store.get(experiment)["status"] == "accepted"


def test_restart_replays_queued_work_and_fails_interrupted_work(tmp_path):
    store = Store(tmp_path / "control.db")
    controller = Controller(store, Runtime())
    policy = Policy("runtime", "verifier", (Check("web", "/"),), repetitions=1)
    queued = controller.prepare(tmp_path, "a" * 40, "b" * 40, policy)
    interrupted = controller.prepare(tmp_path, "a" * 40, "c" * 40, policy)
    store.start(interrupted)
    with TestClient(create_app(controller, tmp_path, policy, "t" * 32)):
        pass
    assert store.get(queued)["status"] == "accepted"
    assert store.get(interrupted)["status"] == "failed"


WORKSHOP = {"Authorization": "Bearer " + "t" * 32}
OPERATOR = {"Authorization": "Bearer " + "o" * 32}
TASK = {"title": "Say which directory", "summary": "The engineer guessed.", "cause": "prompt",
        "change": "--- a/x\n+++ b/x\n", "source": {"review": "r1"}}


def _task_app(tmp_path, task_token="o" * 32):
    store = Store(tmp_path / "control.db")
    policy = Policy("runtime", "verifier", (Check("web", "/"),), repetitions=1)
    return store, create_app(Controller(store, Runtime()), tmp_path, policy, "t" * 32, task_token)


def test_the_operator_sends_a_task_the_workshop_takes_and_links(tmp_path):
    store, app = _task_app(tmp_path)
    with TestClient(app) as client:
        sent = client.post("/tasks", headers=OPERATOR, json=TASK)
        task = sent.json()["id"]
        waiting = client.get("/tasks", headers=WORKSHOP).json()
        taken = client.post(f"/tasks/{task}/take", headers=WORKSHOP)
        again = client.post(f"/tasks/{task}/take", headers=WORKSHOP)
        experiment = client.post(
            "/experiments", headers=WORKSHOP, json={"baseline": "a" * 40, "candidate": "b" * 40}
        ).json()["id"]
        linked = client.post(f"/tasks/{task}/link", headers=WORKSHOP, json={"experiment": experiment})
        status = client.get(f"/tasks/{task}", headers=OPERATOR).json()

    assert sent.status_code == 201
    assert [item["title"] for item in waiting] == ["Say which directory"]
    assert taken.json()["change"] == TASK["change"] and again.status_code == 409
    assert linked.status_code == 200
    assert status["status"] == "taken" and status["experiment"] == experiment and status["verdict"] is not None
    assert store.open_tasks() == []


def test_each_token_opens_only_its_own_routes(tmp_path):
    _, app = _task_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/tasks", headers=WORKSHOP, json=TASK).status_code == 401
        assert client.get("/tasks", headers=OPERATOR).status_code == 401
        assert client.get("/source", headers=OPERATOR).status_code == 401
        assert client.post("/tasks", headers=OPERATOR, json={**TASK, "command": "rm"}).status_code == 422


def test_without_a_task_token_there_is_no_way_to_send_tasks(tmp_path):
    _, app = _task_app(tmp_path, task_token=None)
    with TestClient(app) as client:
        assert client.post("/tasks", headers=OPERATOR, json=TASK).status_code in (401, 405)


def test_the_task_token_is_not_the_workshops(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        _task_app(tmp_path, task_token="t" * 32)
