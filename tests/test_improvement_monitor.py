import json

from core.config import Config
from core.improvement.monitor import ImprovementMonitor
from core.improvement.registry import ImprovementRegistry
from schemas.benchmarking import BenchmarkAdapterResult
import yaml


class PassingAdapter:
    def __init__(self, config_path: str, working_directory: str | None = None):
        self.config_path = config_path
        self.working_directory = working_directory

    async def run_fixture(self, fixture):
        return BenchmarkAdapterResult(output="ok", tools_used=[], latency_ms=5)

    async def cleanup(self):
        return None


class ConditionalAdapter:
    def __init__(self, config_path: str, working_directory: str | None = None):
        self.config_path = config_path
        self.working_directory = working_directory

    async def run_fixture(self, fixture):
        config_text = open(self.config_path, "r", encoding="utf-8").read()
        if fixture.required_substrings and "ok" in fixture.required_substrings and "new-model" in config_text:
            return BenchmarkAdapterResult(output="bad", tools_used=[], latency_ms=5)
        return BenchmarkAdapterResult(output="ok", tools_used=[], latency_ms=5)

    async def cleanup(self):
        return None


def _build_config(temp_dir):
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
            "old-model": {
                "name": "old-model",
                "provider": "lm-studio",
            },
            "new-model": {
                "name": "new-model",
                "provider": "lm-studio",
            },
        },
        "agents": {
            "chat_agent": {
                "name": "Chat",
                "model": "old-model",
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
            "rollback_threshold": -0.03,
            "allowed_change_types": ["refactor"],
            "allowed_paths": ["core/"],
            "allowed_config_keys": ["agents.chat_agent.model"],
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
    return config_path, metrics_path, fixtures_dir


def _prepare_promotable_experiment(config):
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
        config_diff=[{"path": "agents.chat_agent.model", "new": "new-model"}],
    )
    from schemas.benchmarking import BenchmarkScorecard

    baseline = BenchmarkScorecard(
        run_id="base",
        config_path=str(config.config_path),
        metrics_path="benchmarks/metrics.yaml",
        fixture_count=1,
        passed_count=1,
        aggregate_score=1.0,
        average_latency_ms=5,
    )
    candidate = BenchmarkScorecard(
        run_id="candidate",
        config_path=str(config.config_path),
        metrics_path="benchmarks/metrics.yaml",
        fixture_count=1,
        passed_count=1,
        aggregate_score=1.0,
        average_latency_ms=5,
    )
    updated = registry.record_evaluation(
        experiment_id=experiment.id,
        baseline_scorecard=baseline,
        candidate_scorecard=candidate,
        comparison={
            "baseline_aggregate_score": 1.0,
            "candidate_aggregate_score": 1.0,
            "aggregate_score_delta": 0.0,
            "promotion_threshold": 0.0,
            "passed": True,
        },
        evaluator="test",
    )
    return registry, updated


def test_canary_promotes_on_passing_shadow_run(temp_dir):
    config_path, metrics_path, fixtures_dir = _build_config(temp_dir)
    config = Config(str(config_path))
    registry, experiment = _prepare_promotable_experiment(config)
    registry.promote_experiment(experiment_id=experiment.id, promoter="system")

    monitor = ImprovementMonitor(
        config=config,
        registry=registry,
        adapter_factory=lambda cfg_path, workdir: PassingAdapter(cfg_path, workdir),
        metrics_path=str(metrics_path),
        fixtures_dir=str(fixtures_dir),
        baseline_path=str(temp_dir / "benchmarks" / "baseline.json"),
    )
    updated = monitor.run_canary(experiment.id)

    assert updated.status.value == "promoted"
    reloaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert reloaded["agents"]["chat_agent"]["model"] == "new-model"


def test_post_promotion_monitor_rolls_back_on_regression(temp_dir):
    config_path, metrics_path, fixtures_dir = _build_config(temp_dir)
    config = Config(str(config_path))
    registry, experiment = _prepare_promotable_experiment(config)
    registry.promote_experiment(experiment_id=experiment.id, promoter="system")
    registry.complete_canary(experiment_id=experiment.id, promoter="system", notes="Apply config")

    monitor = ImprovementMonitor(
        config=config,
        registry=registry,
        adapter_factory=lambda cfg_path, workdir: ConditionalAdapter(cfg_path, workdir),
        metrics_path=str(metrics_path),
        fixtures_dir=str(fixtures_dir),
        baseline_path=str(temp_dir / "benchmarks" / "baseline.json"),
    )
    updated = monitor.monitor_experiment(experiment.id)

    assert updated.status.value == "rolled_back"
    reloaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert reloaded["agents"]["chat_agent"]["model"] == "old-model"
