"""Workshop ↔ controller round trip on real git repositories, without Docker."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from core.workshop import ControlClient, Workshop, WorkshopError
from grid_control.controller import Controller
from grid_control.models import Check, Policy, Trial
from grid_control.repository import Repository
from grid_control.service import create_app
from grid_control.store import Store

TOKEN = "t" * 32


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@local", *args],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


class PassingRuntime:
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


def _policy(**changes):
    return Policy("runtime", "verifier", (Check("web", "/"),), repetitions=1, **changes)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "grid"
    path.mkdir()
    _git(path, "init", "--quiet")
    (path / "routing.yaml").write_text("routing: {}\n", encoding="utf-8")
    _git(path, "add", "--all")
    _git(path, "commit", "--quiet", "-m", "initial")
    return path


@pytest.fixture
def control(tmp_path, source):
    """Evolution repository, journal and a factory for a running controller API."""
    repository = Repository.create(tmp_path / "evolution.git", source)
    store = Store(tmp_path / "control.db")

    def serve(policy):
        app = create_app(Controller(store, PassingRuntime()), repository.path, policy, TOKEN)
        return TestClient(app, headers={"Authorization": "Bearer " + TOKEN})

    return repository, store, serve


def test_candidate_travels_from_workshop_to_stable(tmp_path, control):
    repository, store, serve = control
    workshop = Workshop(tmp_path / "workshop")
    with serve(_policy()) as http:
        client = ControlClient(http)
        baseline = workshop.init(client)
        assert baseline == repository.stable()
        assert workshop.is_workshop()

        (workshop.path / "notes.md").write_text("better prompt\n", encoding="utf-8")
        report = workshop.submit(client, "Add notes to test the round trip")
    # Leaving the client drains the controller's queue.
    experiment = store.get(report["id"])
    assert experiment["status"] == "accepted"
    assert (experiment["baseline"], experiment["candidate"]) == (baseline, report["candidate"])
    assert repository.stable() == baseline, "acceptance alone must not move stable"

    Controller(store, PassingRuntime()).promote(report["id"])
    assert repository.stable() == report["candidate"]
    assert store.get(report["id"])["status"] == "promoted"

    with serve(_policy()) as http:
        assert workshop.begin(ControlClient(http)) == report["candidate"]


def test_auto_promote_policy_moves_stable_without_operator(tmp_path, control):
    repository, store, serve = control
    workshop = Workshop(tmp_path / "workshop")
    with serve(_policy(auto_promote=True)) as http:
        client = ControlClient(http)
        workshop.init(client)
        (workshop.path / "notes.md").write_text("x\n", encoding="utf-8")
        report = workshop.submit(client, "Change promoted by policy")
    assert store.get(report["id"])["status"] == "promoted"
    assert repository.stable() == report["candidate"]


def test_promotion_refuses_a_candidate_evaluated_against_old_stable(tmp_path, control):
    repository, store, serve = control
    first, second = Workshop(tmp_path / "one"), Workshop(tmp_path / "two")
    with serve(_policy()) as http:
        client = ControlClient(http)
        for workshop, name in ((first, "a.md"), (second, "b.md")):
            workshop.init(client)
            (workshop.path / name).write_text("x\n", encoding="utf-8")
        reports = [w.submit(client, "Parallel change " + w.path.name) for w in (first, second)]
    controller = Controller(store, PassingRuntime())
    controller.promote(reports[0]["id"])

    with pytest.raises(ValueError, match="Stable moved"):
        controller.promote(reports[1]["id"])
    assert repository.stable() == reports[0]["candidate"]
    assert store.get(reports[1]["id"])["status"] == "accepted"


def test_working_copy_is_never_treated_as_a_workshop(source, control):
    _, _, serve = control
    working_copy = Workshop(source)
    (source / "local.txt").write_text("user work\n", encoding="utf-8")
    head = _git(source, "rev-parse", "HEAD")
    with serve(_policy()) as http:
        client = ControlClient(http)
        for action in (working_copy.begin, lambda c: working_copy.submit(c, "Should not happen")):
            with pytest.raises(WorkshopError, match="not a workshop"):
                action(client)
    assert _git(source, "rev-parse", "HEAD") == head
    assert (source / "local.txt").read_text(encoding="utf-8") == "user work\n"


def test_workshop_guards_its_own_experiment(tmp_path, control):
    _, _, serve = control
    workshop = Workshop(tmp_path / "workshop")
    with serve(_policy()) as http:
        client = ControlClient(http)
        workshop.init(client)
        with pytest.raises(WorkshopError, match="no changes"):
            workshop.submit(client, "Nothing changed here")
        (workshop.path / "draft.md").write_text("x\n", encoding="utf-8")
        with pytest.raises(WorkshopError, match="uncommitted changes"):
            workshop.begin(client)
        with pytest.raises(WorkshopError, match="Describe the change"):
            workshop.submit(client, "fix")


def test_controller_rejects_bundles_that_do_not_match_the_submission(tmp_path, control):
    repository, _, serve = control
    workshop = Workshop(tmp_path / "workshop")
    with serve(_policy()) as http:
        client = ControlClient(http)
        baseline = workshop.init(client)
        (workshop.path / "a.md").write_text("x\n", encoding="utf-8")
        _git(workshop.path, "add", "--all")
        _git(workshop.path, "commit", "--quiet", "-m", "candidate")
        candidate = _git(workshop.path, "rev-parse", "HEAD")
        bundle = workshop.git("bundle", "create", "-", "refs/heads/experiment", f"^{baseline}")

        with pytest.raises(WorkshopError, match="declared candidate"):
            client.submit(baseline, "f" * 40, bundle)
        with pytest.raises(WorkshopError, match="Baseline is not a commit"):
            client.submit(candidate, candidate, bundle)
    assert not _git(repository.path, "for-each-ref", "refs/heads/").count("\n")


async def test_control_tools_refuse_without_a_configured_controller(monkeypatch, tmp_path):
    from tools.control_tools import control_begin

    monkeypatch.delenv("GRID_CONTROL_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    result = await control_begin.on_invoke_tool(None, "{}")
    assert "GRID_CONTROL_URL" in result["error"]


def test_runtime_files_never_reach_a_candidate(tmp_path, control):
    """The running administrator writes its trace database into the workshop."""
    repository, store, serve = control
    workshop = Workshop(tmp_path / "workshop")
    with serve(_policy()) as http:
        client = ControlClient(http)
        workshop.init(client)
        (workshop.path / ".grid").mkdir()
        (workshop.path / ".grid" / "timeline.db-wal").write_bytes(b"\x82\x00")
        (workshop.path / "logs").mkdir()
        (workshop.path / "logs" / "context.json").write_text("{}", encoding="utf-8")
        with pytest.raises(WorkshopError, match="no changes"):
            workshop.submit(client, "Only runtime files changed")
        (workshop.path / "routing.yaml").write_text("routing: {x: 1}\n", encoding="utf-8")
        report = workshop.submit(client, "Change routing only")
    assert report["files"] == [{"path": "routing.yaml", "change": "modified"}]
    committed = _git(workshop.path, "show", "--name-only", "--format=", report["candidate"])
    assert committed.split() == ["routing.yaml"]


def test_review_covers_the_whole_experiment_and_revert_restores_baseline(tmp_path, control):
    repository, store, serve = control
    workshop = Workshop(tmp_path / "workshop")
    with serve(_policy()) as http:
        client = ControlClient(http)
        workshop.init(client)
        (workshop.path / "routing.yaml").write_text("routing: {a: 1}\n", encoding="utf-8")
        workshop.submit(client, "First candidate of the experiment")
        # Second round: a stray scratch file and a damaged committed file.
        (workshop.path / "_test_patch.txt").write_text("alpha\n", encoding="utf-8")
        (workshop.path / "examples").mkdir()
        (workshop.path / "examples" / "new.yaml").write_text("x: 1\n", encoding="utf-8")

        review = workshop.diff()
        assert {f["path"]: f["change"] for f in review["files"]} == {
            "routing.yaml": "modified", "_test_patch.txt": "added", "examples/new.yaml": "added"}
        assert "+routing: {a: 1}" in review["diff"], "diff is against the baseline, not HEAD"
        assert set(workshop.diff("routing.yaml")["diff"].split("diff --git")[1:]).__len__() == 1

        left = workshop.revert(["_test_patch.txt", "routing.yaml"])
        assert [f["path"] for f in left["files"]] == ["examples/new.yaml"]
        assert not (workshop.path / "_test_patch.txt").exists()
        assert (workshop.path / "routing.yaml").read_text(encoding="utf-8") == "routing: {}\n"
        report = workshop.submit(client, "Second candidate without the stray files")
    assert [f["path"] for f in report["files"]] == ["examples/new.yaml"]


def test_revert_refuses_paths_outside_the_repository(tmp_path, control):
    repository, store, serve = control
    workshop = Workshop(tmp_path / "workshop")
    with serve(_policy()) as http:
        workshop.init(ControlClient(http))
    for path in ("../outside", ".git/config", "."):
        with pytest.raises(WorkshopError):
            workshop.revert([path])
