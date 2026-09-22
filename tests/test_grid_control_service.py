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
