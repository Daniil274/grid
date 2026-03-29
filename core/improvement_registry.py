"""Persistent registry for the staged improvement loop."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config_apply import apply_config_diff, read_config_value, revert_config_diff
from core.config import Config
from schemas.benchmarking import BenchmarkScorecard
from schemas.improvement import (
    ImprovementConfig,
    ImprovementConfigDiff,
    ImprovementExperiment,
    ImprovementExperimentStatus,
    ImprovementProblem,
    ImprovementProblemStatus,
    ImprovementRegistryState,
    ImprovementReview,
    ImprovementReviewDecision,
    ImprovementReviewType,
    ImprovementRisk,
)


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


class ImprovementRegistry:
    """JSON-backed registry for problems, experiments, and human gates."""

    def __init__(self, config: Optional[Config] = None, registry_path: Optional[str] = None):
        self._config = config
        self._lock = threading.RLock()
        self._path = self._resolve_registry_path(registry_path)
        self._state = self._load()

    def _resolve_registry_path(self, registry_path: Optional[str]) -> Path:
        if registry_path:
            return Path(registry_path)
        if self._config is not None:
            improvement_cfg = self._config.get_improvement_config()
            return Path(self._config.get_absolute_path(improvement_cfg.registry_path))
        return Path("data/improvement_registry.json")

    def _load(self) -> ImprovementRegistryState:
        if not self._path.exists():
            return ImprovementRegistryState()
        with self._path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return ImprovementRegistryState(**raw)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            json.dump(self._state.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)

    def _get_cfg(self) -> ImprovementConfig:
        if self._config is not None:
            return self._config.get_improvement_config()
        return ImprovementConfig()

    def create_problem(
        self,
        *,
        title: str,
        summary: str,
        requirements: Optional[List[str]] = None,
        success_criteria: Optional[List[str]] = None,
        priority: str = "medium",
        source: str = "agent",
        owner: Optional[str] = None,
        related_paths: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ImprovementProblem:
        with self._lock:
            problem = ImprovementProblem(
                id=f"problem-{uuid.uuid4().hex[:8]}",
                title=title,
                summary=summary,
                requirements=requirements or [],
                success_criteria=success_criteria or [],
                priority=priority,
                source=source,
                owner=owner,
                related_paths=related_paths or [],
                metadata=metadata or {},
                status=(
                    ImprovementProblemStatus.REQUIREMENTS_DRAFT
                    if (requirements or success_criteria)
                    else ImprovementProblemStatus.OBSERVED
                ),
            )
            self._state.problems[problem.id] = problem
            self._save()
            return problem

    def list_problems(self, status: Optional[str] = None) -> List[ImprovementProblem]:
        with self._lock:
            problems = list(self._state.problems.values())
            if status:
                problems = [item for item in problems if item.status.value == status]
            return sorted(problems, key=lambda item: item.created_at, reverse=True)

    def list_experiments(self, status: Optional[str] = None) -> List[ImprovementExperiment]:
        with self._lock:
            experiments = list(self._state.experiments.values())
            if status:
                experiments = [item for item in experiments if item.status.value == status]
            return sorted(experiments, key=lambda item: item.created_at, reverse=True)

    def get_problem(self, problem_id: str) -> ImprovementProblem:
        with self._lock:
            if problem_id not in self._state.problems:
                raise KeyError(f"Problem '{problem_id}' not found")
            return self._state.problems[problem_id]

    def get_experiment(self, experiment_id: str) -> ImprovementExperiment:
        with self._lock:
            if experiment_id not in self._state.experiments:
                raise KeyError(f"Experiment '{experiment_id}' not found")
            return self._state.experiments[experiment_id]

    def create_experiment(
        self,
        *,
        problem_id: str,
        title: str,
        hypothesis: str,
        risk: str = "medium",
        change_type: str = "unspecified",
        allowed_paths: Optional[List[str]] = None,
        created_by: str = "agent",
        config_diff: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ImprovementExperiment:
        with self._lock:
            cfg = self._get_cfg()
            problem = self.get_problem(problem_id)
            open_experiments = [
                exp for exp in self._state.experiments.values()
                if exp.status not in (
                    ImprovementExperimentStatus.PROMOTED,
                    ImprovementExperimentStatus.REJECTED,
                    ImprovementExperimentStatus.ROLLED_BACK,
                )
            ]
            if len(open_experiments) >= cfg.max_open_experiments:
                raise ValueError("Maximum number of open experiments reached")
            if cfg.allowed_change_types and change_type not in cfg.allowed_change_types:
                raise ValueError(f"Change type '{change_type}' is not allowed by configuration")
            if cfg.require_human_requirements_review and not self._has_required_review(
                problem.reviews,
                ImprovementReviewType.REQUIREMENTS,
                reviewer_kind="human",
            ):
                raise ValueError("Human requirements review is required before creating an experiment")

            effective_paths = allowed_paths or problem.related_paths
            self._validate_allowed_paths(effective_paths)
            normalized_config_diff = self._normalize_config_diff(config_diff or [])

            experiment = ImprovementExperiment(
                id=f"experiment-{uuid.uuid4().hex[:8]}",
                problem_id=problem_id,
                title=title,
                hypothesis=hypothesis,
                risk=ImprovementRisk(risk),
                change_type=change_type,
                allowed_paths=effective_paths,
                config_diff=normalized_config_diff,
                created_by=created_by,
                metadata=metadata or {},
                status=(
                    ImprovementExperimentStatus.IMPLEMENTATION_READY
                    if not cfg.require_human_final_review
                    else ImprovementExperimentStatus.IN_REVIEW
                ),
            )
            self._state.experiments[experiment.id] = experiment
            problem.experiment_ids.append(experiment.id)
            problem.status = ImprovementProblemStatus.EXPERIMENT_ACTIVE
            problem.updated_at = _now_iso()
            self._save()
            return experiment

    def record_review(
        self,
        *,
        target_id: str,
        review_type: str,
        decision: str,
        reviewer: str,
        reviewer_kind: str,
        summary: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ImprovementReview:
        with self._lock:
            metadata = metadata or {}
            if reviewer_kind == "human" and metadata.get("approved_via") != "cli":
                raise ValueError("Human reviews must be recorded via the CLI approval flow")
            review = ImprovementReview(
                id=f"review-{uuid.uuid4().hex[:8]}",
                review_type=ImprovementReviewType(review_type),
                target_id=target_id,
                decision=ImprovementReviewDecision(decision),
                reviewer=reviewer,
                reviewer_kind=reviewer_kind,
                summary=summary,
                metadata=metadata,
            )
            if target_id in self._state.problems:
                problem = self._state.problems[target_id]
                problem.reviews.append(review)
                problem.updated_at = _now_iso()
                if (
                    review.review_type == ImprovementReviewType.REQUIREMENTS
                    and review.decision == ImprovementReviewDecision.APPROVED
                ):
                    problem.status = ImprovementProblemStatus.REQUIREMENTS_APPROVED
                elif review.decision == ImprovementReviewDecision.REJECTED:
                    problem.status = ImprovementProblemStatus.REJECTED
            elif target_id in self._state.experiments:
                experiment = self._state.experiments[target_id]
                experiment.reviews.append(review)
                experiment.updated_at = _now_iso()
                if review.review_type == ImprovementReviewType.FINAL:
                    if review.decision == ImprovementReviewDecision.APPROVED:
                        experiment.status = ImprovementExperimentStatus.PROMOTION_PENDING
                    elif review.decision == ImprovementReviewDecision.CHANGES_REQUESTED:
                        experiment.status = ImprovementExperimentStatus.IN_REVIEW
                    elif review.decision == ImprovementReviewDecision.REJECTED:
                        experiment.status = ImprovementExperimentStatus.REJECTED
            else:
                raise KeyError(f"Target '{target_id}' not found")
            self._save()
            return review

    def record_evaluation(
        self,
        *,
        experiment_id: str,
        baseline_scorecard: BenchmarkScorecard,
        candidate_scorecard: BenchmarkScorecard,
        comparison: Dict[str, Any],
        evaluator: str,
    ) -> ImprovementExperiment:
        with self._lock:
            experiment = self.get_experiment(experiment_id)
            experiment.baseline_scorecard = baseline_scorecard
            experiment.candidate_scorecard = candidate_scorecard
            experiment.evaluation_summary = {
                **comparison,
                "evaluator": evaluator,
                "recorded_at": _now_iso(),
            }
            experiment.metrics["evaluation"] = experiment.evaluation_summary
            experiment.updated_at = _now_iso()
            if comparison.get("passed"):
                experiment.status = (
                    ImprovementExperimentStatus.PROMOTION_PENDING
                    if self._get_cfg().require_human_final_review
                    else ImprovementExperimentStatus.IMPLEMENTATION_READY
                )
            else:
                experiment.status = ImprovementExperimentStatus.REJECTED
            self._save()
            return experiment

    def promote_experiment(
        self,
        *,
        experiment_id: str,
        promoter: str,
        notes: str = "",
    ) -> ImprovementExperiment:
        with self._lock:
            cfg = self._get_cfg()
            experiment = self.get_experiment(experiment_id)
            if cfg.require_benchmark_before_promotion:
                if experiment.candidate_scorecard is None:
                    raise ValueError("Benchmark evaluation is required before promotion")
                if not experiment.evaluation_summary.get("passed", False):
                    raise ValueError("Experiment did not pass benchmark evaluation")
            if cfg.require_human_final_review and not self._has_required_review(
                experiment.reviews,
                ImprovementReviewType.FINAL,
                reviewer_kind="human",
            ):
                raise ValueError("Human final review is required before promotion")
            if experiment.status not in (
                ImprovementExperimentStatus.PROMOTION_PENDING,
                ImprovementExperimentStatus.IMPLEMENTATION_READY,
            ):
                raise ValueError(
                    f"Experiment '{experiment_id}' is not ready for promotion (status={experiment.status.value})"
                )
            if not cfg.auto_promote_safe_changes:
                experiment.metadata.setdefault("promotion_gate", "manual_only")
            experiment.status = ImprovementExperimentStatus.CANARY
            experiment.updated_at = _now_iso()
            experiment.promotion_notes = notes or f"Canary started by {promoter}"
            experiment.metadata["canary"] = {
                "started_at": _now_iso(),
                "started_by": promoter,
                "notes": notes,
            }
            self._save()
            return experiment

    def complete_canary(
        self,
        *,
        experiment_id: str,
        promoter: str,
        notes: str = "",
        comparison: Optional[Dict[str, Any]] = None,
    ) -> ImprovementExperiment:
        with self._lock:
            experiment = self.get_experiment(experiment_id)
            if experiment.status != ImprovementExperimentStatus.CANARY:
                raise ValueError(
                    f"Experiment '{experiment_id}' is not in canary state (status={experiment.status.value})"
                )
            if experiment.config_diff:
                config_path = self._resolve_config_path()
                apply_result = apply_config_diff(config_path, experiment.config_diff)
                experiment.metadata["config_apply"] = apply_result
            experiment.status = ImprovementExperimentStatus.PROMOTED
            experiment.updated_at = _now_iso()
            experiment.promotion_notes = notes or f"Promoted by {promoter}"
            experiment.metadata["promotion"] = {
                "promoted_at": _now_iso(),
                "promoted_by": promoter,
                "notes": notes,
                "comparison": comparison or {},
            }
            problem = self.get_problem(experiment.problem_id)
            problem.status = ImprovementProblemStatus.SOLVED
            problem.updated_at = _now_iso()
            self._save()
            return experiment

    def record_runtime_check(
        self,
        *,
        experiment_id: str,
        phase: str,
        scorecard: BenchmarkScorecard,
        comparison: Dict[str, Any],
        trigger: str,
    ) -> ImprovementExperiment:
        with self._lock:
            experiment = self.get_experiment(experiment_id)
            experiment.metrics[phase] = {
                "scorecard": scorecard.model_dump(mode="json"),
                "comparison": comparison,
                "trigger": trigger,
                "recorded_at": _now_iso(),
            }
            experiment.updated_at = _now_iso()
            self._save()
            return experiment

    def rollback_experiment(
        self,
        *,
        experiment_id: str,
        trigger: str,
        reason: str,
        comparison: Optional[Dict[str, Any]] = None,
    ) -> ImprovementExperiment:
        with self._lock:
            experiment = self.get_experiment(experiment_id)
            if experiment.status not in (
                ImprovementExperimentStatus.CANARY,
                ImprovementExperimentStatus.PROMOTED,
            ):
                raise ValueError(
                    f"Experiment '{experiment_id}' cannot be rolled back from status={experiment.status.value}"
                )
            rollback_result = None
            if experiment.status == ImprovementExperimentStatus.PROMOTED and experiment.config_diff:
                config_path = self._resolve_config_path()
                rollback_result = revert_config_diff(config_path, experiment.config_diff)
            experiment.status = ImprovementExperimentStatus.ROLLED_BACK
            experiment.updated_at = _now_iso()
            experiment.metadata["rollback"] = {
                "trigger": trigger,
                "reason": reason,
                "comparison": comparison or {},
                "rolled_back_at": _now_iso(),
                "config_revert": rollback_result,
            }
            experiment.metrics["rollback"] = experiment.metadata["rollback"]
            experiment.reviews.append(
                ImprovementReview(
                    id=f"review-{uuid.uuid4().hex[:8]}",
                    review_type=ImprovementReviewType.FINAL,
                    target_id=experiment_id,
                    decision=ImprovementReviewDecision.REJECTED,
                    reviewer=trigger,
                    reviewer_kind="agent",
                    summary=reason,
                    metadata={"comparison": comparison or {}},
                )
            )
            problem = self.get_problem(experiment.problem_id)
            if problem.status == ImprovementProblemStatus.SOLVED:
                problem.status = ImprovementProblemStatus.EXPERIMENT_ACTIVE
            problem.updated_at = _now_iso()
            self._save()
            return experiment

    def reject_experiment(
        self,
        *,
        experiment_id: str,
        reviewer: str,
        reason: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ImprovementExperiment:
        with self._lock:
            experiment = self.get_experiment(experiment_id)
            experiment.status = ImprovementExperimentStatus.REJECTED
            experiment.updated_at = _now_iso()
            experiment.reviews.append(
                ImprovementReview(
                    id=f"review-{uuid.uuid4().hex[:8]}",
                    review_type=ImprovementReviewType.FINAL,
                    target_id=experiment_id,
                    decision=ImprovementReviewDecision.REJECTED,
                    reviewer=reviewer,
                    reviewer_kind="human",
                    summary=reason,
                    metadata=metadata or {},
                )
            )
            problem = self.get_problem(experiment.problem_id)
            problem.updated_at = _now_iso()
            self._save()
            return experiment

    def _validate_allowed_paths(self, paths: List[str]) -> None:
        cfg = self._get_cfg()
        if not cfg.allowed_paths or not paths:
            return
        for path in paths:
            if not any(path.startswith(prefix) for prefix in cfg.allowed_paths):
                raise ValueError(f"Path '{path}' is not allowed for experiments")

    def _normalize_config_diff(self, config_diff: List[Dict[str, Any]]) -> List[ImprovementConfigDiff]:
        if not config_diff:
            return []
        cfg = self._get_cfg()
        current_config = self._load_config_document()
        normalized: List[ImprovementConfigDiff] = []
        for raw_entry in config_diff:
            entry = ImprovementConfigDiff(**raw_entry)
            self._validate_config_key(entry.path, cfg)
            current_value = read_config_value(current_config, entry.path)
            if raw_entry.get("old", None) is None:
                entry.old = current_value
            normalized.append(entry)
        return normalized

    def _validate_config_key(self, dotted_path: str, cfg: ImprovementConfig) -> None:
        if not cfg.allowed_config_keys:
            return
        if not any(
            dotted_path == allowed_key or dotted_path.startswith(f"{allowed_key}.")
            for allowed_key in cfg.allowed_config_keys
        ):
            raise ValueError(f"Config key '{dotted_path}' is not allowed for experiments")

    def _load_config_document(self) -> Dict[str, Any]:
        config_path = Path(self._resolve_config_path())
        if not config_path.exists():
            return {}
        import yaml

        with config_path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def _resolve_config_path(self) -> str:
        if self._config is None:
            return "config.yaml"
        return str(self._config.config_path)

    @staticmethod
    def _has_required_review(
        reviews: List[ImprovementReview],
        review_type: ImprovementReviewType,
        *,
        reviewer_kind: str,
    ) -> bool:
        return any(
            review.review_type == review_type
            and review.decision == ImprovementReviewDecision.APPROVED
            and review.reviewer_kind == reviewer_kind
            for review in reviews
        )
