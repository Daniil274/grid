from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from web_chat.runtime import WebChatRuntime
from web_chat.systems import SystemRegistry

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


def test_catalog_action_policy_applies_to_web_factories(
    minimal_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    routing = minimal_config.parent / "routing.yaml"
    routing.write_text(
        f"""
settings:
  action_policy:
    mode: enforce
    prompts:
      action:
        instructions: Judge the proposed action against the policy.
        criteria:
          allow: The action is allowed.
          deny: The action is denied.
          review: The action needs review.
      chain:
        instructions: Judge the complete chain against the policy.
        criteria:
          allow: The chain is allowed.
          deny: The chain is denied.
          review: The chain needs review.
    validator:
      model: validator
providers:
  openrouter:
    name: openrouter
    base_url: https://example.com/v1
    api_key_env: TEST_OPENROUTER_KEY
models:
  validator:
    name: decisions-model
    provider: openrouter
routing:
  model: validator
  api: decisions
  default_system: app
  systems:
    app:
      config: {minimal_config.name}
""",
        encoding="utf-8",
    )

    runtime = WebChatRuntime(routing_path=str(routing))
    factory = runtime.registry.factory("app")

    assert factory.action_gate is not None
    assert factory.action_gate.config.mode == "enforce"
    assert factory.action_gate.validator.model.model_name == "decisions-model"


@pytest.mark.asyncio
async def test_routing_keeps_follow_ups_with_the_previous_agent():
    """An open selection routes every message, continuing where the last one went."""
    registry = _registry(agents={"assistant": "Talk", "engineer": "Code"})
    runtime = _runtime(
        registry, metadata={"routed_system": "solo", "routed_agent": "engineer"}
    )

    resolution = await runtime.resolve_turn("fix the build", context_id="coding")

    assert resolution.agent == "engineer"
    assert resolution.routed_agent is True
    assert registry.choose.call_args.kwargs["previous"] == "engineer"


@pytest.mark.asyncio
async def test_pinned_agent_is_never_routed():
    registry = _registry(agents={"assistant": "Talk", "engineer": "Code"})
    runtime = _runtime(registry, metadata={})

    resolution = await runtime.resolve_turn(
        "fix", agent_key="assistant", context_id="ctx"
    )

    assert (resolution.agent, resolution.routed_agent) == ("assistant", False)
    registry.choose.assert_not_awaited()


@pytest.mark.asyncio
async def test_routing_timeout_falls_back_to_the_default_agent(monkeypatch):
    registry = _registry(agents={"assistant": "Talk", "engineer": "Code"}, hang=True)
    monkeypatch.setattr("web_chat.systems.ROUTING_TIMEOUT_SECONDS", 0.01)
    runtime = _runtime(registry, metadata={})

    resolution = await runtime.resolve_turn("hello", context_id="ctx")

    assert resolution.agent == "assistant"


def _registry(*, agents: dict[str, str], hang: bool = False) -> SystemRegistry:
    """A single-system registry whose router is a stub we can inspect."""
    config = SimpleNamespace(
        config=SimpleNamespace(
            agents={
                key: SimpleNamespace(
                    routable=True, description=description, name=description
                )
                for key, description in agents.items()
            }
        ),
        config_path=Path("solo.yaml"),
        get_default_agent=lambda: next(iter(agents)),
        project_tools_loader=None,
    )

    async def never(*args, **kwargs):
        await asyncio.Event().wait()

    choose = (
        AsyncMock(side_effect=never) if hang else AsyncMock(return_value="engineer")
    )
    registry = object.__new__(SystemRegistry)
    registry._base_config = config
    registry._build_factory = lambda cfg: SimpleNamespace(config=cfg)
    registry._catalog = None
    registry._router = SimpleNamespace(router=SimpleNamespace(choose=choose))
    registry._factories = {}
    registry._base_key = "solo"
    registry.choose = choose
    return registry


def _runtime(registry: SystemRegistry, *, metadata: dict) -> WebChatRuntime:
    runtime = object.__new__(WebChatRuntime)
    runtime.registry = registry
    runtime.conversation_metadata = lambda context_id: metadata
    return runtime
