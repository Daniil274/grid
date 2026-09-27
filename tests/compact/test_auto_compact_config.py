"""Tests for config-driven auto compact thresholds."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.compact import (
    calculate_token_warning_state,
    get_auto_compact_threshold,
    is_auto_compact_enabled,
)
from schemas import CompactAutoConfig, CompactConfig, ModelConfig


def test_auto_compact_threshold_uses_config_buffers():
    compact_cfg = CompactConfig(
        auto=CompactAutoConfig(
            enabled=True,
            buffer_tokens=5000,
            max_output_tokens_for_summary=10000,
        )
    )

    # effective window = 128000 - 10000, threshold = 118000 - 5000
    assert get_auto_compact_threshold(128000, compact_cfg) == 113000


def test_warning_state_uses_model_context_window_and_config():
    compact_cfg = CompactConfig(
        auto=CompactAutoConfig(
            enabled=True,
            buffer_tokens=2000,
            warning_buffer_tokens=1000,
            error_buffer_tokens=1000,
            manual_buffer_tokens=250,
            max_output_tokens_for_summary=4000,
        )
    )

    state = calculate_token_warning_state(26000, 32000, compact_cfg)

    assert state.is_above_auto_compact_threshold is True
    assert state.is_above_warning_threshold is True
    assert state.is_above_error_threshold is True
    assert state.is_at_blocking_limit is False


def test_global_compact_disable_turns_off_auto_compact():
    compact_cfg = CompactConfig(enabled=False)
    assert is_auto_compact_enabled(compact_cfg) is False


EXAMPLES = sorted(Path("examples").glob("*/config.yaml"))
# Instructions and tool schemas are counted outside the estimate of a request.
SCHEMA_MARGIN_TOKENS = 4000


def _raw(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.parent.name)
def test_every_example_keeps_room_for_the_answer(path):
    """Compaction starts early enough that a request and its answer fit the window."""
    raw = _raw(path)
    assert "compact" not in (raw.get("settings") or {}), "compact belongs at the top level"
    compact_cfg = CompactConfig(**raw.get("compact", {}))
    assert compact_cfg.enabled and compact_cfg.auto.enabled and compact_cfg.micro.enabled
    models = raw.get("models") or {}
    for agent_key, agent in (raw.get("agents") or {}).items():
        keys = agent["model"] if isinstance(agent["model"], list) else [agent["model"]]
        for key in keys:
            model = ModelConfig(**models[key])
            threshold = get_auto_compact_threshold(model.context_window, compact_cfg)
            assert threshold + model.max_tokens + SCHEMA_MARGIN_TOKENS <= model.context_window, (
                f"{agent_key}/{key}: compacts at {threshold}, answers up to {model.max_tokens}, "
                f"window {model.context_window}"
            )


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.parent.name)
def test_every_compactable_tool_is_a_tool_of_that_system(path):
    """A misspelled name would silently never be compacted."""
    raw = _raw(path)
    tools = {tool for agent in (raw.get("agents") or {}).values() for tool in agent.get("tools") or []}
    tools |= set(((raw.get("settings") or {}).get("project_tools") or {}).get("base_tools") or [])
    compactable = set(CompactConfig(**raw.get("compact", {})).micro.compactable_tools)
    assert compactable <= tools, sorted(compactable - tools)


def test_the_routing_catalog_is_the_base_a_system_refines():
    from core.agent_factory import layered_compact

    def config(compact=None):
        fields = {"compact"} if compact is not None else set()
        grid = SimpleNamespace(compact=CompactConfig(**(compact or {})), model_fields_set=fields)
        return SimpleNamespace(config=grid)

    catalog = config({"auto": {"buffer_tokens": 10000}, "micro": {"preserve_last_n": 4}})
    system = config({"micro": {"preserve_last_n": 3, "compactable_tools": ["perceive"]}})

    layered = layered_compact(catalog, system)
    assert layered.auto.buffer_tokens == 10000  # from the catalog
    assert layered.micro.preserve_last_n == 3  # the system wins
    assert layered.micro.compactable_tools == ["perceive"]
    assert layered_compact(catalog, config()).auto.buffer_tokens == 10000
    assert layered_compact(None, system).auto.buffer_tokens == CompactConfig().auto.buffer_tokens
    catalog_raw = _raw(Path("routing.yaml"))
    assert get_auto_compact_threshold(40000, CompactConfig(**catalog_raw["compact"])) == 26000
