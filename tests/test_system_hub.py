"""Created and user systems: the store, routing, the rules, the systems page API."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from core.config import Config
from core.routing import AutoRouter, check_system
from core.system_quality import evidence_dir, save_evidence
from core.system_store import StoreError, SystemManifest, SystemStore, relocate_config
from schemas.schemas import PersonalAgentsPolicy
from web_chat.deployment import Deployment
from web_chat.identity import User
from web_chat.server import WebChatServer
from web_chat.space import SpaceLayout, UserSpace
from web_chat.spaces import SpacePool
from web_chat.system_activity import SystemActivity
from web_chat.system_hub import blank_config, copy_config, export_user_system
from web_chat.user_systems import (
    MemberSpec,
    UserSystemError,
    UserSystemSpec,
    ask_tool,
    check_spec,
    materialize,
    member_key,
)

SAME_SITE = {"Origin": "http://testserver"}


def test_blank_system_has_real_delegation_and_independent_review(deployment, tmp_path):
    base = Config(str(deployment.system_config_path("base")))
    document = yaml.safe_load(blank_config(base))
    entry = document["agents"][document["settings"]["default_agent"]]
    targets = {document["tools"][name]["target_agent"] for name in entry["tools"]}
    assert targets == {"worker", "reviewer"}
    assert all(document["agents"][key]["routable"] is False for key in targets)
    path = tmp_path / "team.yaml"
    path.write_text(yaml.safe_dump(document))
    assert set(Config(str(path)).config.agents) == {"assistant", "worker", "reviewer"}

PROVIDERS = """
models:
  m1: {name: model-one, provider: openrouter}
  m2: {name: model-two, provider: openrouter}
  m3: {name: model-three, provider: openrouter}
  r: {name: router-model, provider: openrouter}
providers:
  openrouter:
    name: openrouter
    base_url: https://example.com/v1
    api_key_env: TEST_OPENROUTER_KEY
"""

BASE = """
settings:
  default_agent: helper
  working_directory: .
  allow_path_override: true
  mcp_enabled: false
isolation:
  enabled: false
prompt_templates:
  base: You are a careful assistant.
tools:
  grid_systems_catalog: {type: function, description: List systems}
  grid_check_system: {type: function, description: Check a system}
  call_writer: {type: agent, target_agent: writer, description: Ask the writer, context_strategy: minimal}
agents:
  helper:
    name: Helper
    description: Does the work
    model: m1
    tools: [grid_systems_catalog, grid_check_system, call_writer]
  writer:
    name: Writer
    description: Writes
    model: m1
    tools: [grid_systems_catalog]
""" + PROVIDERS

CATALOG = """
settings:
  default_agent: helper
routing:
  model: r
  systems_dir: systems
  default_system: base
  systems:
    base:
      config: base/config.yaml
      description: General work on code and questions
personal_agents:
  enabled: true
  templates:
    base: [helper, writer]
  models: [m2]
  max_systems: 2
  max_system_members: 3
  max_instructions_chars: 200
""" + PROVIDERS


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    (tmp_path / "base" / "skills").mkdir(parents=True)
    (tmp_path / "base" / "config.yaml").write_text(BASE, encoding="utf-8")
    (tmp_path / "base" / "skills" / "style.md").write_text("Write plainly.", encoding="utf-8")
    (tmp_path / "routing.yaml").write_text(CATALOG, encoding="utf-8")
    return Deployment(routing_path=str(tmp_path / "routing.yaml"))


def manifest(key="team", **changes) -> SystemManifest:
    return SystemManifest(**{"key": key, "name": "Team", "description": "Research with sources", **changes})


def member(member_id="lead", **changes) -> MemberSpec:
    return MemberSpec(**{"id": member_id, "name": member_id.title(), "template": "helper", **changes})


def user_spec(**changes) -> UserSystemSpec:
    fields = {
        "name": "Research team",
        "description": "Market research with sources",
        "base": "base",
        "entry": "lead",
        "members": [
            member("lead", delegates=["scout"], instructions="Plan, then ask the scout."),
            member("scout", template="writer", description="Finds sources"),
        ],
    }
    return UserSystemSpec(**{**fields, **changes})


def policy(deployment) -> PersonalAgentsPolicy:
    return deployment.personal_agents_policy


# -- the store ---------------------------------------------------------------------


def test_a_created_system_goes_from_draft_to_published_to_archived(tmp_path):
    store = SystemStore(tmp_path / "systems")
    store.create(manifest(), "settings: {}\n", skills_from=None)
    assert [item.status for item in store.list()] == ["draft"]
    assert store.published() == []

    published = store.set_status("team", "published")
    assert published.published_at is not None
    assert [item.key for item in store.published()] == ["team"]
    with pytest.raises(StoreError, match="Withdraw"):
        store.delete("team")

    store.set_status("team", "archived")
    store.delete("team")
    assert store.list() == []


@pytest.mark.parametrize("key", ["", "A", "x", "1abc", "has space", "../up", "a" * 41])
def test_a_key_must_name_a_directory_safely(tmp_path, key):
    with pytest.raises((StoreError, ValueError)):
        SystemStore(tmp_path).create(manifest(key=key), "settings: {}\n")


def test_two_systems_cannot_share_a_key(tmp_path):
    store = SystemStore(tmp_path)
    store.create(manifest(), "settings: {}\n")
    with pytest.raises(StoreError, match="already exists"):
        store.create(manifest(), "settings: {}\n")


def test_a_moved_config_still_reaches_what_the_original_did(tmp_path):
    source, target = tmp_path / "examples" / "coder", tmp_path / "systems" / "copy"
    (source / "tools").mkdir(parents=True)
    (source / "server.py").write_text("", encoding="utf-8")
    document = {
        "settings": {
            "working_directory": ".",
            "project_tools": {"enabled": True, "tools_directory": "./tools"},
            "action_policy": {"policy_file": "../../policies/p.yaml"},
        },
        "tools": {"local": {"type": "mcp", "server_command": ["python", "server.py", "--flag"]}},
    }
    moved = relocate_config(document, source, target)
    assert moved["settings"]["working_directory"] == "."
    assert moved["settings"]["project_tools"]["tools_directory"] == "../../examples/coder/tools"
    assert moved["settings"]["action_policy"]["policy_file"] == "../../policies/p.yaml"
    assert moved["tools"]["local"]["server_command"] == ["python", "../../examples/coder/server.py", "--flag"]


# -- routing ---------------------------------------------------------------------


def test_the_router_offers_published_created_systems_and_no_drafts(deployment):
    store = deployment.created_store
    store.create(manifest("draft-one"), BASE)
    store.create(manifest("live-one", description="Market research"), BASE)
    store.set_status("live-one", "published")
    store.create(manifest("base", description="shadowing the catalog"), BASE)
    store.set_status("base", "published")

    router = AutoRouter.from_config(deployment.catalog)
    assert router.systems() == {"base": "General work on code and questions", "live-one": "Market research"}
    assert router.system_config_path("live-one") == store.config_path("live-one").resolve()
    assert router.system_config_path("base") == (Path(deployment.routing_path).parent / "base" / "config.yaml").resolve()


# -- created systems ---------------------------------------------------------------


def test_a_copy_keeps_the_chosen_agents_and_loads_healthy(deployment):
    store = deployment.created_store
    source = deployment.system_config_path("base")
    text = copy_config(source, store.directory("solo"), ["helper"])
    store.create(manifest("solo"), text, skills_from=source.parent / "skills")

    config = Config(str(store.config_path("solo")))
    assert list(config.config.agents) == ["helper"]
    # Its target is gone, so the agent tool goes too - declared and used.
    assert "call_writer" not in config.config.tools
    assert config.config.agents["helper"].tools == ["grid_systems_catalog", "grid_check_system"]
    assert config.get_default_agent() == "helper"
    assert (store.directory("solo") / "skills" / "style.md").is_file()
    assert check_system(config) == []


def test_a_copy_refuses_agents_the_source_has_not(deployment):
    with pytest.raises(StoreError, match="no agents"):
        copy_config(deployment.system_config_path("base"), deployment.created_store.directory("xx"), ["ghost"])


# -- user systems ------------------------------------------------------------------


def base_config(deployment) -> Config:
    return Config(str(deployment.system_config_path("base")))


def test_a_user_system_hands_work_between_its_members(deployment):
    config = materialize(user_spec(), base_config(deployment))

    agents = config.config.agents
    lead, scout = agents[member_key("lead")], agents[member_key("scout")]
    assert config.get_default_agent() == member_key("lead")
    assert lead.routable and not scout.routable
    assert not agents["helper"].routable and not agents["writer"].routable
    assert lead.tools == ["grid_systems_catalog", "grid_check_system", "call_writer", ask_tool("scout")]
    assert config.config.tools[ask_tool("scout")].target_agent == member_key("scout")
    assert ask_tool("lead") not in config.config.tools
    assert "Plan, then ask the scout." in lead.custom_prompt
    assert check_system(config) == []


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"base": "elsewhere"}, "cannot be used as a base"),
        ({"entry": "nobody"}, "entry agent"),
        ({"members": [member("lead", template="ghost")]}, "cannot be used as a template"),
        ({"members": [member("lead", tools=["grid_systems_catalog", "bash_tool"])]}, "tools of its template"),
        ({"members": [member("lead", model="m3")]}, "model is not available"),
        ({"members": [member("lead", delegates=["ghost"])]}, "does not have"),
        ({"members": [member("lead", delegates=["lead"])]}, "itself"),
        ({"members": [member("lead"), member("lead")]}, "same id"),
        ({"members": [member("lead"), member("b"), member("c"), member("d")]}, "at most 3"),
        ({"members": [member("lead", instructions="x" * 201)]}, "limited to 200"),
    ],
)
def test_a_user_system_can_do_nothing_its_templates_could_not(deployment, changes, message):
    with pytest.raises(UserSystemError, match=message):
        check_spec(user_spec(**changes), base_config(deployment), policy(deployment))


def test_a_member_id_must_not_clash_with_the_base_system(deployment, tmp_path):
    config = base_config(deployment)
    config.config.tools[ask_tool("lead")] = config.config.tools["call_writer"]
    with pytest.raises(UserSystemError, match="clashes"):
        check_spec(user_spec(), config, policy(deployment))


def test_an_imported_user_system_is_a_healthy_config_of_its_own(deployment):
    store = deployment.created_store
    base = base_config(deployment)
    text = export_user_system(user_spec(), base, store.directory("team"))
    store.create(manifest(), text, skills_from=Path(base.config_path).parent / "skills")

    config = Config(str(store.config_path("team")))
    assert config.get_default_agent() == member_key("lead")
    assert [key for key, agent in config.config.agents.items() if agent.routable] == [member_key("lead")]
    assert config.config.tools[ask_tool("scout")].target_agent == member_key("scout")
    assert check_system(config) == []


# -- the space ---------------------------------------------------------------------


def space_of(deployment, user_id: str, *, admin=False, activity=None) -> UserSpace:
    root = Path(deployment.routing_path).parent / "users" / user_id
    return UserSpace(
        deployment, user_id=user_id, layout=SpaceLayout.under(root), admin=admin, activity=activity
    )


def test_drafts_are_for_admins_to_pick_and_never_routed(deployment):
    deployment.created_store.create(manifest("draft-one"), BASE)
    admin = space_of(deployment, "admin", admin=True)
    user = space_of(deployment, "user")

    assert "draft-one" in admin.registry.keys()
    assert admin.registry.systems()[-1].badge == "draft"
    assert "draft-one" not in admin.registry.route_candidates()
    assert admin.registry.selection_is_valid("draft-one", "helper")
    assert "draft-one" not in user.registry.keys()


def test_a_user_system_is_routed_only_while_active_and_only_for_its_owner(deployment):
    alice, bob = space_of(deployment, "alice"), space_of(deployment, "bob")
    system = alice.change_user_systems(lambda mine: mine.create(user_spec()))

    assert system.key in alice.registry.keys()
    assert system.key not in alice.registry.route_candidates()
    assert system.key not in bob.registry.keys()
    assert alice.registry.config(system.key).get_default_agent() == member_key("lead")

    alice.change_user_systems(lambda mine: mine.set_active(system.key, True))
    assert alice.registry.route_candidates()[system.key] == "Market research with sources"
    # A new space reads the systems back from the owner's file.
    assert system.key in space_of(deployment, "alice").registry.route_candidates()


# -- activity ----------------------------------------------------------------------


def test_activity_counts_turns_and_keeps_events(tmp_path):
    activity = SystemActivity(tmp_path / "activity.json")
    activity.record_turn("team", agent="lead", outcome="answered", duration_ms=1200, user_id="u1", policy_blocks=1)
    activity.record_turn("team", agent="lead", outcome="error", duration_ms=800, user_id="u2")
    activity.record_event("team", "published", by="admin")

    summary = SystemActivity(tmp_path / "activity.json").summary("team", names={"u1": "alice"})
    assert summary["turns"] == 2 and summary["outcomes"]["error"] == 1
    assert summary["policy_blocks"] == 1 and summary["user_count"] == 2
    assert summary["average_ms"] == 1000
    assert [turn["user"] for turn in summary["recent"]] == ["u2", "alice"]
    assert summary["events"][0]["event"] == "published"
    assert activity.counts()["team"] == {"turns": 2, "errors": 1, "last_at": summary["last_at"]}


# -- over HTTP -------------------------------------------------------------------


def client_for(deployment, tmp_path):
    activity = SystemActivity(tmp_path / "activity.json")
    roles = {"root": "admin", "alice": "user", "bob": "user"}

    async def identify(connection):
        name = connection.headers["X-Test-User"]
        return User(id=name, username=name, role=roles[name])

    server = WebChatServer(
        deployment,
        SpacePool(lambda user_id: space_of(deployment, user_id, admin=roles[user_id] == "admin", activity=activity)),
        identify=identify,
        warm_user=None,
        activity=activity,
        submissions_dir=tmp_path / "submissions",
    )
    return TestClient(server.app, headers=SAME_SITE)


ROOT, ALICE, BOB = {"X-Test-User": "root"}, {"X-Test-User": "alice"}, {"X-Test-User": "bob"}


def test_an_admin_makes_tests_and_publishes_a_system(deployment, tmp_path):
    client = client_for(deployment, tmp_path)
    body = {"key": "writers", "name": "Writers", "description": "", "source": "base", "agents": ["writer"]}

    assert client.post("/api/systems/created", headers=ALICE, json=body).status_code == 403
    created = client.post("/api/systems/created", headers=ROOT, json=body)
    assert created.status_code == 201, created.text
    assert client.post("/api/systems/created", headers=ROOT, json=body).status_code == 400

    detail = client.get("/api/systems/created/writers", headers=ROOT).json()
    assert detail["status"] == "draft"
    assert detail["inspection"]["loaded"] and detail["inspection"]["config"] == []
    assert [agent["key"] for agent in detail["inspection"]["agents"]] == ["writer"]
    assert client.get("/api/systems/created/writers", headers=ALICE).status_code == 403

    # A draft is in the admin's picker only.
    assert "writers" in [s["key"] for s in client.get("/api/chat/bootstrap", headers=ROOT).json()["systems"]]
    assert "writers" not in [s["key"] for s in client.get("/api/chat/bootstrap", headers=ALICE).json()["systems"]]

    refused = client.post("/api/systems/created/writers/status", headers=ROOT, json={"status": "published"})
    assert refused.status_code == 400 and "description" in refused.json()["detail"]
    broken = client.put("/api/systems/created/writers/config", headers=ROOT, json={"yaml": "agents: [1, 2]"})
    assert broken.status_code == 400
    assert client.patch("/api/systems/created/writers", headers=ROOT, json={"description": "Plain writing"}).status_code == 200
    assert client.post("/api/systems/created/writers/status", headers=ROOT, json={"status": "published"}).status_code == 200

    # Published: everyone's router offers it.
    assert "writers" in [s["key"] for s in client.get("/api/chat/bootstrap", headers=ALICE).json()["systems"]]
    library = client.get("/api/systems", headers=ALICE).json()
    assert any(item["kind"] == "library" and item["key"] == "writers" for item in library["items"])
    public = client.get("/api/systems/library/writers", headers=ALICE).json()
    assert public["inspection"]["loaded"] and "config_yaml" not in public and "config_path" not in public
    assert public["activity"]["recent"] == [] and public["activity"]["events"] == []
    events = client.get("/api/systems/created/writers", headers=ROOT).json()["activity"]["events"]
    assert [event["event"] for event in events] == ["published", "described", "created"]
    assert client.delete("/api/systems/created/writers", headers=ROOT).status_code == 400


def test_a_user_builds_submits_and_an_admin_imports(deployment, tmp_path):
    client = client_for(deployment, tmp_path)

    overview = client.get("/api/systems", headers=ALICE).json()
    assert overview["admin"] is False and overview["user_systems"]["enabled"] is True
    [base] = overview["user_systems"]["bases"]
    assert [template["agent"] for template in base["templates"]] == ["helper", "writer"]

    created = client.post("/api/systems/mine", headers=ALICE, json=user_spec().model_dump())
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    assert client.get(f"/api/systems/mine/{key}", headers=BOB).status_code == 404
    refused = client.post("/api/systems/mine", headers=ALICE, json=user_spec(base="elsewhere").model_dump())
    assert refused.status_code == 400

    assert client.post(f"/api/systems/mine/{key}/active", headers=ALICE, json={"active": True}).status_code == 200
    submitted = client.post(f"/api/systems/mine/{key}/submit", headers=ALICE, json={"note": "tested on 5 tasks"})
    assert submitted.status_code == 200, submitted.text
    again = client.post(f"/api/systems/mine/{key}/submit", headers=ALICE, json={"note": ""})
    assert again.status_code == 400

    admin_view = client.get("/api/systems", headers=ROOT).json()
    [submission] = [item for item in admin_view["items"] if item["kind"] == "submission"]
    assert submission["by"] == "alice"
    assert client.post(f"/api/systems/submissions/{submission['key']}/import", headers=ALICE, json={"key": "research"}).status_code == 403
    imported = client.post(f"/api/systems/submissions/{submission['key']}/import", headers=ROOT, json={"key": "research"})
    assert imported.status_code == 201, imported.text
    assert imported.json()["origin"] == {"kind": "user", "user_id": "alice", "username": "alice", "source": key}

    mine = client.get(f"/api/systems/mine/{key}", headers=ALICE).json()
    assert mine["submission"]["state"] == "imported" and mine["submission"]["system_key"] == "research"
    detail = client.get("/api/systems/created/research", headers=ROOT).json()
    assert detail["inspection"]["config"] == []


def test_a_private_builder_system_is_frozen_and_imported_as_admin_draft(deployment, tmp_path, monkeypatch):
    client = client_for(deployment, tmp_path)
    alice = space_of(deployment, "alice")
    access = alice._builder_access()
    source = alice.built_systems
    config = yaml.safe_load(BASE)
    config["settings"]["allow_path_override"] = True
    config["settings"]["config_directory"] = "."
    config["providers"] = access.providers()
    config["tools"] = {}
    for agent in config["agents"].values():
        agent["tools"] = []
    config["models"] = {key: value for key, value in config["models"].items()
                        if key in deployment.config.config.models}
    text = yaml.safe_dump(config)
    access.validate(text, "private-team")
    # What runs unjudged is the operator's to say, never a private system's.
    for setting in ({"tool_effects": {"remote_*": "read"}}, {"filters": {"open": {"label": "Open", "exec": "allow"}}, "default_filter": "open"}):
        loose = yaml.safe_load(text)
        loose["settings"]["action_policy"] = setting
        with pytest.raises(StoreError, match="inherit the server's policy filters"):
            access.validate(yaml.safe_dump(loose), "private-team")
    source.create(manifest("private-team"), text)
    directory = source.directory("private-team")

    refused = client.post("/api/systems/built/private-team/submit", headers=ALICE, json={"note": "ready"})
    assert refused.status_code == 400 and "quality.yaml" in refused.text
    (directory / "quality.yaml").write_text("test contract\n", encoding="utf-8")
    save_evidence(directory, "evaluation", {"revision": "example", "passed": True})
    monkeypatch.setattr("web_chat.system_hub.require_quality", lambda root: None)
    submitted = client.post("/api/systems/built/private-team/submit", headers=ALICE, json={"note": "tested"})
    assert submitted.status_code == 200, submitted.text
    submission_id = submitted.json()["submission"]["id"]
    assert (evidence_dir(tmp_path / "submissions" / submission_id) / "evaluation.json").is_file()
    assert submission_id.startswith("s")
    assert client.get(f"/api/systems/submission/{submission_id}", headers=BOB).status_code == 403
    assert client.get(f"/api/systems/submission/{submission_id}", headers=ROOT).json()["inspection"]["loaded"]
    assert client.post("/api/systems/built/private-team/submit", headers=ALICE, json={"note": "again"}).status_code == 400

    snapshot = tmp_path / "submissions" / submission_id / "config.yaml"
    frozen = snapshot.read_bytes()
    (directory / "config.yaml").write_text("broken: true\n", encoding="utf-8")
    assert snapshot.read_bytes() == frozen
    snapshot.write_text("broken: true\n", encoding="utf-8")
    tampered = client.post(f"/api/systems/submissions/{submission_id}/import", headers=ROOT,
                           json={"key": "shared-team"})
    assert tampered.status_code == 400 and "changed" in tampered.text
    assert client.get("/api/systems/created/shared-team", headers=ROOT).status_code == 404
    snapshot.write_bytes(frozen)
    imported = client.post(f"/api/systems/submissions/{submission_id}/import", headers=ROOT,
                           json={"key": "shared-team"})
    assert imported.status_code == 201, imported.text
    assert imported.json()["status"] == "draft"
    assert client.get("/api/systems/created/shared-team", headers=ALICE).status_code == 403
    assert client.get("/api/systems/created/shared-team", headers=ROOT).json()["inspection"]["loaded"]
    assert client.get("/api/systems/built/private-team", headers=ALICE).json()["submission"]["state"] == "imported"
    (directory / "config.yaml").write_bytes(frozen)
    second = client.post("/api/systems/built/private-team/submit", headers=ALICE, json={"note": "again"})
    assert second.status_code == 200, second.text
    assert client.delete("/api/systems/built/private-team/submit", headers=BOB).status_code == 404
    assert client.delete("/api/systems/built/private-team/submit", headers=ALICE).json()["submission"]["state"] == "withdrawn"


def test_library_hides_other_users_activity_and_private_drafts(deployment, tmp_path):
    store = deployment.created_store
    store.create(manifest("public-team"), BASE)
    store.set_status("public-team", "published")
    store.create(manifest("secret-draft"), BASE)
    activity = SystemActivity(tmp_path / "activity.json")
    activity.record_turn("public-team", agent="helper", outcome="answered", duration_ms=10, user_id="bob")
    activity.record_event("public-team", "admin private note", by="root", note="not for users")
    client = client_for(deployment, tmp_path)

    public = client.get("/api/systems/library/public-team", headers=ALICE).json()
    assert public["activity"]["turns"] == 1
    assert public["activity"]["recent"] == [] and public["activity"]["events"] == []
    assert client.get("/api/systems/library/secret-draft", headers=ALICE).status_code == 404
    assert client.get("/api/systems/created/secret-draft", headers=ALICE).status_code == 403
    assert client.get("/api/systems/created/public-team", headers=ROOT).json()["activity"]["recent"][0]["user"] == "bob"


def test_the_routing_probe_puts_the_tested_system_among_the_candidates(deployment, tmp_path, monkeypatch):
    client = client_for(deployment, tmp_path)
    key = client.post("/api/systems/mine", headers=ALICE, json=user_spec().model_dump()).json()["key"]
    seen = {}

    async def choose(self, message, candidates, default, previous):
        seen["candidates"] = dict(candidates)
        return key if "market" in message else "base"

    from web_chat.systems import SystemRegistry

    monkeypatch.setattr(SystemRegistry, "_choose", choose)
    probe = client.post(
        "/api/systems/probe",
        headers=ALICE,
        json={"kind": "mine", "key": key, "messages": ["compare market prices", "fix my test", " "]},
    ).json()
    assert [result["hit"] for result in probe["results"]] == [True, False]
    assert probe["hits"] == 1
    assert set(seen["candidates"]) == {"base", key}
    assert client.post("/api/systems/probe", headers=ALICE, json={"kind": "created", "key": "x", "messages": ["a"]}).status_code == 403
