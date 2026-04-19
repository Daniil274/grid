"""Heuristic config proposer for Stage 3 improvement experiments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.config.config import Config
from core.improvement.config_apply import read_config_value
from core.improvement.evaluator import ExperimentEvaluator
from core.improvement.registry import ImprovementRegistry
from schemas.improvement import ImprovementExperiment, ImprovementProblem


@dataclass
class ProposalCandidate:
    title: str
    hypothesis: str
    change_type: str
    allowed_paths: List[str]
    config_diff: List[Dict[str, Any]]
    metadata: Dict[str, Any]


class ConfigProposer:
    """Create bounded config diff experiments from observed problems."""

    def __init__(
        self,
        *,
        config: Config,
        registry: Optional[ImprovementRegistry] = None,
        evaluator: Optional[ExperimentEvaluator] = None,
    ):
        self.config = config
        self.registry = registry or ImprovementRegistry(config=config)
        self.evaluator = evaluator
        self._config_document = self._load_config_document()

    def propose(
        self,
        problem_id: str,
        *,
        created_by: str = "agent",
        evaluate_after_create: Optional[bool] = None,
    ) -> ImprovementExperiment:
        problem = self.registry.get_problem(problem_id)
        candidate = self._build_candidate(problem)
        if candidate is None:
            raise ValueError(f"No safe config proposal available for problem '{problem_id}'")
        experiment = self.registry.create_experiment(
            problem_id=problem.id,
            title=candidate.title,
            hypothesis=candidate.hypothesis,
            change_type=candidate.change_type,
            allowed_paths=candidate.allowed_paths,
            created_by=created_by,
            config_diff=candidate.config_diff,
            metadata=candidate.metadata,
        )
        should_evaluate = evaluate_after_create
        if should_evaluate is None:
            should_evaluate = self.config.get_improvement_config().auto_evaluate_proposed_experiments
        if should_evaluate:
            evaluator = self.evaluator or ExperimentEvaluator(config=self.config, registry=self.registry)
            experiment = evaluator.evaluate_experiment(experiment.id)
        return experiment

    def _build_candidate(self, problem: ImprovementProblem) -> Optional[ProposalCandidate]:
        subtype = problem.metadata.get("signal_subtype")
        agent_name = problem.metadata.get("agent_name")

        if subtype == "provider_error":
            return self._proposal_for_provider_error(problem, agent_name)
        if subtype == "routing_error":
            return self._proposal_for_routing_error(problem, agent_name)
        if subtype == "context_overflow":
            return self._proposal_for_context_overflow(problem)
        if subtype == "tool_schema_error":
            return self._proposal_for_tool_schema_error(problem, agent_name)
        return None

    def _proposal_for_provider_error(
        self,
        problem: ImprovementProblem,
        agent_name: Optional[str],
    ) -> Optional[ProposalCandidate]:
        return None

    def _proposal_for_routing_error(
        self,
        problem: ImprovementProblem,
        agent_name: Optional[str],
    ) -> Optional[ProposalCandidate]:
        hf_router = "https://router.huggingface.co/v1"
        diff = self._single_diff("providers.hf.base_url", hf_router)
        if diff:
            return ProposalCandidate(
                title="Normalize Hugging Face provider base URL",
                hypothesis="Using the supported Hugging Face router URL will remove stale routing failures.",
                change_type="refactor",
                allowed_paths=["core/"],
                config_diff=[diff],
                metadata={"proposed_by": "config_proposer", "strategy": "provider_base_url_fix"},
            )
        return None

    def _proposal_for_context_overflow(self, problem: ImprovementProblem) -> Optional[ProposalCandidate]:
        current_max_history = read_config_value(self._config_document, "settings.max_history")
        if not isinstance(current_max_history, int):
            return None
        proposed = max(10, min(current_max_history - 10, 30))
        diff = self._single_diff("settings.max_history", proposed)
        if not diff:
            return None
        return ProposalCandidate(
            title="Reduce max_history for large-context failures",
            hypothesis="Reducing retained history will lower payload size and avoid context overflow.",
            change_type="refactor",
            allowed_paths=["core/"],
            config_diff=[diff],
            metadata={"proposed_by": "config_proposer", "strategy": "reduce_history_window"},
        )

    def _proposal_for_tool_schema_error(
        self,
        problem: ImprovementProblem,
        agent_name: Optional[str],
    ) -> Optional[ProposalCandidate]:
        return None

    def _single_diff(self, dotted_path: str, new_value: Any) -> Optional[Dict[str, Any]]:
        current_value = read_config_value(self._config_document, dotted_path)
        if current_value == new_value:
            return None
        return {"path": dotted_path, "old": current_value, "new": new_value}

    def _load_config_document(self) -> Dict[str, Any]:
        import yaml

        with open(self.config.config_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    @staticmethod
    def _normalize_agent_name(agent_name: Optional[str]) -> Optional[str]:
        if not agent_name:
            return None
        if agent_name.startswith("tool:"):
            return None
        return agent_name
