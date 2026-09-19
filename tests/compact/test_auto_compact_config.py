"""Tests for config-driven auto compact thresholds."""

from pathlib import Path

from core.compact import (
    calculate_token_warning_state,
    get_auto_compact_threshold,
    is_auto_compact_enabled,
)
from core.config import Config
from schemas import CompactAutoConfig, CompactConfig


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


def test_claude_tools_example_uses_expected_compact_thresholds():
    config = Config(str(Path("examples/claude-tools/config.yaml")))

    model_keys = ("glm-latest", "deepseek-flash-latest", "mercury-2.5")
    compact_cfg = config.config.compact

    for model_key in model_keys:
        model = config.get_model(model_key)
        assert model.provider == "openrouter"
        assert get_auto_compact_threshold(model.context_window, compact_cfg) == 33000

    assert config.get_model("glm-latest").name == "~z-ai/glm-latest"
    assert (
        config.get_model("deepseek-flash-latest").name
        == "~deepseek/deepseek-flash-latest"
    )
    assert config.get_model("mercury-2.5").name == "inception/mercury-2.5"
    assert config.get_agent("engineer").model == "glm-latest"
    assert config.get_agent("general_purpose_glm").model == "deepseek-flash-latest"
    assert config.get_agent("web_spider").model == "mercury-2.5"
