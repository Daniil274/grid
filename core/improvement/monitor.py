"""Canary and post-promotion monitor for improvement experiments."""

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
from schemas.improvement import ImprovementExperiment, ImprovementExperimentStatus


class ImprovementMonitor:
    """Run canary and post-promotion benchmark checks."""

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

    def run_canary(self, experiment_id: str) -> ImprovementExperiment:
        return asyncio.run(self._run_canary(experiment_id))

    def monitor_experiment(self, experiment_id: str) -> ImprovementExperiment:
        return asyncio.run(self._monitor_experiment(experiment_id))

    async def _run_canary(self, experiment_id: str) -> ImprovementExperiment:
        experiment = self.registry.get_experiment(experiment_id)
        if experiment.status in (
            ImprovementExperimentStatus.PROMOTION_PENDING,
            ImprovementExperimentStatus.IMPLEMENTATION_READY,
        ):
            experiment = self.registry.promote_experiment(
                experiment_id=experiment_id,
                promoter="canary_monitor",
                notes="Entered canary window",
            )
        if experiment.status != ImprovementExperimentStatus.CANARY:
            raise ValueError(f"Experiment '{experiment_id}' is not ready for canary")

        baseline = await self._load_baseline_scorecard(experiment)
        candidate = await self._run_candidate_shadow_scorecard(experiment)
        comparison = self._compare_scorecards(baseline, candidate)
        self.registry.record_runtime_check(
            experiment_id=experiment_id,
            phase="canary",
            scorecard=candidate,
            comparison=comparison,
            trigger="canary_monitor",
        )
        if comparison["passed"]:
            return self.registry.complete_canary(
                experiment_id=experiment_id,
                promoter="canary_monitor",
                notes="Canary benchmark passed",
                comparison=comparison,
            )
        return self.registry.rollback_experiment(
            experiment_id=experiment_id,
            trigger="canary_monitor",
            reason="Canary benchmark regressed before promotion",
            comparison=comparison,
        )

    async def _monitor_experiment(self, experiment_id: str) -> ImprovementExperiment:
        experiment = self.registry.get_experiment(experiment_id)
        if experiment.status != ImprovementExperimentStatus.PROMOTED:
            raise ValueError(f"Experiment '{experiment_id}' is not promoted")
        baseline = await self._load_baseline_scorecard(experiment)
        current = await self._run_scorecard(str(self.config.config_path))
        comparison = self._compare_scorecards(baseline, current)
        self.registry.record_runtime_check(
            experiment_id=experiment_id,
            phase="post_promotion_monitor",
            scorecard=current,
            comparison=comparison,
            trigger="post_promotion_monitor",
        )
        if comparison["passed"]:
            return self.registry.get_experiment(experiment_id)
        return self.registry.rollback_experiment(
            experiment_id=experiment_id,
            trigger="post_promotion_monitor",
            reason="Post-promotion benchmark regressed below rollback threshold",
            comparison=comparison,
        )

    async def _load_baseline_scorecard(self, experiment: ImprovementExperiment) -> BenchmarkScorecard:
        if experiment.baseline_scorecard is not None:
            return experiment.baseline_scorecard
        baseline_file = Path(self.baseline_path)
        if baseline_file.exists():
            import json

            with baseline_file.open("r", encoding="utf-8") as fh:
                return BenchmarkScorecard(**json.load(fh))
        return await self._run_scorecard(str(self.config.config_path))

    async def _run_candidate_shadow_scorecard(self, experiment: ImprovementExperiment) -> BenchmarkScorecard:
        with tempfile.TemporaryDirectory(prefix="grid-canary-") as temp_dir:
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
        threshold = self.config.get_improvement_config().rollback_threshold
        return {
            "baseline_aggregate_score": baseline.aggregate_score,
            "candidate_aggregate_score": candidate.aggregate_score,
            "aggregate_score_delta": delta,
            "rollback_threshold": threshold,
            "passed": delta >= threshold,
        }
