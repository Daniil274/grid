from pathlib import Path

import pytest

from web_chat.runtime import WebChatRuntime

MINIMAL_CONFIG = """
settings:
  default_agent: test_agent
  max_history: 10
  max_turns: 5
  agent_timeout: 60
  working_directory: .
  allow_path_override: true
  mcp_enabled: false
isolation:
  enabled: false
agents:
  test_agent:
    name: Test
    model: m1
    tools: []
models:
  m1:
    name: m1
    provider: openrouter
providers:
  openrouter:
    name: openrouter
    base_url: https://example.com/v1
    api_key_env: TEST_OPENROUTER_KEY
"""


@pytest.fixture
def minimal_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(MINIMAL_CONFIG, encoding="utf-8")
    return config_file


def test_runtime_uses_working_directory_for_sessions(
    minimal_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")

    workdir = minimal_config.parent / "workspace" / "user_test"
    logs_dir = workdir / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "context.json").write_text(
        '{"contexts": {}, "current_context_id": null}',
        encoding="utf-8",
    )

    runtime = WebChatRuntime(
        config_path=str(minimal_config),
        working_directory=str(workdir),
        user_id="test",
    )

    assert runtime.workspace_path.resolve() == workdir.resolve()
    assert runtime.persist_path.resolve() == logs_dir.resolve()
    assert runtime.context_manager().persist_path == logs_dir / "context.json"


def test_runtime_defaults_to_config_working_directory(
    minimal_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")

    runtime = WebChatRuntime(config_path=str(minimal_config))

    expected = (minimal_config.parent / ".").resolve()
    assert runtime.workspace_path.resolve() == expected
    assert runtime.persist_path.resolve() == (expected / "logs").resolve()
