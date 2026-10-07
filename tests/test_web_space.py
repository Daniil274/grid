from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from web_chat.deployment import Deployment
from web_chat.space import SpaceLayout, UserSpace, WorkspaceChoiceError
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


def test_single_user_space_keeps_conversations_in_the_workspace_logs(
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

    space = UserSpace(
        Deployment(config_path=str(minimal_config), working_directory=str(workdir)),
        user_id="test",
    )

    assert space.workspace_path.resolve() == workdir.resolve()
    assert space.conversations_path.resolve() == (logs_dir / "context.json").resolve()
    assert space.context_manager().persist_path == logs_dir / "context.json"


def test_single_user_space_defaults_to_config_working_directory(
    minimal_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")

    space = UserSpace(Deployment(config_path=str(minimal_config)))

    expected = (minimal_config.parent / ".").resolve()
    assert space.workspace_path.resolve() == expected
    assert space.conversations_path.resolve() == (expected / "logs" / "context.json").resolve()


def test_a_laid_out_space_keeps_its_records_beside_its_workspace(
    minimal_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user's conversations and agent sessions are outside the agents' workspace."""
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    root = minimal_config.parent / "users" / "u1"

    space = UserSpace(Deployment(config_path=str(minimal_config)), user_id="u1", layout=SpaceLayout.under(root))
    factory = space.registry.factory(space.registry.default_key())

    assert space.workspace_path == (root / "workspace").resolve()
    assert space.conversations_path == root / "conversations.json"
    assert factory._agent_session_db_path == str(root / "agent_sessions.db")
    assert factory._logs_directory_path() == root / "logs"
    assert Path(factory.config.get_working_directory()).resolve() == (root / "workspace").resolve()
    assert not (root / "workspace" / "logs").exists()


def test_missing_routing_catalog_does_not_fall_back_to_root_config(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="Routing catalog not found"):
        Deployment(routing_path=str(tmp_path / "missing.yaml"))


def test_single_system_requires_explicit_config(minimal_config: Path) -> None:
    deployment = Deployment(config_path=str(minimal_config), routing_path=None)
    assert deployment.config_path == minimal_config.resolve()


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

    space = UserSpace(Deployment(routing_path=str(routing)))
    factory = space.registry.factory("app")

    assert factory.action_gate is not None
    assert factory.action_gate.config.mode == "enforce"
    assert factory.action_gate.validator.model.model_name == "decisions-model"


@pytest.mark.asyncio
async def test_routing_keeps_follow_ups_with_the_previous_agent():
    """An open selection routes every message, continuing where the last one went."""
    registry = _registry(agents={"assistant": "Talk", "engineer": "Code"})
    space = _space(
        registry, metadata={"routed_system": "solo", "routed_agent": "engineer"}
    )

    resolution = await space.resolve_turn("fix the build", context_id="coding")

    assert resolution.agent == "engineer"
    assert resolution.routed_agent is True
    assert registry.choose.call_args.kwargs["previous"] == "engineer"


@pytest.mark.asyncio
async def test_pinned_agent_is_never_routed():
    registry = _registry(agents={"assistant": "Talk", "engineer": "Code"})
    space = _space(registry, metadata={})

    resolution = await space.resolve_turn(
        "fix", agent_key="assistant", context_id="ctx"
    )

    assert (resolution.agent, resolution.routed_agent) == ("assistant", False)
    registry.choose.assert_not_awaited()


@pytest.mark.asyncio
async def test_routing_timeout_falls_back_to_the_default_agent(monkeypatch):
    registry = _registry(agents={"assistant": "Talk", "engineer": "Code"}, hang=True)
    monkeypatch.setattr("web_chat.systems.ROUTING_TIMEOUT_SECONDS", 0.01)
    space = _space(registry, metadata={})

    resolution = await space.resolve_turn("hello", context_id="ctx")

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
    registry._customize = None
    registry._customized = {}
    registry._catalog = None
    registry._router = SimpleNamespace(router=SimpleNamespace(choose=choose))
    registry._factories = {}
    registry._base_key = "solo"
    registry._extras_source = None
    registry._extras = {}
    registry._extra_configs = {}
    registry.choose = choose
    return registry


def _space(registry: SystemRegistry, *, metadata: dict) -> UserSpace:
    space = object.__new__(UserSpace)
    space.registry = registry
    space.conversation_metadata = lambda context_id: metadata
    # A shared server's space: every chat works in the space's workspace.
    space.layout = SpaceLayout.under(Path("/srv/users/u"))
    space.workspace_path = space.layout.workspace
    return space


ROUTING_WITH_VOICE = """
providers:
  openrouter:
    name: openrouter
    base_url: https://example.com/v1
    api_key_env: TEST_OPENROUTER_KEY
models:
  router:
    name: decisions-model
    provider: openrouter
routing:
  model: router
  api: decisions
  default_system: app
  systems:
    app:
      config: {config}
voice:
  decision_model: router
"""


def test_voice_settings_come_from_the_catalog_when_routing(minimal_config, monkeypatch):
    """The default system's config has no voice section; the catalog's is used."""
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    routing = minimal_config.parent / "routing.yaml"
    routing.write_text(ROUTING_WITH_VOICE.format(config=minimal_config.name), encoding="utf-8")

    deployment = Deployment(routing_path=str(routing))

    path, source = deployment.voice_source()
    assert path == routing.resolve() or path == routing
    assert deployment.voice_config_dict()["voice"]["decision_model"] == "router"
    assert source.get_model("router").name == "decisions-model"


def test_a_single_system_keeps_its_own_voice_settings(minimal_config, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    deployment = Deployment(config_path=str(minimal_config), routing_path=None)
    path, source = deployment.voice_source()
    assert path == deployment.config_path
    assert source is deployment.config


def test_voice_is_off_unless_the_config_turns_it_on(minimal_config, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    assert Deployment(config_path=str(minimal_config), routing_path=None).voice_enabled() is False

    minimal_config.write_text(minimal_config.read_text(encoding="utf-8") + "voice:\n  enabled: true\n", encoding="utf-8")

    assert Deployment(config_path=str(minimal_config), routing_path=None).voice_enabled() is True


class _Containers:
    """ContainerManager stand-in: records whether a container was asked for."""

    started = []

    def __init__(self, config, *, enabled=None):
        self.enabled = enabled if enabled is not None else config.config.isolation.enabled

    def get_or_create_container(self, user_id, workspace=None):
        _Containers.started.append(user_id)
        return SimpleNamespace(id=f"c-{user_id}", name=f"grid-agent-{user_id}")


def test_a_space_that_requires_isolation_gets_a_container_whatever_the_config_says(minimal_config, monkeypatch):
    """Isolation belongs to the server with accounts, not to the config's flag."""
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    monkeypatch.setattr("web_chat.space.ContainerManager", _Containers)
    _Containers.started = []
    deployment = Deployment(config_path=str(minimal_config), routing_path=None)  # isolation.enabled: false

    isolated = UserSpace(deployment, user_id="u1", layout=SpaceLayout.under(minimal_config.parent / "u1"), require_isolation=True)
    single = UserSpace(deployment)

    assert isolated.container_id == "c-u1"
    assert single.container_id is None
    assert _Containers.started == ["u1"]


class _WorkspaceContainers(_Containers):
    """Records the workspace each container was asked for."""

    workspaces = []

    def get_or_create_container(self, user_id, workspace=None):
        _WorkspaceContainers.workspaces.append(Path(workspace))
        return SimpleNamespace(id=f"c-{Path(workspace).name}", name=f"grid-agent-{Path(workspace).name}")

    def stop_container(self, user_id, container_id):
        pass


def _isolating(minimal_config: Path) -> Path:
    minimal_config.write_text(
        MINIMAL_CONFIG.replace("isolation:\n  enabled: false", "isolation:\n  enabled: true"), encoding="utf-8"
    )
    return minimal_config


@pytest.mark.asyncio
async def test_chats_work_in_the_directories_chosen_for_them(minimal_config, monkeypatch, tmp_path):
    """Two chats in two directories: each its own config, factories and container."""
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    monkeypatch.setattr("web_chat.space.ContainerManager", _WorkspaceContainers)
    _WorkspaceContainers.workspaces = []
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir(), project.mkdir()
    space = UserSpace(Deployment(config_path=str(_isolating(minimal_config)), working_directory=str(home)))
    manager = space.context_manager()
    plain, chosen = manager.start_new_context(), manager.start_new_context()

    assert space.can_choose_workspace
    assert space.choose_workspace(chosen, str(project)) == project.resolve()

    assert space.conversation_workspace(plain) == space.workspace_path
    assert space.conversation_workspace(chosen) == project.resolve()
    assert await space.registry_for(plain) is space.registry
    other = await space.registry_for(chosen)
    assert other is not space.registry and await space.registry_for(chosen) is other
    assert other.config(other.default_key()).get_working_directory() == str(project.resolve())
    assert other.factory(other.default_key()).container_id == "c-project"
    assert space.registry.factory(space.registry.default_key()).container_id == "c-home"
    assert _WorkspaceContainers.workspaces == [home.resolve(), project.resolve()]
    await space.close()


def test_a_chat_keeps_its_directory_once_it_has_messages(minimal_config, monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    space = UserSpace(Deployment(config_path=str(minimal_config), working_directory=str(tmp_path)))
    manager = space.context_manager()
    chat = manager.start_new_context()
    manager.add_message("user", "hello")

    with pytest.raises(WorkspaceChoiceError, match="before the chat's first message"):
        space.choose_workspace(chat, str(tmp_path))


@pytest.mark.parametrize("path", ["relative/dir", "/no/such/dir/anywhere", "/", ""])
def test_a_chat_cannot_work_in_a_directory_that_is_not_one(minimal_config, monkeypatch, tmp_path, path):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    space = UserSpace(Deployment(config_path=str(minimal_config), working_directory=str(tmp_path)))
    chat = space.context_manager().start_new_context()

    with pytest.raises(WorkspaceChoiceError):
        space.choose_workspace(chat, path)
    assert space.conversation_workspace(chat) == space.workspace_path


def test_a_shared_server_gives_every_chat_the_users_workspace(minimal_config, monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    deployment = Deployment(config_path=str(minimal_config), routing_path=None)
    space = UserSpace(deployment, user_id="u1", layout=SpaceLayout.under(tmp_path / "u1"))
    chat = space.context_manager().start_new_context()

    assert not space.can_choose_workspace
    with pytest.raises(WorkspaceChoiceError):
        space.choose_workspace(chat, str(tmp_path))


def test_a_branch_works_in_the_directory_of_its_conversation(minimal_config, monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    project = tmp_path / "project"
    project.mkdir()
    space = UserSpace(Deployment(config_path=str(minimal_config), working_directory=str(tmp_path)))
    manager = space.context_manager()
    chat = manager.start_new_context()
    space.choose_workspace(chat, str(project))
    manager.add_message("user", "hello")
    message_id = manager.conversation_view(chat)["messages"][0].metadata["message_id"]

    branch, _ = manager.fork_context(chat, message_id)

    assert space.conversation_workspace(branch) == project.resolve()
