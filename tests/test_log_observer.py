import json

from core.config import Config
from core.improvement.registry import ImprovementRegistry
from core.improvement.log_observer import LogObserver


def _write_context_log(base_dir, contexts):
    payload = {
        "active_context_id": next(iter(contexts.keys()), None),
        "contexts": contexts,
    }
    (base_dir / "logs" / "context.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_grid_log(base_dir, lines):
    text = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
    (base_dir / "logs" / "grid.log").write_text(text, encoding="utf-8")


def _build_config(config_file, temp_dir, sample_config):
    sample_config["settings"]["working_directory"] = str(temp_dir)
    sample_config["improvement"] = {
        "enabled": True,
        "registry_path": "data/improvement_registry.json",
        "plans_directory": "plans",
        "require_human_requirements_review": False,
        "require_human_final_review": False,
        "allowed_change_types": ["logging", "refactor", "tests", "diagnostics"],
        "allowed_paths": ["config.yaml", "core/", "tests/", "plans/", "tools/", "schemas/"],
        "max_open_experiments": 5,
    }
    config_file.write_text(json.dumps(sample_config), encoding="utf-8")
    return Config(str(config_file))


def test_log_observer_creates_problem_from_repeated_user_corrections(temp_dir, config_file, sample_config):
    (temp_dir / "logs").mkdir(parents=True, exist_ok=True)
    contexts = {}
    for index in range(3):
        context_id = f"ctx-correct-{index}"
        contexts[context_id] = {
            "conversation_history": [
                {"role": "user", "content": "Make it brief", "timestamp": "2026-03-20T10:00:00"},
                {"role": "assistant", "content": "Very long answer", "timestamp": "2026-03-20T10:00:01"},
                {"role": "user", "content": "no, that's not it", "timestamp": "2026-03-20T10:00:02"},
            ],
            "execution_history": [],
            "metadata": {"last_invocation": {"agent": "chat_agent"}},
            "created_at": "2026-03-20T10:00:00",
            "updated_at": "2026-03-20T10:00:03",
        }
    _write_context_log(temp_dir, contexts)
    _write_grid_log(temp_dir, [])

    config = _build_config(config_file, temp_dir, sample_config)
    registry = ImprovementRegistry(config=config)
    observer = LogObserver(config=config, registry=registry, logs_directory="logs", min_occurrences=3)

    result = observer.observe()

    assert len(result.created) == 1
    problem = result.created[0]
    assert problem.source == "observer"
    assert problem.metadata["signal_type"] == "user_correction"
    assert problem.metadata["context_count"] == 3


def test_log_observer_deduplicates_on_second_run(temp_dir, config_file, sample_config):
    (temp_dir / "logs").mkdir(parents=True, exist_ok=True)
    contexts = {}
    for index in range(3):
        context_id = f"ctx-fail-{index}"
        contexts[context_id] = {
            "conversation_history": [],
            "execution_history": [
                {
                    "agent_name": "coordinator",
                    "output": "Cannot complete the task due to a tool error",
                    "error": None,
                    "end_time": "2026-03-20T10:00:03",
                }
            ],
            "metadata": {"last_invocation": {"agent": "coordinator"}},
            "created_at": "2026-03-20T10:00:00",
            "updated_at": "2026-03-20T10:00:03",
        }
    _write_context_log(temp_dir, contexts)
    _write_grid_log(temp_dir, [])

    config = _build_config(config_file, temp_dir, sample_config)
    registry = ImprovementRegistry(config=config)
    observer = LogObserver(config=config, registry=registry, logs_directory="logs", min_occurrences=3)

    first = observer.observe()
    second = observer.observe()

    assert len(first.created) == 1
    assert len(second.created) == 0
    assert len(registry.list_problems()) == 1
    assert first.created[0].metadata["signal_subtype"] == "provider_error"


def test_log_observer_handles_timeout_logs_and_malformed_files(temp_dir, config_file, sample_config):
    (temp_dir / "logs").mkdir(parents=True, exist_ok=True)
    _write_context_log(temp_dir, {})
    (temp_dir / "logs" / "grid.log").write_text(
        "\n".join(
            [
                "not-json",
                json.dumps(
                    {
                        "timestamp": "2026-03-20T10:00:00",
                        "message": "Agent execution timed out for chat_agent context ctx-timeout-1",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-20T10:00:30",
                        "message": "MCP tool 'write_file' got invalid JSON from model: {'error': 'Execution timed out'}",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-20T10:01:00",
                        "message": "Agent execution timed out for chat_agent context ctx-timeout-2",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-20T10:02:00",
                        "message": "max_turns exceeded for chat_agent context ctx-timeout-3",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    config = _build_config(config_file, temp_dir, sample_config)
    registry = ImprovementRegistry(config=config)
    observer = LogObserver(config=config, registry=registry, logs_directory="logs", min_occurrences=3)

    result = observer.observe()

    assert len(result.created) == 1
    assert result.created[0].metadata["signal_type"] == "model_timeout"


def test_log_observer_classifies_failure_subtypes(temp_dir, config_file, sample_config):
    (temp_dir / "logs").mkdir(parents=True, exist_ok=True)
    contexts = {}
    for index in range(3):
        contexts[f"ctx-json-{index}"] = {
            "conversation_history": [],
            "execution_history": [
                {
                    "agent_name": "coordinator",
                    "error": "Agent execution failed: Invalid JSON input for tool execute_command: {\"command\":\"bad\"}",
                    "end_time": "2026-03-20T10:00:03",
                }
            ],
            "metadata": {"last_invocation": {"agent": "coordinator"}},
            "created_at": "2026-03-20T10:00:00",
            "updated_at": "2026-03-20T10:00:03",
        }
        contexts[f"ctx-route-{index}"] = {
            "conversation_history": [],
            "execution_history": [
                {
                    "agent_name": "chat_agent",
                    "error": "Failed to create agent 'chat_agent': API key not found for provider 'hf'",
                    "end_time": "2026-03-20T10:00:03",
                }
            ],
            "metadata": {"last_invocation": {"agent": "chat_agent"}},
            "created_at": "2026-03-20T10:00:00",
            "updated_at": "2026-03-20T10:00:03",
        }
    _write_context_log(temp_dir, contexts)
    _write_grid_log(temp_dir, [])

    config = _build_config(config_file, temp_dir, sample_config)
    registry = ImprovementRegistry(config=config)
    observer = LogObserver(config=config, registry=registry, logs_directory="logs", min_occurrences=3)

    result = observer.observe()

    assert len(result.created) == 2
    subtypes = {problem.metadata["signal_subtype"] for problem in result.created}
    assert subtypes == {"tool_schema_error", "routing_error"}
