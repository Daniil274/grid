"""Acceptance evidence must check artifacts, survive lifecycle changes and reject stale revisions."""
import json
from types import SimpleNamespace

import pytest
import yaml

from core.system_quality import (Assertion, QualityContract, Scenario, check_assertion,
                                 design_review, quality_summary, record_tool_test,
                                 require_quality, revision, save_evidence)
from tests.test_system_builder import access_for, call, private_config
from tests import test_system_builder as builder_tests
from tests.conftest import link


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    return builder_tests.deployment.__wrapped__(tmp_path, monkeypatch)


def contract_document():
    return {
        "goal": "Count words", "result": "result.json", "acceptance": "Exact count", "rationale": "One task",
        "roles": [{"agent": "main", "purpose": "Count", "inputs": "text", "outputs": "JSON",
                   "acceptance": "Exact count", "failure": "Report missing file"}],
        "examples": ["Count input.txt"], "failure_behavior": "Report missing file",
        "scenarios": [{"name": "count", "message": "Count input.txt", "fixtures": {"input.txt": "one two"},
                       "assertions": [{"kind": "json_equals", "path": "result.json", "value": {"words": 2}}]}],
    }


def create_system(deployment, key="stats"):
    access = access_for(deployment)
    from core.system_store import SystemManifest
    access.store.create(SystemManifest(key=key, name="Stats", description="Counts words"), private_config())
    root = access.store.directory(key)
    (root / "README.md").write_text("Count words; input.txt -> result.json. Missing files are reported.")
    (root / "quality.yaml").write_text(yaml.safe_dump(contract_document()))
    return access, root


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../secret", "C:/secret", "a\\secret", "."])
def test_contract_refuses_escaping_paths(path):
    with pytest.raises(ValueError):
        Scenario(name="bad", message="read", fixtures={path: "secret"}, assertions=[{"kind": "contains", "value": "ok"}])
    with pytest.raises(ValueError):
        Assertion(kind="exists", path=path)


def test_contract_rejects_vacuous_or_unbounded_checks():
    document = contract_document()
    document["scenarios"][0]["assertions"] = []
    with pytest.raises(ValueError):
        QualityContract.model_validate(document)
    with pytest.raises(ValueError):
        Assertion(kind="contains", value="")
    document = contract_document()
    document["repetitions"] = 100
    with pytest.raises(ValueError):
        QualityContract.model_validate(document)


def test_multiagent_contract_requires_distinct_reachable_agents(deployment):
    _, root = create_system(deployment)
    document = contract_document()
    document["architecture"] = "coordinator"
    contract = QualityContract.model_validate(document)
    assert "A multi-agent architecture requires distinct configured agents" in design_review(root, contract)["errors"]
    config = yaml.safe_load((root / "config.yaml").read_text())
    config["agents"]["reviewer"] = dict(config["agents"]["main"])
    document["roles"].append({**document["roles"][0], "agent": "reviewer"})
    contract = QualityContract.model_validate(document)
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    assert any("unreachable" in error for error in design_review(root, contract)["errors"])
    config.setdefault("tools", {})["review"] = {"type": "agent", "target_agent": "reviewer", "context_strategy": "minimal"}
    config["agents"]["main"].setdefault("tools", []).append("review")
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    assert not design_review(root, contract)["errors"]


def test_artifact_checks_inspect_values_and_refuse_links(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out.json").write_text('{"words": 2}')
    assert check_assertion(Assertion(kind="json_equals", path="out.json", value={"words": 2}), "", workspace)["passed"]
    assert not check_assertion(Assertion(kind="json_equals", path="out.json", value={"words": 3}), "", workspace)["passed"]
    (workspace / "out.csv").write_text("name,count\na,2\n")
    assert check_assertion(Assertion(kind="csv_equals", path="out.csv", value=[{"name": "a", "count": "2"}]), "", workspace)["passed"]
    (tmp_path / "secret").mkdir()
    link(workspace / "secret", tmp_path / "secret", directory=True)
    for kind in ("exists", "absent"):
        assert not check_assertion(Assertion(kind=kind, path="secret"), "", workspace)["passed"]


def test_revision_ignores_lifecycle_but_includes_behavior(deployment):
    access, root = create_system(deployment)
    before = revision(root)
    access.store.set_status("stats", "published")
    assert revision(root) == before
    access.store.update("stats", description="Other routing behavior")
    assert revision(root) != before


def test_evidence_is_outside_candidate_and_goes_stale(deployment):
    access, root = create_system(deployment)
    assert quality_summary(root)["state"] == "not_tested"
    with pytest.raises(ValueError):
        require_quality(root)
    save_evidence(root, "evaluation", {"revision": revision(root), "passed": True})
    assert quality_summary(root)["ready"]
    require_quality(root)
    assert not (root / "evaluation.json").exists()
    (root / "README.md").write_text("Changed")
    assert quality_summary(root)["state"] == "stale"
    with pytest.raises(ValueError):
        require_quality(root)


def test_tool_evidence_requires_real_offline_tests(deployment):
    access, root = create_system(deployment)
    config = yaml.safe_load((root / "config.yaml").read_text())
    config["tools"] = {"stats": {"type": "mcp", "tool_package": "tools/stats"}}
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    record_tool_test(root, "stats", {"ok": True, "offline": True, "tests": {"passed": 0}}, revision(root))
    assert not quality_summary(root)["tools_tested"]
    record_tool_test(root, "stats", {"ok": True, "offline": False, "tests": {"passed": 1}}, revision(root))
    assert not quality_summary(root)["tools_tested"]
    record_tool_test(root, "stats", {"ok": True, "offline": True, "tests": {"passed": 1}}, revision(root))
    assert quality_summary(root)["tools_tested"]


def test_fork_preserves_active_source_and_requires_new_evidence(deployment):
    access, root = create_system(deployment)
    access.store.set_status("stats", "published")
    before = revision(root)
    assert "published" in call("builder_write", access, system="stats", path="README.md", content="changed")
    result = json.loads(call("builder_fork", access, system="stats", key="stats-v2", name="Stats v2"))
    assert result["created"] == "stats-v2"
    assert revision(root) == before
    assert access.store.get("stats").status == "published"
    assert access.store.get("stats-v2").status == "draft"
    assert (access.store.directory("stats-v2") / "quality.yaml").is_file()
    assert not result["health"]["quality"]["ready"]


@pytest.mark.asyncio
async def test_evaluation_uses_snapshot_and_checks_every_repeat(deployment, monkeypatch):
    from core import system_evaluation as evaluator
    access, root = create_system(deployment)
    doc = contract_document()
    doc["repetitions"] = 2
    (root / "quality.yaml").write_text(yaml.safe_dump(doc))
    calls = []

    async def run(access, snapshot, scenario, **budgets):
        assert snapshot != root
        assert not (snapshot / "quality.yaml").exists()
        assert (snapshot / "config.yaml").is_file()
        calls.append(budgets)
        return {"name": scenario.name, "passed": len(calls) == 1, "tokens": 12, "tool_calls": 1, "assertions": []}

    monkeypatch.setattr(evaluator, "run_scenario", run)
    report = await evaluator.evaluate(access, "stats")
    assert not report["passed"]
    assert report["pass_rate"] == 0.5
    assert report["tokens"] == 24
    assert report["tokens_per_success"] == 24
    assert quality_summary(root)["state"] == "failed"


@pytest.mark.asyncio
async def test_edit_during_evaluation_invalidates_pass(deployment, monkeypatch):
    from core import system_evaluation as evaluator
    access, root = create_system(deployment)

    async def run(*args, **kwargs):
        (root / "README.md").write_text("Changed during evaluation")
        return {"name": "count", "passed": True, "tokens": 1, "tool_calls": 0, "assertions": []}

    monkeypatch.setattr(evaluator, "run_scenario", run)
    report = await evaluator.evaluate(access, "stats")
    assert not report["passed"]
    assert "changed during" in report["error"]
    assert quality_summary(root)["state"] == "stale"


@pytest.mark.asyncio
async def test_suite_budget_prevents_another_run(deployment, monkeypatch):
    from core import system_evaluation as evaluator
    access, root = create_system(deployment)
    document = contract_document()
    document.update(repetitions=2, max_total_tokens=5)
    (root / "quality.yaml").write_text(yaml.safe_dump(document))

    async def run(*args, **kwargs):
        return {"name": "count", "passed": True, "tokens": 5, "tool_calls": 0, "assertions": []}

    monkeypatch.setattr(evaluator, "run_scenario", run)
    report = await evaluator.evaluate(access, "stats")
    assert len(report["results"]) == 1
    assert not report["passed"]
    assert report["error"] == "Suite budget exhausted"


def test_design_catches_uncontracted_agents(deployment):
    access, root = create_system(deployment)
    doc = contract_document()
    doc["roles"][0]["agent"] = "absent"
    assert design_review(root, QualityContract.model_validate(doc))["errors"]


def test_invalid_contract_is_rejected_before_writing(deployment):
    access, root = create_system(deployment)
    before = (root / "quality.yaml").read_text()
    assert call("builder_write", access, system="stats", path="quality.yaml", content="goal: only") .startswith("Not written")
    assert (root / "quality.yaml").read_text() == before


def test_owner_cannot_activate_a_contract_without_current_evidence(deployment, tmp_path):
    from tests.test_system_hub import ROOT, client_for
    access, root = create_system(deployment)
    client = client_for(deployment, tmp_path)
    response = client.post("/api/systems/created/stats/status", headers=ROOT, json={"status": "published"})
    assert response.status_code == 400
    save_evidence(root, "evaluation", {"revision": revision(root), "passed": True})
    assert client.post("/api/systems/created/stats/status", headers=ROOT, json={"status": "published"}).status_code == 200
    detail = client.get("/api/systems/created/stats", headers=ROOT).json()
    assert detail["quality"]["ready"]


def test_observer_counts_nested_usage_once():
    from core.system_evaluation import EvaluationObserver
    observer = EvaluationObserver()
    event = SimpleNamespace(data=SimpleNamespace(type="response.completed", response=SimpleNamespace(
        id="one", usage=SimpleNamespace(input_tokens=8, output_tokens=2))))
    observer.handle_event(event, agent_key="worker")
    observer.handle_event(event, agent_key="worker")
    observer.handle_event(SimpleNamespace(name="tool_called"), agent_key="coordinator")
    assert observer.tokens == 10 and observer.tool_calls == 1
    assert observer.by_agent["worker"]["tokens"] == 10


def test_owner_acceptance_cases_are_not_builder_files(deployment, tmp_path):
    from tests.test_system_hub import ROOT, ALICE, client_for
    access, root = create_system(deployment)
    client = client_for(deployment, tmp_path)
    suite = {"scenarios": contract_document()["scenarios"]}
    route = "/api/systems/created/stats/acceptance"
    assert client.put(route, headers=ALICE, json={"yaml": yaml.safe_dump(suite)}).status_code == 403
    assert client.put(route, headers=ROOT, json={"yaml": yaml.safe_dump(suite)}).status_code == 200
    from core.system_quality import acceptance_revision
    assert acceptance_revision(root)
    assert "outside" in call("builder_read", access, system="stats", path="../.quality/stats/acceptance.json")
    assert "outside" in call("builder_write", access, system="stats", path="../.quality/stats/acceptance.json", content="{}")
    assert not (root / "acceptance.json").exists()
    save_evidence(root, "evaluation", {"revision": revision(root), "acceptance_revision": acceptance_revision(root), "passed": True})
    assert quality_summary(root)["ready"]
    suite["scenarios"][0]["message"] = "A new owner request"
    assert client.put(route, headers=ROOT, json={"yaml": yaml.safe_dump(suite)}).status_code == 200
    assert quality_summary(root)["state"] == "stale"


@pytest.mark.asyncio
async def test_independent_cases_are_run_and_can_fail_the_suite(deployment, monkeypatch):
    from core import system_evaluation as evaluator
    access, root = create_system(deployment)
    suite = {"scenarios": [{"name": "independent", "message": "Unseen task",
                            "assertions": [{"kind": "contains", "value": "correct"}]}]}
    save_evidence(root, "acceptance", suite)

    async def run(access, snapshot, scenario, **kwargs):
        assert not (snapshot / "acceptance.json").exists()
        return {"name": scenario.name, "passed": scenario.name != "independent", "tokens": 1,
                "tool_calls": 0, "assertions": []}

    monkeypatch.setattr(evaluator, "run_scenario", run)
    report = await evaluator.evaluate(access, "stats")
    assert report["expected_runs"] == 2
    assert not report["passed"]
    assert [r["suite"] for r in report["results"]] == ["development", "acceptance"]


def test_comparison_refuses_changed_suite_and_reports_regression():
    from core.system_quality import comparison
    report = {"revision": "a", "suite_digest": "same", "results": [{"name": "task", "repetition": 1, "passed": True}],
              "tokens": 100, "seconds": 10, "tool_calls": 3}
    baseline = {"state": "passed", "ready": True, "evaluation": report}
    candidate_report = {**report, "revision": "b", "tokens": 10,
                        "results": [{"name": "task", "repetition": 1, "passed": False}]}
    candidate = {"state": "failed", "ready": False, "evaluation": candidate_report}
    result = comparison(baseline, candidate)
    assert result["tokens_delta"] == -90
    assert result["regressions"] == ["development/task/1"]
    assert not result["candidate_passed"]
    candidate_report["suite_digest"] = "different"
    with pytest.raises(ValueError, match="identical"):
        comparison(baseline, candidate)


def test_contract_gate_cannot_be_removed_by_deleting_contract(deployment):
    access, root = create_system(deployment)
    call("builder_write", access, system="stats", path="quality.yaml", content=yaml.safe_dump(contract_document()))
    call("builder_delete", access, system="stats", path="quality.yaml")
    assert not (root / "quality.yaml").exists()
    with pytest.raises(ValueError):
        require_quality(root)


def test_fork_copies_owner_cases_without_copying_reports(deployment):
    from core.system_quality import acceptance_suite
    access, root = create_system(deployment)
    save_evidence(root, "acceptance", {"scenarios": contract_document()["scenarios"]})
    result = json.loads(call("builder_fork", access, system="stats", key="stats-next", name="Next"))
    fork = access.store.directory("stats-next")
    assert acceptance_suite(fork) == acceptance_suite(root)
    assert not result["health"]["quality"]["ready"]


def test_active_behavior_cannot_be_edited_through_config_api(deployment, tmp_path):
    from tests.test_system_hub import ROOT, client_for
    access, root = create_system(deployment)
    access.store.set_status("stats", "published")
    client = client_for(deployment, tmp_path)
    assert client.put("/api/systems/created/stats/config", headers=ROOT, json={"yaml": private_config()}).status_code == 400
    assert client.patch("/api/systems/created/stats", headers=ROOT, json={"description": "Different"}).status_code == 400
    assert client.patch("/api/systems/created/stats", headers=ROOT, json={"name": "Better name"}).status_code == 200


def test_overlapping_fixtures_are_rejected():
    with pytest.raises(ValueError, match="overlap"):
        Scenario(name="bad", message="Task", fixtures={"a": "file", "a/b": "other"},
                 assertions=[{"kind": "contains", "value": "ok"}])


def test_corrupt_owner_suite_fails_closed(deployment):
    from core.system_quality import evidence_dir
    access, root = create_system(deployment)
    save_evidence(root, "acceptance", {"scenarios": contract_document()["scenarios"]})
    (evidence_dir(root) / "acceptance.json").write_text("broken JSON")
    assert quality_summary(root)["state"] == "invalid"
    with pytest.raises(ValueError):
        require_quality(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,passes", [("ok", True), ("no_usage", False), ("budget", False), ("timeout", False), ("docker_error", False)])
async def test_scenario_failure_paths_always_remove_sandbox(deployment, monkeypatch, mode, passes):
    import asyncio
    from core.system_evaluation import run_scenario
    from core import agent_factory
    from core import tool_packages
    access, root = create_system(deployment)
    commands = []

    async def command(args, **kwargs):
        commands.append(args)
        if args[1] == "run" and mode == "docker_error":
            return 1, "", "Docker unavailable"
        return 0, "", ""

    built = []

    class Factory:
        def __init__(self, **kwargs):
            built.append(kwargs)
            self.workspace = kwargs["working_directory"]

        async def run_agent(self, *args, **kwargs):
            from pathlib import Path
            observer = kwargs["stream_observer"]
            if mode == "timeout":
                await asyncio.sleep(5)
            if mode != "no_usage":
                observer.handle_event(SimpleNamespace(data=SimpleNamespace(type="response.completed",
                    response=SimpleNamespace(id="r1", usage=SimpleNamespace(input_tokens=100 if mode == "budget" else 1, output_tokens=1)))),
                    agent_key="main")
            (Path(self.workspace) / "result.json").write_text('{"words": 2}')
            return "Done\nContext ID: ctx-12345678"

        async def cleanup(self):
            pass

    monkeypatch.setattr(tool_packages, "_run", command)
    monkeypatch.setattr(agent_factory, "AgentFactory", Factory)
    scenario = Scenario.model_validate({**contract_document()["scenarios"][0], "timeout_seconds": 1})
    result = await run_scenario(access, root, scenario, token_limit=10, call_limit=5)
    assert result["passed"] is passes, result
    assert any(args[1:3] == ["rm", "-f"] for args in commands)
    if mode == "ok":
        # The scenario runs on the owner's credentials and plan, never the environment's.
        assert built[0]["model_access"] is access.model_access
        assert any(args[1:3] == ["network", "disconnect"] for args in commands)
        assert any(args[1] == "stop" for args in commands)
        assert result["tokens"] == 2
    if mode == "no_usage":
        assert "usage" in result["error"]
    if mode == "timeout":
        assert "deadline" in result["error"]


@pytest.mark.asyncio
async def test_evaluation_rejects_overlapping_runs(deployment, monkeypatch):
    import asyncio
    from core import system_evaluation as evaluator
    access, root = create_system(deployment)
    started, release = asyncio.Event(), asyncio.Event()

    async def run(*args, **kwargs):
        started.set()
        await release.wait()
        return {"name": "count", "passed": True, "tokens": 1, "tool_calls": 0, "assertions": []}

    monkeypatch.setattr(evaluator, "run_scenario", run)
    first = asyncio.create_task(evaluator.evaluate(access, "stats"))
    await started.wait()
    try:
        with pytest.raises(ValueError, match="already running"):
            await evaluator.evaluate(access, "stats")
    finally:
        release.set()
        await first


def test_private_acceptance_is_scoped_to_owner(deployment, tmp_path):
    from tests.test_system_builder import space_of
    from tests.test_system_hub import ALICE, BOB, client_for
    alice = space_of(deployment, "alice", admin=False)
    call("builder_create", alice._builder_access(), key="private-stats", name="Stats", description="Count", config_yaml=private_config())
    client = client_for(deployment, tmp_path)
    route = "/api/systems/built/private-stats/acceptance"
    suite = yaml.safe_dump({"scenarios": contract_document()["scenarios"]})
    assert client.put(route, headers=BOB, json={"yaml": suite}).status_code == 404
    assert client.put(route, headers=ALICE, json={"yaml": suite}).status_code == 200
    assert client.get("/api/systems/built/private-stats", headers=BOB).status_code == 404
