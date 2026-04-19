import json

from core.config import Config
from core.improvement.evaluator import ExperimentEvaluator
from core.improvement.registry import ImprovementRegistry
from schemas.benchmarking import BenchmarkAdapterResult


class FakeAdapter:
    def __init__(self, config_path: str, working_directory: str | None = None):
        self.config_path = config_path
        self.working_directory = working_directory

    async def run_fixture(self, fixture):
        return BenchmarkAdapterResult(
            output="ok",
            tools_used=[],
            latency_ms=5,
        )

    async def cleanup(self):
        return None


def test_experiment_evaluator_records_scorecards(temp_dir):
    config_path = temp_dir / "config.json"
    payload = {
        "settings": {
            "default_agent": "chat_agent",
            "working_directory": str(temp_dir),
        },
        "providers": {
            "lm-studio": {
                "name": "lm-studio",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "lm-studio",
            }
        },
        "models": {
            "glm-4.7-flash-local": {
                "name": "glm-4.7-flash-local",
                "provider": "lm-studio",
            },
            "minimax-m2.7": {
                "name": "minimax-m2.7",
                "provider": "lm-studio",
            },
        },
        "agents": {
            "chat_agent": {
                "name": "Chat",
                "model": "glm-4.7-flash-local",
                "tools": [],
            },
            "coordinator": {
                "name": "Coordinator",
                "model": "minimax-m2.7",
                "tools": [],
            },
        },
        "improvement": {
            "enabled": True,
            "registry_path": "data/improvement_registry.json",
            "plans_directory": "plans",
            "require_human_requirements_review": False,
            "require_human_final_review": False,
            "require_benchmark_before_promotion": True,
            "promotion_threshold": 0.0,
            "allowed_change_types": ["refactor"],
            "allowed_paths": ["core/"],
            "allowed_config_keys": ["agents.coordinator.model"],
        },
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    metrics_path = temp_dir / "benchmarks" / "metrics.yaml"
    fixtures_dir = temp_dir / "benchmarks" / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        "version: 1\nmetrics:\n  - name: task_completion\n    description: pass rate\n",
        encoding="utf-8",
    )
    (fixtures_dir / "fixture.yaml").write_text(
        "\n".join(
            [
                "id: smoke",
                "prompt: ping",
                "agent: chat_agent",
                "owner: tests",
                "source: synthetic",
                "required_substrings:",
                "  - ok",
            ]
        ),
        encoding="utf-8",
    )

    config = Config(str(config_path))
    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Provider failures",
        summary="Need fallback.",
        related_paths=["core/"],
    )
    experiment = registry.create_experiment(
        problem_id=problem.id,
        title="Fallback model",
        hypothesis="Use local model",
        change_type="refactor",
        allowed_paths=["core/"],
        config_diff=[{"path": "agents.coordinator.model", "new": "glm-4.7-flash-local"}],
    )

    evaluator = ExperimentEvaluator(
        config=config,
        registry=registry,
        adapter_factory=lambda cfg_path, workdir: FakeAdapter(cfg_path, workdir),
        metrics_path=str(metrics_path),
        fixtures_dir=str(fixtures_dir),
        baseline_path=str(temp_dir / "benchmarks" / "baseline.json"),
    )
    updated = evaluator.evaluate_experiment(experiment.id)

    assert updated.baseline_scorecard is not None
    assert updated.candidate_scorecard is not None
    assert updated.evaluation_summary["passed"] is True
