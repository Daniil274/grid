"""Regression for the residual workspace-binding holes of the 2026-11-06 audit.

Three defects are covered, all from ``/workspace/security-recheck``:

* D - a symlinked user directory (``users/alice -> users/bob``) used to pass
  the parent check because both sides were resolved first, so alice worked in
  bob's files. Links are now refused before they are collapsed.
* F - a catalog system's config was loaded as a plain override, so its own
  ``working_directory`` (even with ``allow_path_override: false``) moved the
  turn out of the space. Every system config now inherits the space's binding.
* G - ``Config.set_working_directory`` ignored the binding and could move a
  bound config until the next reload.

The CLI's one-user semantics are kept: the root config still decides whether
``--path`` is taken, and that choice - not a system config - is then
authoritative for every nested system.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config.config import Config
from core.routing import AutoRouter
from web_chat import space as space_module
from web_chat.deployment import Deployment
from web_chat.space import SpaceLayout, UserSpace, WorkspaceBindingError


BASE_CONFIG = """
settings:
  default_agent: a
  working_directory: {working_directory}
  allow_path_override: {allow_override}
  mcp_enabled: false
isolation:
  enabled: false
agents:
  a: {{name: A, model: m1, tools: []}}
models:
  m1: {{name: m1, provider: p}}
providers:
  p: {{name: p, base_url: "https://example.com/v1", api_key_env: TEST_KEY}}
"""

SYSTEM_CONFIG = """
settings:
  default_agent: a
  working_directory: {working_directory}
  allow_path_override: {allow_override}
  mcp_enabled: false
agents:
  a: {{name: A, model: m1, tools: []}}
models:
  m1: {{name: m1, provider: p}}
providers:
  p: {{name: p, base_url: "https://example.com/v1", api_key_env: TEST_KEY}}
"""


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "x")


def _write_system(path: Path, workdir: Path, *, allow_override: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        SYSTEM_CONFIG.format(
            working_directory=workdir, allow_override="true" if allow_override else "false"
        ),
        encoding="utf-8",
    )
    return path


def _catalog(tmp_path: Path, system_path: Path, *, allow_override: bool) -> Path:
    routing = tmp_path / "routing.yaml"
    routing.write_text(
        f"""
routing:
  model: m1
  systems:
    other: {{config: {system_path}}}
models:
  m1: {{name: m1, provider: p}}
providers:
  p: {{name: p, base_url: "https://example.com/v1", api_key_env: TEST_KEY}}
""",
        encoding="utf-8",
    )
    return routing


# -- F: a catalog system inherits the space's binding --------------------------
@pytest.mark.parametrize("root_allow_override", [True, False])
def test_catalog_system_config_cannot_escape_the_space(tmp_path, root_allow_override):
    """The system config names bob's directory; alice's space still wins."""
    alice = tmp_path / "users" / "alice" / "workspace"
    alice.mkdir(parents=True)
    bob = tmp_path / "users" / "bob" / "workspace"
    bob.mkdir(parents=True)
    other = _write_system(tmp_path / "systems" / "other.yaml", bob, allow_override=False)
    deployment = Deployment(routing_path=str(_catalog(tmp_path, other, allow_override=root_allow_override)))
    space = UserSpace(
        deployment,
        user_id="alice",
        layout=SpaceLayout.under(tmp_path / "users" / "alice"),
    )

    resolved = Path(space.registry.config("other").get_working_directory()).resolve()
    assert resolved == alice.resolve()
    assert resolved != bob.resolve()
    assert Path(space.fresh_config("other").get_working_directory()).resolve() == alice.resolve()


def test_the_deployment_path_is_authoritative_for_nested_systems(tmp_path):
    """The audit probe: AutoRouter built with the space's directory, no binding kw."""
    alice = tmp_path / "users" / "alice" / "workspace"
    alice.mkdir(parents=True)
    bob = tmp_path / "users" / "bob" / "workspace"
    bob.mkdir(parents=True)
    other = _write_system(tmp_path / "systems" / "other.yaml", bob, allow_override=False)
    catalog = Config(str(_catalog(tmp_path, other, allow_override=True)))

    router = AutoRouter.from_config(catalog, working_directory=str(alice))

    assert Path(router.system_config("other").get_working_directory()).resolve() == alice.resolve()


# -- CLI single-user semantics are preserved -----------------------------------
@pytest.mark.parametrize("allow_override", [True, False])
def test_cli_path_remains_an_override_at_the_root_and_a_binding_below(tmp_path, allow_override):
    """``--path`` is taken only when the root config allows it; then no system
    config may redirect it. With the flag off the config's directory wins."""
    cli = tmp_path / "cli"
    cli.mkdir()
    bob = tmp_path / "users" / "bob" / "workspace"
    bob.mkdir(parents=True)
    other = _write_system(
        tmp_path / "systems" / "other.yaml", bob, allow_override=allow_override
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        BASE_CONFIG.format(
            working_directory=".", allow_override="true" if allow_override else "false"
        )
        + f"""
routing:
  model: m1
  systems:
    other: {{config: {other}}}
""",
        encoding="utf-8",
    )
    root = Config(str(config_path), working_directory=str(cli))

    router = AutoRouter.from_config(root, working_directory=str(cli))

    expected_root = cli.resolve() if allow_override else config_path.parent.resolve()
    assert Path(root.get_working_directory()).resolve() == expected_root
    assert Path(router.system_config("other").get_working_directory()).resolve() == expected_root
    assert Path(router.system_config("other").get_working_directory()).resolve() != bob.resolve()


# -- G: a bound config cannot be moved afterwards ------------------------------
def test_a_bound_config_refuses_the_public_setter(tmp_path):
    alice = tmp_path / "users" / "alice" / "workspace"
    alice.mkdir(parents=True)
    bob = tmp_path / "users" / "bob" / "workspace"
    bob.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        BASE_CONFIG.format(working_directory=".", allow_override="true"), encoding="utf-8"
    )

    config = Config(str(config_path), workspace_binding=str(alice))
    config.set_working_directory(str(bob))

    assert Path(config.get_working_directory()) == alice.resolve()
    assert config.workspace_binding == str(alice)


# -- D: a symlinked user directory is refused before it is resolved ------------
class _Containers:
    made = []

    def __init__(self, config, enabled=None):
        self.enabled = True

    def get_or_create_container(self, user_id, workspace=None, mounts=None):
        type(self).made.append(workspace)
        return SimpleNamespace(id="c", name="grid-agent")

    def stop_container(self, user_id, container_id):
        pass

    def remove_container(self, user_id, container_id):
        pass


def test_a_symlinked_user_directory_is_refused_before_resolve(tmp_path, monkeypatch):
    users = tmp_path / "users"
    bob = users / "bob"
    (bob / "workspace").mkdir(parents=True)
    (bob / "workspace" / "secret.txt").write_text("bob's data", encoding="utf-8")
    os.symlink(bob, users / "alice")
    alice_layout = SpaceLayout.under(users / "alice")

    # The check sees the link even when the caller resolved the workspace first.
    with pytest.raises(WorkspaceBindingError):
        UserSpace._require_bound_directory(
            alice_layout.workspace.resolve(), alice_layout.workspace.parent, "The user's workspace"
        )
    with pytest.raises(WorkspaceBindingError):
        UserSpace._require_bound_directory(
            alice_layout.workspace, alice_layout.workspace.parent, "The user's workspace"
        )

    # And a whole space fails closed, before making a directory or a container.
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        BASE_CONFIG.format(working_directory=".", allow_override=False), encoding="utf-8"
    )
    deployment = Deployment(config_path=str(config_path), routing_path=None)
    _Containers.made = []
    monkeypatch.setattr(space_module, "ContainerManager", _Containers)

    with pytest.raises(WorkspaceBindingError):
        UserSpace(
            deployment,
            user_id="alice",
            layout=alice_layout,
            require_isolation=True,
        )

    assert _Containers.made == []
    assert (bob / "workspace" / "secret.txt").read_text(encoding="utf-8") == "bob's data"


# -- the registry owns the binding: base, catalog and every ExtraSystem -------
def _extras_for(tmp_path: Path, bob: Path, alice: Path) -> list:
    """One ExtraSystem per loader shape: a plain load, a load that already
    chains bind_workspace, and a load handed an explicit working directory."""
    from web_chat.systems import ExtraSystem

    loose = _write_system(tmp_path / "systems" / "loose.yaml", bob, allow_override=True)
    return [
        ExtraSystem(
            key="draft",
            name="Draft",
            description="",
            config_path=loose,
            load=lambda: Config(str(loose), str(bob)),
            routable=False,
            badge="draft",
        ),
        ExtraSystem(
            key="mine",
            name="Mine",
            description="",
            config_path=loose,
            load=lambda: Config(str(loose), str(alice)),
            routable=True,
            badge="mine",
        ),
    ]


@pytest.mark.parametrize("explicit_binding", [True, False])
def test_a_registry_binds_its_base_catalog_and_extras(tmp_path, explicit_binding):
    """An explicit workspace_binding, or the one the base config already has,
    keeps every system the registry loads inside the space - including extras
    whose loader knows nothing about the binding."""
    from web_chat.systems import SystemRegistry

    alice = tmp_path / "users" / "alice" / "workspace"
    alice.mkdir(parents=True)
    bob = tmp_path / "users" / "bob" / "workspace"
    bob.mkdir(parents=True)
    other = _write_system(tmp_path / "systems" / "other.yaml", bob, allow_override=True)
    catalog = Config(str(_catalog(tmp_path, other, allow_override=True)))
    base_path = tmp_path / "base.yaml"
    base_path.write_text(
        BASE_CONFIG.format(working_directory=bob, allow_override="true"), encoding="utf-8"
    )
    base = Config(str(base_path), workspace_binding=str(alice))
    extras = _extras_for(tmp_path, bob, alice)

    registry = SystemRegistry(
        base_config=base,
        catalog=catalog,
        build_factory=lambda config: None,
        workspace_binding=str(alice) if explicit_binding else None,
        extras=lambda: extras,
    )

    assert registry.workspace_binding == str(alice)
    for key in ("other", "draft", "mine"):
        config = registry.config(key)
        assert config.workspace_binding == str(alice), key
        assert Path(config.get_working_directory()).resolve() == alice.resolve(), key
    assert Path(registry._router.system_config("other").get_working_directory()).resolve() == alice.resolve()
    assert Path(base.get_working_directory()).resolve() == alice.resolve()
    # A routable extra is offered to the router, and the catalog still routes.
    assert "mine" in registry.route_candidates() and "other" in registry.route_candidates()


def test_an_unbound_registry_keeps_the_cli_semantics(tmp_path):
    """A one-user server binds nothing: --path stays an override, as before."""
    from web_chat.systems import SystemRegistry

    cli = tmp_path / "cli"
    cli.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        BASE_CONFIG.format(working_directory=".", allow_override="true"), encoding="utf-8"
    )
    root = Config(str(config_path), working_directory=str(cli))

    registry = SystemRegistry(base_config=root, build_factory=lambda config: None)

    assert registry.workspace_binding is None
    assert Path(registry.config(registry._base_key).get_working_directory()).resolve() == cli.resolve()


# -- every ExtraSystem the space builds inherits its binding ------------------
HUB_PROVIDERS = """
models:
  m1: {name: model-one, provider: openrouter}
  m2: {name: model-two, provider: openrouter}
providers:
  openrouter:
    name: openrouter
    base_url: https://example.com/v1
    api_key_env: TEST_KEY
"""

HUB_BASE = """
settings:
  default_agent: helper
  working_directory: .
  allow_path_override: true
  mcp_enabled: false
isolation:
  enabled: false
prompt_templates:
  base: You are a careful assistant.
agents:
  helper: {name: Helper, description: Does the work, model: m1, tools: []}
  writer: {name: Writer, description: Writes, model: m1, tools: []}
""" + HUB_PROVIDERS

HUB_CATALOG = """
settings:
  default_agent: helper
routing:
  model: m1
  systems_dir: systems
  default_system: base
  systems:
    base: {config: base/config.yaml, description: General work}
personal_agents:
  enabled: true
  templates:
    base: [helper, writer]
  models: [m2]
  max_systems: 2
  max_system_members: 3
  max_instructions_chars: 200
""" + HUB_PROVIDERS


def _private_system(bob: Path) -> str:
    """A config a user built: it names bob's directory and may override paths."""
    return f"""
settings:
  default_agent: main
  working_directory: {bob}
  allow_path_override: true
  mcp_enabled: false
agents:
  main: {{name: Main, model: m1, tools: []}}
""" + HUB_PROVIDERS


def _hub_deployment(tmp_path: Path) -> Deployment:
    (tmp_path / "base").mkdir(parents=True)
    (tmp_path / "base" / "config.yaml").write_text(HUB_BASE, encoding="utf-8")
    (tmp_path / "routing.yaml").write_text(HUB_CATALOG, encoding="utf-8")
    return Deployment(routing_path=str(tmp_path / "routing.yaml"))


def _hub_space(deployment: Deployment, tmp_path: Path, user_id: str, *, admin: bool) -> UserSpace:
    return UserSpace(
        deployment,
        user_id=user_id,
        layout=SpaceLayout.under(tmp_path / "users" / user_id),
        admin=admin,
    )


def test_every_extra_system_of_a_space_stays_inside_it(tmp_path):
    """The draft, private and built loaders all end up in the space's workspace."""
    from core.system_store import SystemManifest
    from web_chat.user_systems import MemberSpec, UserSystemSpec

    alice = tmp_path / "users" / "alice" / "workspace"
    alice.mkdir(parents=True)
    bob = tmp_path / "users" / "bob" / "workspace"
    bob.mkdir(parents=True)
    deployment = _hub_deployment(tmp_path)
    config = _private_system(bob)

    # A draft of an admin's shared store.
    admin = _hub_space(deployment, tmp_path, "root", admin=True)
    deployment.created_store.create(SystemManifest(key="team", name="Team", description="Draft"), config)
    admin.registry.refresh_extras()

    # A private system the user built, and one built by the system builder.
    user = _hub_space(deployment, tmp_path, "alice", admin=False)
    user.user_systems.create(
        UserSystemSpec(
            name="Research",
            description="Mine",
            base="base",
            entry="lead",
            members=[MemberSpec(id="lead", name="Lead", template="helper")],
        )
    )
    user.built_systems.create(
        SystemManifest(key="stats", name="Stats", description="Built"), config
    )
    user.registry.refresh_extras()

    assert {extra.badge for extra in admin.registry._extras.values()} == {"draft"}
    assert {extra.badge for extra in user.registry._extras.values()} == {"mine"}
    assert len(user.registry._extras) == 2  # the user's own system and the built one

    for space in (admin, user):
        for extra in space.registry._extras.values():
            workspace = space.workspace_path.resolve()
            # The loader itself is bound too, not only what the registry caches.
            loaded = extra.load()
            assert loaded.workspace_binding == str(space.layout.workspace), extra.key
            assert Path(loaded.get_working_directory()).resolve() == workspace, extra.key
            config = space.registry.config(extra.key)
            assert config.workspace_binding == str(space.layout.workspace), extra.key
            assert Path(config.get_working_directory()).resolve() == workspace, extra.key
            assert Path(config.get_working_directory()).resolve() != bob.resolve(), extra.key
