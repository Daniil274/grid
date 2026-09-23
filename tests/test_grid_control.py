"""Control-plane invariants, independent of Docker and Grid's agent SDK."""

from dataclasses import replace
from pathlib import Path

import pytest

from grid_control.controller import Controller
from grid_control.docker import DockerRuntime
from grid_control.models import Check, Policy, Scenario, Trial, decide
from grid_control.store import Store


@pytest.fixture
def policy():
    return Policy(
        "runtime",
        "verifier",
        (
            Check(
                "routing",
                "/api/chat/bootstrap",
                json_pointer="/routing_enabled",
                expected=True,
            ),
        ),
        repetitions=2,
    )


class FakeRuntime:
    def __init__(self, fail=False, cleanup_error=False):
        self.calls = []
        self.fail = fail
        self.cleanup_error = cleanup_error

    def revision(self, repo, ref):
        return ref

    def image(self, ref):
        return "immutable-" + ref

    def build(self, repo, commit, runtime, experiment, timeout):
        self.calls.append(("build", commit, runtime))
        if self.fail:
            raise RuntimeError("build failed")
        return commit + "-image"

    def trial(self, image, verifier, experiment, index, policy):
        self.calls.append(("trial", image, verifier, index))
        return Trial({"routing": image.startswith("new")}, 0.2)

    def cleanup(self, experiment):
        self.calls.append(("cleanup", experiment))
        if self.cleanup_error:
            raise RuntimeError("daemon unreachable")


def test_records_exact_inputs_and_alternates_fresh_trials(tmp_path, policy):
    store = Store(tmp_path / "journal.db")
    runtime = FakeRuntime()
    report = Controller(store, runtime).evaluate(tmp_path, "old", "new", policy)
    assert report["status"] == "accepted"
    assert report["policy_sha256"] == policy.digest
    assert report["runtime_image"] == "immutable-runtime"
    trials = [call for call in runtime.calls if call[0] == "trial"]
    assert [call[1] for call in trials] == [
        "old-image",
        "new-image",
        "new-image",
        "old-image",
    ]
    assert [call[3] for call in trials] == [0, 1, 2, 3]
    assert Store(tmp_path / "journal.db").get(report["id"]) == report


@pytest.mark.parametrize("fail,cleanup_error", [(True, False), (False, True)])
def test_infrastructure_failure_never_accepts(tmp_path, policy, fail, cleanup_error):
    runtime = FakeRuntime(fail, cleanup_error)
    report = Controller(Store(tmp_path / "journal.db"), runtime).evaluate(
        tmp_path, "old", "new", policy
    )
    assert report["status"] == "failed"
    assert runtime.calls[-1][0] == "cleanup"


def test_flaky_candidate_is_rejected(policy):
    baseline = [Trial({"routing": False}, 1)] * 2
    candidate = [Trial({"routing": True}, 1), Trial({"routing": False}, 1)]
    assert not decide(policy, baseline, candidate)["accepted"]


@pytest.mark.parametrize(
    "checks", [{}, {"routing": "true"}, {"routing": True, "extra": True}]
)
def test_missing_or_forged_results_fail_closed(policy, checks):
    with pytest.raises(ValueError):
        decide(policy, [Trial({"routing": True}, 1)] * 2, [Trial(checks, 1)] * 2)


def test_equal_results_do_not_satisfy_improvement_threshold(policy):
    trials = [Trial({"routing": True}, 1)] * 2
    assert not decide(replace(policy, min_improvement=0.1), trials, trials)["accepted"]


def test_active_experiment_cannot_be_cleaned_concurrently(tmp_path):
    store = Store(tmp_path / "journal.db")
    experiment = "a" * 32
    with store.lease(experiment):
        with pytest.raises(RuntimeError, match="running controller"):
            with store.lease(experiment):
                pytest.fail("Acquired active experiment")
    with store.lease(experiment):
        pass


def test_terminal_decision_cannot_be_overwritten(tmp_path):
    store = Store(tmp_path / "journal.db")
    store.create("experiment", {})
    store.finish("experiment", "rejected", {})
    with pytest.raises(ValueError):
        store.finish("experiment", "accepted", {})


def test_verifier_has_no_candidate_source_mount_or_credentials(policy):
    class RecordingDocker(DockerRuntime):
        def __init__(self):
            self.commands = []

        def command(self, args, **kwargs):
            self.commands.append((args, kwargs))
            return b'{"checks": {"routing": true}, "elapsed_seconds": 0.1}'

    runtime = RecordingDocker()
    runtime.trial(
        "candidate-image",
        "verifier-image",
        "a" * 32,
        0,
        replace(policy, environment_names=("OPENROUTER_API_KEY",)),
    )
    assert "--internal" in runtime.commands[0][0]
    verifier = runtime.commands[-1][0]
    assert "--env" not in verifier
    assert "--volume" not in verifier
    assert "--read-only" in verifier
    assert "--cap-drop=ALL" in verifier
    assert "--network-alias=candidate" in runtime.commands[1][0]


def test_source_ref_is_not_a_git_option():
    class RecordingDocker(DockerRuntime):
        def command(self, args, **kwargs):
            assert args[-2] == "--end-of-options"
            return b"a" * 40

    RecordingDocker().revision(Path("."), "--output=unexpected")


def test_scenario_pass_rate_tolerates_one_noisy_repetition_but_not_a_regression():
    policy = Policy(
        "runtime", "verifier", (Check("web", "/"),), repetitions=3,
        scenarios=(Scenario("route", "hello", system="engineering"),),
    )
    def trials(*outcomes):
        return [Trial({"web": True, "scenario:route": ok}, 1) for ok in outcomes]
    noisy = trials(True, False, True)
    assert not decide(policy, noisy, noisy)["accepted"], "default stays strict"
    lenient = replace(policy, scenario_pass_rate=0.66)
    assert decide(lenient, noisy, noisy)["accepted"]
    decision = decide(lenient, noisy, trials(False, True, False))
    assert not decision["accepted"] and decision["failing"] == ["scenario:route"]
    web_down = [Trial({"web": False, "scenario:route": True}, 1)] + trials(True, True)
    assert not decide(lenient, noisy, web_down)["accepted"], "HTTP checks stay strict"
    with pytest.raises(ValueError):
        replace(policy, scenario_pass_rate=0.5)
