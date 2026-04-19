"""Benchmark evaluator for improvement experiments."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from benchmarks.run import BenchmarkRunner, LiveGridAdapter
from core.config.config import Config
from core.improvement.config_apply import apply_config_diff
from core.improvement.registry import ImprovementRegistry
from schemas.benchmarking import BenchmarkScorecard
from schemas.improvement import ImprovementExperiment


class ExperimentEvaluator:
    """Run benchmark A/B evaluation for config-diff experiments."""

    def __init__(
        self,
        *,
        config: Config,
        registry: Optional[ImprovementRegistry] = None,
        adapter_factory: Optional[Callable[[str, Optional[str]], Any]] = None,
        metrics_path: str = "benchmarks/metrics.yaml",
        fixtures_dir: str = "benchmarks/fixtures",
        baseline_path: str = "benchmarks/baseline.json",
    ) -> None:
        self.config = config
        self.registry = registry or ImprovementRegistry(config=config)
        self.adapter_factory = adapter_factory or (
            lambda config_path, workdir: LiveGridAdapter(config_path=config_path, working_directory=workdir)
        )
        self.metrics_path = metrics_path
        self.fixtures_dir = fixtures_dir
        self.baseline_path = baseline_path

    def evaluate_experiment(self, experiment_id: str) -> ImprovementExperiment:
        return asyncio.run(self._evaluate_experiment(experiment_id))

    async def _evaluate_experiment(self, experiment_id: str) -> ImprovementExperiment:
        experiment = self.registry.get_experiment(experiment_id)
        baseline_scorecard = await self._load_baseline_scorecard()
        candidate_scorecard = await self._run_candidate_scorecard(experiment)
        comparison = self._compare_scorecards(baseline_scorecard, candidate_scorecard)
        return self.registry.record_evaluation(
            experiment_id=experiment_id,
            baseline_scorecard=baseline_scorecard,
            candidate_scorecard=candidate_scorecard,
            comparison=comparison,
            evaluator="benchmark_harness",
        )

    async def _load_baseline_scorecard(self) -> BenchmarkScorecard:
        baseline_file = Path(self.baseline_path)
        if baseline_file.exists():
            import json

            with baseline_file.open("r", encoding="utf-8") as fh:
                return BenchmarkScorecard(**json.load(fh))
        return await self._run_scorecard(str(self.config.config_path))

    async def _run_candidate_scorecard(self, experiment: ImprovementExperiment) -> BenchmarkScorecard:
        with tempfile.TemporaryDirectory(prefix="grid-eval-") as temp_dir:
            temp_config = Path(temp_dir) / Path(self.config.config_path).name
            temp_config.write_text(Path(self.config.config_path).read_text(encoding="utf-8"), encoding="utf-8")
            apply_config_diff(str(temp_config), experiment.config_diff)
            return await self._run_scorecard(str(temp_config))

    async def _run_scorecard(self, config_path: str) -> BenchmarkScorecard:
        adapter = self.adapter_factory(config_path, self.config.get_working_directory())
        runner = BenchmarkRunner(
            config_path=config_path,
            metrics_path=self.metrics_path,
            fixtures_dir=self.fixtures_dir,
            adapter=adapter,
        )
        return await runner.run()

    def _compare_scorecards(
        self,
        baseline: BenchmarkScorecard,
        candidate: BenchmarkScorecard,
    ) -> dict[str, Any]:
        delta = candidate.aggregate_score - baseline.aggregate_score
        threshold = self.config.get_improvement_config().promotion_threshold
        return {
            "baseline_aggregate_score": baseline.aggregate_score,
            "candidate_aggregate_score": candidate.aggregate_score,
            "aggregate_score_delta": delta,
            "promotion_threshold": threshold,
            "passed": delta >= threshold,
        }
