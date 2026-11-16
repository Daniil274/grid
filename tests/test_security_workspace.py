"""The workspace of a user is the server's binding, not a config override.

Regression for S1 of the 2026-11-06 audit: with ``allow_path_override: false``
a laid-out space used to fall back to the config's ``working_directory`` (or
the config's directory), so every user mounted one base workspace. These tests
build two real :class:`UserSpace` objects over one config, for both values of
the flag and a relative/absolute configured directory, and for chat runtimes
whose roots must stay the user's own.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config.config import Config
from utils.exceptions import ConfigError
from web_chat import space as space_module
from web_chat.deployment import Deployment
from web_chat.space import ChatRoots, SpaceLayout, UserSpace, WorkspaceBindingError


CONFIG_TEMPLATE = """
settings:
  default_agent: test_agent
  max_history: 10
  max_turns: 5
  agent_timeout: 60
  working_directory: {working_directory}
  allow_path_override: {allow_override}
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


def _write_config(path: Path, *, allow_override: bool, working_directory: str) -> Path:
    path.write_text(
        CONFIG_TEMPLATE.format(
            allow_override="true" if allow_override else "false",
            working_directory=working_directory,
        ),
        encoding="utf-8",
    )
    return path


class _Containers:
    """ContainerManager stand-in: records the directory each one would mount."""

    made = []

    def __init__(self, config, enabled=None):
        self.enabled = True

    def get_or_create_container(self, user_id, workspace=None, mounts=None):
        _Containers.made.append(
            {"user": user_id, "workspace": Path(workspace).resolve(), "mounts": dict(mounts or {})}
        )
        return SimpleNamespace(id=f"c-{Path(workspace).name}", name=f"grid-agent-{Path(workspace).name}")

    def stop_container(self, user_id, container_id):
        pass

    def remove_container(self, user_id, container_id):
        pass


@pytest.fixture
def containers(monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    monkeypatch.setattr("web_chat.space.ContainerManager", _Containers)
    _Containers.made = []
    return _Containers


# -- the config's binding ---------------------------------------------------------
def test_a_bound_workspace_wins_over_allow_path_override(tmp_path):
    config_path = _write_config(tmp_path / "config.yaml", allow_override=False, working_directory=".")
    space_dir = tmp_path / "users" / "u1" / "workspace"
    space_dir.mkdir(parents=True)

    config = Config(str(config_path), str(tmp_path / "cli"), workspace_binding=str(space_dir))

    assert Path(config.get_working_directory()) == space_dir.resolve()
    assert Path(config.get_working_directory()) != config_path.parent.resolve()


@pytest.mark.parametrize("binding", ["relative/space", "", "   "])
def test_a_workspace_binding_must_be_an_absolute_directory(tmp_path, binding):
    config_path = _write_config(tmp_path / "config.yaml", allow_override=True, working_directory=".")

    with pytest.raises(ConfigError):
        Config(str(config_path), workspace_binding=binding)


# -- two users, one config --------------------------------------------------------
@pytest.mark.parametrize("allow_override", [True, False])
@pytest.mark.parametrize("where", ["relative", "absolute"])
def test_each_user_works_in_their_own_workspace_whatever_the_flag_says(
    tmp_path, containers, allow_override, where
):
    """The S1 reproduction: two users of one config, neither sharing a workspace."""
    server_tree = tmp_path / "shared" / "server-tree"
    server_tree.mkdir(parents=True)
    working_directory = "." if where == "relative" else str(server_tree)
    config_path = _write_config(
        tmp_path / "config.yaml", allow_override=allow_override, working_directory=working_directory
    )
    deployment = Deployment(config_path=str(config_path), routing_path=None)

    spaces = {}
    for user_id in ("alice", "bob"):
        spaces[user_id] = UserSpace(
            deployment,
            user_id=user_id,
            layout=SpaceLayout.under(tmp_path / "users" / user_id),
            require_isolation=True,
        )

    expected = {user_id: (tmp_path / "users" / user_id / "workspace").resolve() for user_id in spaces}
    assert expected["alice"] != expected["bob"]
    for user_id, space in spaces.items():
        assert space.workspace_path == expected[user_id]
        factory = space.registry.factory(space.registry.default_key())
        assert Path(factory.config.get_working_directory()) == expected[user_id]
        assert space.conversations_path == tmp_path / "users" / user_id / "conversations.json"

    # Each container is asked for that user's own workspace, never the config's tree.
    mounted = {entry["user"]: entry["workspace"] for entry in containers.made}
    assert mounted == expected
    assert server_tree.resolve() not in mounted.values()


def test_a_config_that_drops_the_binding_cannot_share_a_workspace(tmp_path, containers, monkeypatch):
    """The old behaviour, reproduced: without the binding the space fails closed."""
    config_path = _write_config(tmp_path / "config.yaml", allow_override=False, working_directory=".")
    deployment = Deployment(config_path=str(config_path), routing_path=None)
    real = space_module.Config

    def dropping_binding(path, working_directory=None, *, workspace_binding=None):
        # What Config did before the fix: the binding is just another override.
        return real(path, working_directory, workspace_binding=None)

    monkeypatch.setattr(space_module, "Config", dropping_binding)
    root = tmp_path / "users" / "alice"

    with pytest.raises(WorkspaceBindingError):
        UserSpace(deployment, user_id="alice", layout=SpaceLayout.under(root), require_isolation=True)

    assert containers.made == []
    assert not (root / "workspace").exists()


# -- the roots of a chat with a space of its own ----------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("allow_override", [True, False])
async def test_a_chat_works_in_its_space_and_mounts_the_users_own(tmp_path, containers, allow_override):
    config_path = _write_config(tmp_path / "config.yaml", allow_override=allow_override, working_directory=".")
    deployment = Deployment(config_path=str(config_path), routing_path=None)
    root = tmp_path / "users" / "alice"
    space = UserSpace(
        deployment,
        user_id="alice",
        layout=SpaceLayout.under(root),
        require_isolation=True,
        chat_spaces=True,
    )
    chat = space.context_manager().start_new_context()
    space.assign_chat_space(chat)
    roots = space.chat_roots(chat)

    assert roots.chat == (root / "chats" / chat).resolve()
    # The user's root is the layout's workspace, not whatever a config resolved to.
    assert roots.user == (root / "workspace").resolve()
    assert roots.user == space.workspace_path == space.layout.workspace.resolve()

    registry = await space.registry_for(chat)
    factory = registry.factory(registry.default_key())
    assert Path(factory.config.get_working_directory()) == roots.chat
    runtime = containers.made[-1]
    assert runtime["workspace"] == roots.chat
    assert runtime["mounts"] == {"/user": roots.user}
    # The base container still mounts the user's workspace alone.
    assert containers.made[0]["workspace"] == (root / "workspace").resolve()
    await space.close()


def test_a_chat_runtime_refuses_roots_outside_the_users_layout(tmp_path, containers):
    config_path = _write_config(tmp_path / "config.yaml", allow_override=False, working_directory=".")
    deployment = Deployment(config_path=str(config_path), routing_path=None)
    root = tmp_path / "users" / "alice"
    space = UserSpace(
        deployment,
        user_id="alice",
        layout=SpaceLayout.under(root),
        require_isolation=True,
        chat_spaces=True,
    )
    user = space.layout.workspace.resolve()
    elsewhere = tmp_path / "users" / "bob" / "workspace"
    elsewhere.mkdir(parents=True)
    before = len(containers.made)

    with pytest.raises(WorkspaceBindingError):
        space._build_chat_runtime(ChatRoots(chat=(root / "chats" / "ctx").resolve(), user=elsewhere.resolve()))

    # A chat directory linked out of the user's chats is refused too.
    escaped = tmp_path / "outside"
    escaped.mkdir()
    (root / "chats").mkdir(parents=True, exist_ok=True)
    os.symlink(escaped, root / "chats" / "linked")
    with pytest.raises(WorkspaceBindingError):
        space._build_chat_runtime(ChatRoots(chat=escaped.resolve(), user=user))

    assert len(containers.made) == before
