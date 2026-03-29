import json

import pytest

from core.config import Config
from core.config_proposer import ConfigProposer
from core.improvement_registry import ImprovementRegistry


def _build_config(config_file, temp_dir, sample_config):
    config_payload = {
        "settings": {
            "default_agent": "chat_agent",
            "working_directory": str(temp_dir),
            "max_history": 50,
        },
        "providers": {
            "lm-studio": {
                "name": "lm-studio",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "lm-studio",
            },
            "hf": {
                "name": "hf",
                "base_url": "https://router.huggingface.co/v1",
                "api_key": "hf-key",
            },
        },
        "models": {
            "kimi-k2.5-opencode": {
                "name": "kimi-k2.5",
                "provider": "lm-studio",
            },
            "glm-4.7-flash-local": {
                "name": "glm-4.7-flash-local",
                "provider": "lm-studio",
            },
            "minimax-m2.7": {
                "name": "minimax-m2.7",
                "provider": "hf",
            },
        },
        "agents": {
            "chat_agent": {
                "name": "Chat",
                "model": "kimi-k2.5-opencode",
                "tools": [],
            },
            "coordinator": {
                "name": "Coordinator",
                "model": "minimax-m2.7",
                "tools": [],
            },
        },
    }
    config_payload["improvement"] = {
        "enabled": True,
        "registry_path": "data/improvement_registry.json",
        "plans_directory": "plans",
        "require_human_requirements_review": False,
        "require_human_final_review": False,
        "allowed_change_types": ["refactor"],
        "allowed_paths": ["core/"],
        "allowed_config_keys": [
            "settings.max_history",
            "agents.chat_agent.model",
            "agents.coordinator.model",
            "providers.hf.base_url",
        ],
        "max_open_experiments": 5,
    }
    config_file.write_text(json.dumps(config_payload), encoding="utf-8")
    return Config(str(config_file))


def test_config_proposer_returns_no_safe_diff_for_provider_error(temp_dir, config_file, sample_config):
    config = _build_config(config_file, temp_dir, sample_config)
    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Provider failures for coordinator",
        summary="Provider outages are frequent.",
        related_paths=["core/"],
        metadata={"signal_subtype": "provider_error", "agent_name": "coordinator"},
    )

    proposer = ConfigProposer(config=config, registry=registry)
    with pytest.raises(ValueError, match="No safe config proposal available"):
        proposer.propose(problem.id, created_by="test")


def test_config_proposer_reduces_history_for_context_overflow(temp_dir, config_file, sample_config):
    config = _build_config(config_file, temp_dir, sample_config)
    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Context overflow",
        summary="History is too large.",
        related_paths=["core/"],
        metadata={"signal_subtype": "context_overflow", "agent_name": "chat_agent"},
    )

    proposer = ConfigProposer(config=config, registry=registry)
    experiment = proposer.propose(problem.id, created_by="test")

    assert experiment.config_diff[0].path == "settings.max_history"
    assert experiment.config_diff[0].new < experiment.config_diff[0].old


def test_config_proposer_returns_no_safe_diff_when_current_config_already_matches(temp_dir, config_file, sample_config):
    config = _build_config(config_file, temp_dir, sample_config)
    payload = json.loads(config_file.read_text(encoding="utf-8"))
    payload["providers"]["hf"]["base_url"] = "https://api-inference.huggingface.co/v1"
    config_file.write_text(json.dumps(payload), encoding="utf-8")
    config = Config(str(config_file))
    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Routing error",
        summary="Need supported local model.",
        related_paths=["core/"],
        metadata={"signal_subtype": "routing_error", "agent_name": "coordinator"},
    )

    proposer = ConfigProposer(config=config, registry=registry)
    first = proposer.propose(problem.id, created_by="test")
    assert first.config_diff[0].path == "providers.hf.base_url"
    assert first.config_diff[0].new == "https://router.huggingface.co/v1"

    # simulate that the config has already been updated to the proposed value
    updated_payload = json.loads(config_file.read_text(encoding="utf-8"))
    updated_payload["providers"]["hf"]["base_url"] = "https://router.huggingface.co/v1"
    config_file.write_text(json.dumps(updated_payload), encoding="utf-8")
    updated_config = Config(str(config_file))
    updated_registry = ImprovementRegistry(config=updated_config)
    updated_problem = updated_registry.create_problem(
        title="Routing error resolved in config",
        summary="No further diff should be needed.",
        related_paths=["core/"],
        metadata={"signal_subtype": "routing_error", "agent_name": "coordinator"},
    )
    updated_proposer = ConfigProposer(config=updated_config, registry=updated_registry)

    with pytest.raises(ValueError, match="No safe config proposal available"):
        updated_proposer.propose(updated_problem.id, created_by="test")


def test_config_proposer_returns_no_safe_diff_for_tool_schema_error(temp_dir, config_file, sample_config):
    config = _build_config(config_file, temp_dir, sample_config)
    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Malformed coordinator tool calls",
        summary="Tool schema errors should not trigger model swaps.",
        related_paths=["core/"],
        metadata={"signal_subtype": "tool_schema_error", "agent_name": "coordinator"},
    )

    proposer = ConfigProposer(config=config, registry=registry)
    with pytest.raises(ValueError, match="No safe config proposal available"):
        proposer.propose(problem.id, created_by="test")


def test_config_proposer_can_auto_evaluate_experiment(temp_dir, config_file, sample_config):
    config = _build_config(config_file, temp_dir, sample_config)
    payload = json.loads(config_file.read_text(encoding="utf-8"))
    payload["providers"]["hf"]["base_url"] = "https://api-inference.huggingface.co/v1"
    config_file.write_text(json.dumps(payload), encoding="utf-8")
    config = Config(str(config_file))
    registry = ImprovementRegistry(config=config)
    problem = registry.create_problem(
        title="Routing error",
        summary="Need supported Hugging Face router URL.",
        related_paths=["core/"],
        metadata={"signal_subtype": "routing_error", "agent_name": "coordinator"},
    )

    class StubEvaluator:
        def __init__(self):
            self.called_with = None

        def evaluate_experiment(self, experiment_id: str):
            self.called_with = experiment_id
            experiment = registry.get_experiment(experiment_id)
            experiment.evaluation_summary = {"passed": True}
            return experiment

    evaluator = StubEvaluator()
    proposer = ConfigProposer(config=config, registry=registry, evaluator=evaluator)
    experiment = proposer.propose(problem.id, created_by="test", evaluate_after_create=True)

    assert evaluator.called_with == experiment.id
    assert experiment.evaluation_summary == {"passed": True}
