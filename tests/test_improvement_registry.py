import json

import pytest
import yaml

from core.config_apply import apply_config_diff, revert_config_diff
from core.config import Config
from core.improvement_registry import ImprovementRegistry
from schemas import GridConfig


def test_grid_config_supports_improvement_section():
    config = GridConfig(
        improvement={
            "enabled": True,
            "registry_path": "data/custom_registry.json",
            "require_human_requirements_review": True,
            "require_human_final_review": False,
        }
    )

    assert config.improvement.enabled is True
    assert config.improvement.registry_path == "data/custom_registry.json"
    assert config.improvement.require_human_requirements_review is True
    assert config.improvement.require_human_final_review is False


def test_config_exposes_improvement_settings(config_file, sample_config):
    sample_config["settings"]["working_directory"] = str(config_file.parent)
    sample_config["improvement"] = {
        "enabled": True,
        "registry_path": "data/improvement_registry.json",
        "plans_directory": "plans",
        "require_human_requirements_review": True,
        "require_human_final_review": True,
    }
    config_file.write_text(json.dumps(sample_config), encoding="utf-8")

    config = Config(str(config_file))

    assert config.get_improvement_config().enabled is True
    assert config.get_improvement_config().plans_directory == "plans"


def test_grid_config_supports_allowed_config_keys():
    config = GridConfig(
        improvement={
            "enabled": True,
            "allowed_config_keys": ["settings.max_turns", "agents.chat_agent.model"],
        }
    )

    assert config.improvement.allowed_config_keys == [
        "settings.max_turns",
        "agents.chat_agent.model",
    ]


def test_improvement_registry_requires_human_reviews_before_experiment_and_promotion(
    temp_dir, config_file, sample_config
):
    sample_config["settings"]["working_directory"] = str(temp_dir)
    sample_config["improvement"] = {
        "enabled": True,
        "registry_path": "data/improvement_registry.json",
        "plans_directory": "plans",
        "require_human_requirements_review": True,
        "require_human_final_review": True,
        "allowed_change_types": ["tests", "logging"],
        "allowed_paths": ["tests/", "docs/"],
        "max_open_experiments": 5,
    }
    config_file.write_text(json.dumps(sample_config), encoding="utf-8")

    config = Config(str(config_file))
    registry = ImprovementRegistry(config=config)

    problem = registry.create_problem(
        title="Flaky tests around improvement loop",
        summary="We need a safer first staged loop.",
        requirements=["Keep user in the loop"],
        success_criteria=["No promotion without review"],
        related_paths=["tests/"],
    )

    with pytest.raises(ValueError, match="Human requirements review is required"):
        registry.create_experiment(
            problem_id=problem.id,
            title="Add review gates",
            hypothesis="Review gates will block unsafe promotion",
            change_type="tests",
            allowed_paths=["tests/"],
        )

    registry.record_review(
        target_id=problem.id,
        review_type="requirements",
        decision="approved",
        reviewer="user",
        reviewer_kind="human",
        summary="Requirements look correct.",
        metadata={"approved_via": "cli"},
    )

    experiment = registry.create_experiment(
        problem_id=problem.id,
        title="Add review gates",
        hypothesis="Review gates will block unsafe promotion",
        change_type="tests",
        allowed_paths=["tests/"],
    )

    with pytest.raises(ValueError, match="Human final review is required"):
        registry.promote_experiment(experiment_id=experiment.id, promoter="agent")

    registry.record_review(
        target_id=experiment.id,
        review_type="final",
        decision="approved",
        reviewer="user",
        reviewer_kind="human",
        summary="Safe to promote.",
        metadata={"approved_via": "cli"},
    )

    canary = registry.promote_experiment(
        experiment_id=experiment.id,
        promoter="user",
        notes="Approved for staged rollout",
    )

    assert canary.status.value == "canary"
    assert registry.get_problem(problem.id).status.value == "experiment_active"

    with pytest.raises(ValueError, match="CLI approval flow"):
        registry.record_review(
            target_id=problem.id,
            review_type="requirements",
            decision="approved",
            reviewer="agent pretending to be human",
            reviewer_kind="human",
            summary="This should be blocked.",
        )


def test_improvement_registry_respects_allowed_paths(temp_dir):
    registry_path = temp_dir / "registry.json"
    config = Config.__new__(Config)
    config.get_improvement_config = lambda: type(
        "ImprovementCfg",
        (),
        {
            "registry_path": str(registry_path),
            "require_human_requirements_review": False,
            "require_human_final_review": False,
            "auto_promote_safe_changes": False,
            "allowed_change_types": ["logging"],
            "allowed_paths": ["core/"],
            "max_open_experiments": 5,
        },
    )()
    config.get_absolute_path = lambda value: str(temp_dir / value)

    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Logging cleanup",
        summary="Normalize improvement logs.",
        related_paths=["core/"],
    )

    with pytest.raises(ValueError, match="Path 'docs/' is not allowed"):
        registry.create_experiment(
            problem_id=problem.id,
            title="Write docs instead",
            hypothesis="This should fail",
            change_type="logging",
            allowed_paths=["docs/"],
        )


def test_improvement_registry_validates_and_snapshots_config_diff(temp_dir, config_file, sample_config):
    sample_config["settings"]["working_directory"] = str(temp_dir)
    sample_config["improvement"] = {
        "enabled": True,
        "registry_path": "data/improvement_registry.json",
        "plans_directory": "plans",
        "require_human_requirements_review": False,
        "require_human_final_review": False,
        "allowed_change_types": ["refactor"],
        "allowed_paths": ["core/"],
        "allowed_config_keys": ["settings.max_turns"],
        "max_open_experiments": 5,
    }
    config_file.write_text(json.dumps(sample_config), encoding="utf-8")

    config = Config(str(config_file))
    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Tune max_turns",
        summary="Limit runaway loops.",
        related_paths=["core/"],
    )

    experiment = registry.create_experiment(
        problem_id=problem.id,
        title="Lower max_turns",
        hypothesis="Lower turn count will reduce loops",
        change_type="refactor",
        allowed_paths=["core/"],
        config_diff=[{"path": "settings.max_turns", "new": 12}],
    )

    assert experiment.config_diff[0].path == "settings.max_turns"
    assert experiment.config_diff[0].old == sample_config["settings"]["max_turns"]
    assert experiment.config_diff[0].new == 12

    with pytest.raises(ValueError, match="Config key 'providers.openrouter.base_url' is not allowed"):
        registry.create_experiment(
            problem_id=problem.id,
            title="Bad config change",
            hypothesis="Should be rejected",
            change_type="refactor",
            allowed_paths=["core/"],
            config_diff=[{"path": "providers.openrouter.base_url", "new": "https://example.com"}],
        )


def test_config_apply_and_revert_round_trip(temp_dir):
    config_path = temp_dir / "config.yaml"
    config_path.write_text(
        json.dumps(
            {
                "settings": {"max_turns": 25},
                "agents": {"chat_agent": {"model": "old-model"}},
            }
        ),
        encoding="utf-8",
    )

    diff = [
        {"path": "settings.max_turns", "old": 25, "new": 10},
        {"path": "agents.chat_agent.model", "old": "old-model", "new": "new-model"},
    ]

    apply_config_diff(str(config_path), diff)
    applied = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert applied["settings"]["max_turns"] == 10
    assert applied["agents"]["chat_agent"]["model"] == "new-model"

    revert_config_diff(str(config_path), diff)
    reverted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert reverted["settings"]["max_turns"] == 25
    assert reverted["agents"]["chat_agent"]["model"] == "old-model"


def test_config_apply_preserves_visual_yaml_structure(temp_dir):
    config_path = temp_dir / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "settings:",
                "  max_turns: 25",
                "telegram:",
                "  allowed_users: null",
                "  agent_logging:",
                "    # Keep this comment",
                "    tools_common_rules: |",
                "      First line.",
                "      Second line.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    diff = [{"path": "settings.max_turns", "old": 25, "new": 12}]
    apply_config_diff(str(config_path), diff)

    updated_text = config_path.read_text(encoding="utf-8")
    assert "# Keep this comment" in updated_text
    assert "tools_common_rules: |" in updated_text
    assert "First line." in updated_text
    assert "max_turns: 12" in updated_text
    assert "allowed_users: null" in updated_text


def test_complete_canary_applies_config_diff(temp_dir, config_file, sample_config):
    sample_config["settings"]["working_directory"] = str(temp_dir)
    sample_config["improvement"] = {
        "enabled": True,
        "registry_path": "data/improvement_registry.json",
        "plans_directory": "plans",
        "require_human_requirements_review": False,
        "require_human_final_review": False,
        "allowed_change_types": ["refactor"],
        "allowed_paths": ["core/"],
        "allowed_config_keys": ["settings.max_turns"],
        "max_open_experiments": 5,
    }
    config_file.write_text(json.dumps(sample_config), encoding="utf-8")

    config = Config(str(config_file))
    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Tune max_turns",
        summary="Limit runaway loops.",
        related_paths=["core/"],
    )
    experiment = registry.create_experiment(
        problem_id=problem.id,
        title="Lower max_turns",
        hypothesis="Lower turn count will reduce loops",
        change_type="refactor",
        allowed_paths=["core/"],
        config_diff=[{"path": "settings.max_turns", "new": 12}],
    )

    canary = registry.promote_experiment(experiment_id=experiment.id, promoter="system")
    assert canary.status.value == "canary"
    registry.complete_canary(experiment_id=experiment.id, promoter="system")

    reloaded = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert reloaded["settings"]["max_turns"] == 12


def test_promotion_can_be_blocked_by_missing_or_failed_benchmark(temp_dir, config_file, sample_config):
    sample_config["settings"]["working_directory"] = str(temp_dir)
    sample_config["improvement"] = {
        "enabled": True,
        "registry_path": "data/improvement_registry.json",
        "plans_directory": "plans",
        "require_human_requirements_review": False,
        "require_human_final_review": False,
        "require_benchmark_before_promotion": True,
        "promotion_threshold": 0.01,
        "allowed_change_types": ["refactor"],
        "allowed_paths": ["core/"],
        "allowed_config_keys": ["settings.max_turns"],
        "max_open_experiments": 5,
    }
    config_file.write_text(json.dumps(sample_config), encoding="utf-8")

    config = Config(str(config_file))
    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Tune max_turns",
        summary="Limit runaway loops.",
        related_paths=["core/"],
    )
    experiment = registry.create_experiment(
        problem_id=problem.id,
        title="Lower max_turns",
        hypothesis="Lower turn count will reduce loops",
        change_type="refactor",
        allowed_paths=["core/"],
        config_diff=[{"path": "settings.max_turns", "new": 12}],
    )

    with pytest.raises(ValueError, match="Benchmark evaluation is required"):
        registry.promote_experiment(experiment_id=experiment.id, promoter="system")

    from schemas.benchmarking import BenchmarkScorecard

    baseline = BenchmarkScorecard(
        run_id="base",
        config_path=str(config_file),
        metrics_path="benchmarks/metrics.yaml",
        fixture_count=2,
        passed_count=2,
        aggregate_score=1.0,
        average_latency_ms=10,
    )
    candidate = BenchmarkScorecard(
        run_id="candidate",
        config_path=str(config_file),
        metrics_path="benchmarks/metrics.yaml",
        fixture_count=2,
        passed_count=1,
        aggregate_score=0.5,
        average_latency_ms=10,
    )
    registry.record_evaluation(
        experiment_id=experiment.id,
        baseline_scorecard=baseline,
        candidate_scorecard=candidate,
        comparison={
            "baseline_aggregate_score": 1.0,
            "candidate_aggregate_score": 0.5,
            "aggregate_score_delta": -0.5,
            "promotion_threshold": 0.01,
            "passed": False,
        },
        evaluator="test",
    )

    with pytest.raises(ValueError, match="did not pass benchmark evaluation"):
        registry.promote_experiment(experiment_id=experiment.id, promoter="system")
