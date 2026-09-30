"""The builder creates shared drafts for admins and private systems for users."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.system_store import BuilderAccess, SystemManifest
from tools import system_builder_tools as builder
from utils.tool_isolation import SYSTEMS, is_confined
from web_chat.space import SpaceLayout, UserSpace

from tests.test_system_hub import BASE, CATALOG, PROVIDERS  # noqa: F401  (the same small server)

CATALOG_WITH_BUILDER = CATALOG.replace(
    """    base:
      config: base/config.yaml
      description: General work on code and questions
""",
    """    base:
      config: base/config.yaml
      description: General work on code and questions
    builder:
      config: builder/config.yaml
      admins_only: false
      description: Making systems
""",
)

BUILDER = """
settings: {default_agent: builder}
tools:
  builder_create: {type: function, description: Create}
agents:
  builder:
    name: Builder
    description: Makes systems
    model: m1
    tools: [builder_create]
""" + PROVIDERS

NEW_SYSTEM = """
settings:
  default_agent: main
  mcp_enabled: true
tools:
  stats:
    type: mcp
    tool_package: tools/stats
    description: Text statistics
agents:
  main:
    name: Main
    description: Text statistics of workspace files
    model: m1
    tools: [stats]
""" + PROVIDERS

PACKAGE = '''
from grid_tool import tool


@tool(read_only=True)
def words(text: str) -> int:
    """Count the words of a text."""
    return len(text.split())
'''


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    from web_chat.deployment import Deployment

    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    for name, text in (("base", BASE), ("builder", BUILDER)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.yaml").write_text(text, encoding="utf-8")
    (tmp_path / "base" / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "routing.yaml").write_text(CATALOG_WITH_BUILDER, encoding="utf-8")
    return Deployment(routing_path=str(tmp_path / "routing.yaml"))


def access_for(deployment, events=None, changes=None):
    return BuilderAccess(
        store=deployment.created_store,
        catalog=deployment.catalog,
        base_config=deployment.config,
        user_id="root",
        image="grid-agent:latest",
        record=lambda key, event, note: (events if events is not None else []).append((key, event, note)),
        changed=lambda: (changes if changes is not None else []).append(1),
    )


def call(tool_name, access, **arguments):
    context = SimpleNamespace(context=SimpleNamespace(factory=SimpleNamespace(system_builder=access)))
    tool = builder.SYSTEM_BUILDER_TOOLS[tool_name]
    return asyncio.run(tool.on_invoke_tool(context, json.dumps(arguments)))


def test_the_builder_requires_a_granted_store(deployment):
    assert "needs access" in call("builder_catalog", None)
    assert "needs access" in call("builder_create", None, key="x1", name="X", description="", config_yaml=NEW_SYSTEM)
    assert deployment.created_store.list() == []
    # Its tools are allowed in isolated spaces because they touch the created systems only.
    assert is_confined(SYSTEMS)


def test_the_builder_makes_a_system_from_scratch_with_a_tool_package(deployment):
    events, changes = [], []
    access = access_for(deployment, events, changes)

    catalog = json.loads(call("builder_catalog", access))
    assert set(catalog["catalog"]) == {"base"}  # the builder itself is no example
    assert "m1" in catalog["models"]

    created = json.loads(call("builder_create", access, key="stats", name="Stats", description="Text statistics", config_yaml=NEW_SYSTEM))
    assert created["created"] == "stats"
    health = created["health"]
    assert health["loads"] and not health["healthy"]
    assert any("does not exist" in problem for problem in health["tool_packages"][0]["problems"])

    written = json.loads(call("builder_write", access, system="stats", path="tools/stats/stats.py", content=PACKAGE))
    health = written["health"]
    assert health["healthy"], health
    assert health["tool_packages"][0]["tools"] == ["words"]

    manifest = deployment.created_store.get("stats")
    assert (manifest.status, manifest.origin.source) == ("draft", "builder")
    assert [event for _, event, _ in events] == ["created", "edited"]
    assert len(changes) == 2


@pytest.mark.parametrize(
    "path, problem",
    [
        ("../base/config.yaml", "outside"),
        ("system.yaml", "manifest"),
        (".env", "not for reading"),
    ],
)
def test_the_builder_writes_only_a_drafts_own_files(deployment, path, problem):
    access = access_for(deployment)
    call("builder_create", access, key="stats", name="Stats", description="", config_yaml=NEW_SYSTEM)
    assert problem in call("builder_write", access, system="stats", path=path, content="x")


def test_a_broken_config_is_not_written(deployment):
    access = access_for(deployment)
    call("builder_create", access, key="stats", name="Stats", description="", config_yaml=NEW_SYSTEM)
    answer = call("builder_write", access, system="stats", path="config.yaml", content="agents: [1]")
    assert answer.startswith("Not written")
    assert deployment.created_store.config_text("stats") == NEW_SYSTEM


def test_published_systems_and_the_catalog_are_read_only(deployment):
    access = access_for(deployment)
    call("builder_create", access, key="stats", name="Stats", description="", config_yaml=NEW_SYSTEM)
    deployment.created_store.set_status("stats", "published")
    assert "published" in call("builder_write", access, system="stats", path="README.md", content="x")
    assert "Not created" in call("builder_create", access, key="base", name="B", description="", config_yaml=NEW_SYSTEM)
    assert "does not" in call("builder_write", access, system="base", path="README.md", content="x") or "No created" in call(
        "builder_write", access, system="base", path="README.md", content="x"
    )
    # Catalog systems are examples to read; secrets are never read.
    assert "default_agent: helper" in call("builder_read", access, system="base")
    assert "not for reading" in call("builder_read", access, system="base", path=".env")
    assert ".env" not in call("builder_read", access, system="base", path="")
    assert "not an example" in call("builder_read", access, system="builder")


# -- who gets the builder ---------------------------------------------------------------


def space_of(deployment, user_id, *, admin):
    root = Path(deployment.routing_path).parent / "users" / user_id
    return UserSpace(deployment, user_id=user_id, layout=SpaceLayout.under(root), admin=admin)


def test_every_users_space_has_the_builder_with_its_own_access(deployment):
    admin, user = space_of(deployment, "root", admin=True), space_of(deployment, "alice", admin=False)

    assert "builder" in admin.registry.keys() and "builder" in admin.registry.route_candidates()
    assert "builder" in user.registry.keys()
    assert "builder" in user.registry.route_candidates()
    assert user.registry.selection_is_valid("builder", None)

    assert admin.registry.factory("builder").system_builder is not None
    access = user.registry.factory("builder").system_builder
    assert access is not None and not access.shared
    assert access.store.root == user.layout.user_systems.parent / "built_systems"
    assert access.store.root != deployment.created_store.root


def private_config():
    document = yaml.safe_load(NEW_SYSTEM)
    document["tools"] = {}
    document["agents"]["main"]["tools"] = []
    return yaml.safe_dump(document)


def test_private_systems_are_isolated_and_selectable_only_by_their_owner(deployment):
    alice = space_of(deployment, "alice", admin=False)
    bob = space_of(deployment, "bob", admin=False)
    admin = space_of(deployment, "root", admin=True)
    first = json.loads(call("builder_create", alice._builder_access(), key="stats", name="Alice stats",
                            description="Text analysis", config_yaml=private_config()))
    second = json.loads(call("builder_create", bob._builder_access(), key="stats", name="Bob stats",
                             description="Text analysis", config_yaml=private_config()))
    assert first["chat_system"] != second["chat_system"]
    assert alice.registry.selection_is_valid(first["chat_system"], None)
    assert not bob.registry.selection_is_valid(first["chat_system"], None)
    assert not admin.registry.selection_is_valid(first["chat_system"], None)
    assert first["chat_system"] not in alice.registry.route_candidates()  # a draft
    assert deployment.created_store.list() == []
    assert alice.built_systems.get("stats").origin.user_id == "alice"
    assert alice.registry.config(first["chat_system"]).get_working_directory() == str(alice.workspace_path)
    assert "Not written" in call("builder_write", bob._builder_access(), system="absent", path="README.md", content="x")


def test_private_system_can_be_managed_and_activated_through_its_owners_api(deployment, tmp_path):
    from tests.test_system_hub import ALICE, BOB, ROOT, client_for

    alice = space_of(deployment, "alice", admin=False)
    created = json.loads(call("builder_create", alice._builder_access(), key="stats", name="Stats",
                              description="Text analysis", config_yaml=private_config()))
    client = client_for(deployment, tmp_path)
    overview = client.get("/api/systems", headers=ALICE).json()
    assert overview["builder"] == "builder"
    assert any(item["kind"] == "built" and item["key"] == "stats" for item in overview["items"])
    assert client.get("/api/systems/built/stats", headers=BOB).status_code == 404
    assert client.get("/api/systems/built/stats", headers=ROOT).status_code == 404
    assert client.put("/api/systems/built/stats/config", headers=BOB, json={"yaml": private_config()}).status_code == 404
    assert client.post("/api/systems/built/stats/active", headers=ALICE, json={"active": True}).status_code == 200
    detail = client.get("/api/systems/built/stats", headers=ALICE).json()
    assert detail["active"] and detail["chat_key"] == created["chat_system"]
    assert client.patch("/api/systems/built/stats", headers=ALICE, json={"name": "My stats"}).status_code == 200
    assert client.get("/api/systems/built/stats", headers=ALICE).json()["name"] == "My stats"
    bootstrap = client.get("/api/chat/bootstrap", headers=ALICE).json()
    assert created["chat_system"] in {item["key"] for item in bootstrap["systems"]}
    assert created["chat_system"] not in {item["key"] for item in client.get("/api/chat/bootstrap", headers=BOB).json()["systems"]}
    assert client.post("/api/systems/created/stats/status", headers=ALICE, json={"status": "published"}).status_code == 403
    assert client.delete("/api/systems/built/stats", headers=BOB).status_code == 404
    assert client.delete("/api/systems/built/stats", headers=ALICE).status_code == 200
    assert client.get("/api/systems/built/stats", headers=ALICE).status_code == 404


@pytest.mark.parametrize("change", [
    lambda c: c["settings"].update(project_tools={"enabled": True, "tools_directory": "tools"}),
    lambda c: c["settings"].update(allow_path_override=False),
    lambda c: c["settings"].update(config_directory="../../root"),
    lambda c: c["settings"].update(action_policy={"policy_file": "../../policy.yaml"}),
    lambda c: c["providers"]["openrouter"].update(base_url="https://attacker.example/v1"),
    lambda c: c["models"]["m1"].update(name="unapproved-model"),
    lambda c: c["tools"]["stats"].update(server_command=["python", "untrusted.py"]),
    lambda c: c["tools"]["stats"].update(tool_package="../../other-user"),
    lambda c: c["agents"]["main"].update(system_skills=["../../secret"]),
])
def test_private_configs_are_rejected_before_the_server_can_load_them(deployment, change):
    alice = space_of(deployment, "alice", admin=False)
    document = yaml.safe_load(NEW_SYSTEM)
    change(document)
    result = call("builder_create", alice._builder_access(), key="unsafe", name="Unsafe", description="",
                  config_yaml=yaml.safe_dump(document))
    assert result.startswith("Not created"), result
    assert alice.built_systems.list() == []


def test_private_builder_obeys_its_system_limit(deployment):
    alice = space_of(deployment, "alice", admin=False)
    access = alice._builder_access()
    for key in ["first", "second"]:
        assert json.loads(call("builder_create", access, key=key, name=key, description="", config_yaml=private_config()))["created"] == key
    assert "At most 2" in call("builder_create", access, key="third", name="Third", description="", config_yaml=private_config())


def test_private_catalog_redacts_credentials_but_runtime_keeps_trusted_headers(deployment):
    from web_chat.deployment import Deployment

    path = deployment.config_path
    document = yaml.safe_load(path.read_text())
    document["providers"]["openrouter"]["api_key"] = "private-key"
    document["providers"]["openrouter"]["default_headers"] = {"Authorization": "Bearer private", "x-opencode-session": "grid-test"}
    path.write_text(yaml.safe_dump(document))
    deployment = Deployment(routing_path=str(deployment.routing_path))
    alice = space_of(deployment, "alice", admin=False)
    access = alice._builder_access()
    catalog = call("builder_catalog", access)
    assert "private-key" not in catalog and "Bearer private" not in catalog
    assert "grid-test" in catalog
    example = call("builder_read", access, system="base")
    assert "private-key" not in example and "Bearer private" not in example
    config = yaml.safe_load(private_config())
    config["providers"] = json.loads(catalog)["providers"]
    created = json.loads(call("builder_create", access, key="safe", name="Safe", description="", config_yaml=yaml.safe_dump(config)))
    assert created["health"]["healthy"], created
    assert access.load("safe").config.providers["openrouter"].default_headers["Authorization"] == "Bearer private"


def test_template_and_builder_systems_share_the_user_limit(deployment, tmp_path):
    from tests.test_system_hub import ALICE, client_for, user_spec

    alice = space_of(deployment, "alice", admin=False)
    alice.user_systems.create(user_spec())
    access = alice._builder_access()
    call("builder_create", access, key="first", name="First", description="", config_yaml=private_config())
    assert "At most 2" in call("builder_create", access, key="second", name="Second", description="", config_yaml=private_config())
    client = client_for(deployment, tmp_path)
    assert client.post("/api/systems/mine", headers=ALICE, json=user_spec(name="Another").model_dump()).status_code == 400


def test_an_admin_can_start_a_blank_system(deployment, tmp_path):
    from tests.test_system_hub import ROOT, client_for

    client = client_for(deployment, tmp_path)
    overview = client.get("/api/systems", headers=ROOT).json()
    assert overview["builder"] == "builder"
    assert "builder" not in overview["sources"]
    created = client.post(
        "/api/systems/created", headers=ROOT, json={"key": "empty", "name": "Empty", "description": "", "source": None}
    )
    assert created.status_code == 201, created.text
    config = yaml.safe_load(deployment.created_store.config_text("empty"))
    assert list(config["agents"]) == ["assistant"] and config["agents"]["assistant"]["tools"] == []
    detail = client.get("/api/systems/created/empty", headers=ROOT).json()
    assert detail["inspection"]["loaded"] and detail["inspection"]["config"] == []
