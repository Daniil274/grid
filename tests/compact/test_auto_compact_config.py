"""Tests for config-driven auto compact thresholds."""

from core.compact import (
    calculate_token_warning_state,
    get_auto_compact_threshold,
    is_auto_compact_enabled,
)
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
