"""Safe tools for staged system self-improvement management."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agents import RunContextWrapper, function_tool

from core.improvement.evaluator import ExperimentEvaluator
from core.improvement.monitor import ImprovementMonitor
from core.improvement.proposer import ConfigProposer
from core.improvement.registry import ImprovementRegistry


def _get_factory_from_context(context: RunContextWrapper) -> Any:
    raw = getattr(context, "context", None)
    return getattr(raw, "factory", None)


def _get_registry(context: RunContextWrapper) -> ImprovementRegistry:
    factory = _get_factory_from_context(context)
    config = getattr(factory, "config", None) if factory is not None else None
    return ImprovementRegistry(config=config)


def _parse_config_diff_json(config_diff_json: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    if not config_diff_json:
        return None
    raw = json.loads(config_diff_json)
    if not isinstance(raw, list):
        raise ValueError("config_diff_json must decode to a list of diff objects")
    if not all(isinstance(item, dict) for item in raw):
        raise ValueError("config_diff_json must decode to a list of objects")
    return raw


@function_tool
def create_improvement_problem(
    context: RunContextWrapper,
    title: str,
    summary: str,
    requirements: Optional[List[str]] = None,
    success_criteria: Optional[List[str]] = None,
    priority: str = "medium",
    source: str = "agent",
    owner: Optional[str] = None,
    related_paths: Optional[List[str]] = None,
) -> str:
    """Create a tracked improvement problem with requirements and success criteria."""
    registry = _get_registry(context)
    problem = registry.create_problem(
        title=title,
        summary=summary,
        requirements=requirements,
        success_criteria=success_criteria,
        priority=priority,
        source=source,
        owner=owner,
        related_paths=related_paths,
    )
    return json.dumps(problem.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def list_improvement_problems(
    context: RunContextWrapper,
    status: Optional[str] = None,
) -> str:
    """List registered improvement problems, optionally filtered by status."""
    registry = _get_registry(context)
    problems = registry.list_problems(status=status)
    return json.dumps([item.model_dump(mode="json") for item in problems], ensure_ascii=False, indent=2)


@function_tool
def create_improvement_experiment(
    context: RunContextWrapper,
    problem_id: str,
    title: str,
    hypothesis: str,
    risk: str = "medium",
    change_type: str = "unspecified",
    allowed_paths: Optional[List[str]] = None,
    config_diff_json: Optional[str] = None,
    created_by: str = "agent",
) -> str:
    """Create a constrained experiment for a previously approved improvement problem."""
    registry = _get_registry(context)
    experiment = registry.create_experiment(
        problem_id=problem_id,
        title=title,
        hypothesis=hypothesis,
        risk=risk,
        change_type=change_type,
        allowed_paths=allowed_paths,
        config_diff=_parse_config_diff_json(config_diff_json),
        created_by=created_by,
    )
    return json.dumps(experiment.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def propose_config_experiment(
    context: RunContextWrapper,
    problem_id: str,
    created_by: str = "agent",
    evaluate_after_create: bool = False,
) -> str:
    """Create a bounded config-diff experiment from an observed problem."""
    registry = _get_registry(context)
    factory = _get_factory_from_context(context)
    config = getattr(factory, "config", None) if factory is not None else None
    if config is None:
        raise ValueError("Config proposer requires factory config in the run context")
    proposer = ConfigProposer(config=config, registry=registry)
    experiment = proposer.propose(
        problem_id,
        created_by=created_by,
        evaluate_after_create=evaluate_after_create,
    )
    return json.dumps(experiment.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def evaluate_improvement_experiment(
    context: RunContextWrapper,
    experiment_id: str,
) -> str:
    """Run benchmark evaluation for an improvement experiment and store scorecards."""
    registry = _get_registry(context)
    factory = _get_factory_from_context(context)
    config = getattr(factory, "config", None) if factory is not None else None
    if config is None:
        raise ValueError("Experiment evaluation requires factory config in the run context")
    evaluator = ExperimentEvaluator(config=config, registry=registry)
    experiment = evaluator.evaluate_experiment(experiment_id)
    return json.dumps(experiment.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def record_requirement_review(
    context: RunContextWrapper,
    problem_id: str,
    decision: str,
    reviewer: str,
    summary: str,
    reviewer_kind: str = "agent",
) -> str:
    """Record a requirement review decision for an improvement problem."""
    registry = _get_registry(context)
    review = registry.record_review(
        target_id=problem_id,
        review_type="requirements",
        decision=decision,
        reviewer=reviewer,
        reviewer_kind=reviewer_kind,
        summary=summary,
    )
    return json.dumps(review.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def record_final_review(
    context: RunContextWrapper,
    experiment_id: str,
    decision: str,
    reviewer: str,
    summary: str,
    reviewer_kind: str = "agent",
) -> str:
    """Record the final review decision for an experiment before promotion."""
    registry = _get_registry(context)
    review = registry.record_review(
        target_id=experiment_id,
        review_type="final",
        decision=decision,
        reviewer=reviewer,
        reviewer_kind=reviewer_kind,
        summary=summary,
    )
    return json.dumps(review.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def promote_improvement_experiment(
    context: RunContextWrapper,
    experiment_id: str,
    promoter: str,
    notes: str = "",
) -> str:
    """Start staged rollout for an experiment after required review gates have passed."""
    registry = _get_registry(context)
    experiment = registry.promote_experiment(
        experiment_id=experiment_id,
        promoter=promoter,
        notes=notes,
    )
    return json.dumps(experiment.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def reject_improvement_experiment(
    context: RunContextWrapper,
    experiment_id: str,
    reviewer: str,
    reason: str,
) -> str:
    """Reject an experiment and keep the registry history for future analysis."""
    registry = _get_registry(context)
    experiment = registry.reject_experiment(
        experiment_id=experiment_id,
        reviewer=reviewer,
        reason=reason,
    )
    return json.dumps(experiment.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def run_improvement_canary(
    context: RunContextWrapper,
    experiment_id: str,
) -> str:
    """Run canary benchmark for an experiment and promote or roll back automatically."""
    registry = _get_registry(context)
    factory = _get_factory_from_context(context)
    config = getattr(factory, "config", None) if factory is not None else None
    if config is None:
        raise ValueError("Canary runner requires factory config in the run context")
    monitor = ImprovementMonitor(config=config, registry=registry)
    experiment = monitor.run_canary(experiment_id)
    return json.dumps(experiment.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def monitor_promoted_improvement(
    context: RunContextWrapper,
    experiment_id: str,
) -> str:
    """Run post-promotion benchmark and roll back automatically on regression."""
    registry = _get_registry(context)
    factory = _get_factory_from_context(context)
    config = getattr(factory, "config", None) if factory is not None else None
    if config is None:
        raise ValueError("Improvement monitor requires factory config in the run context")
    monitor = ImprovementMonitor(config=config, registry=registry)
    experiment = monitor.monitor_experiment(experiment_id)
    return json.dumps(experiment.model_dump(mode="json"), ensure_ascii=False, indent=2)


EVOLUTION_TOOLS: Dict[str, Any] = {
    "create_improvement_problem": create_improvement_problem,
    "list_improvement_problems": list_improvement_problems,
    "create_improvement_experiment": create_improvement_experiment,
    "propose_config_experiment": propose_config_experiment,
    "evaluate_improvement_experiment": evaluate_improvement_experiment,
    "record_requirement_review": record_requirement_review,
    "record_final_review": record_final_review,
    "promote_improvement_experiment": promote_improvement_experiment,
    "reject_improvement_experiment": reject_improvement_experiment,
    "run_improvement_canary": run_improvement_canary,
    "monitor_promoted_improvement": monitor_promoted_improvement,
}
