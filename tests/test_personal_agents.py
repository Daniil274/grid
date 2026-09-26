"""Personal agents: derived from an operator's template, never beyond it, one space only."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_chat.deployment import Deployment
from web_chat.identity import User
from web_chat.personal_agents import OWNER_INSTRUCTIONS, PersonalAgentError, PersonalAgentSpec
from web_chat.server import WebChatServer
from web_chat.space import SpaceLayout, UserSpace
from web_chat.spaces import SpacePool

SAME_SITE = {"Origin": "http://testserver"}

CONFIG = """
settings:
  default_agent: helper
  working_directory: .
  allow_path_override: true
  mcp_enabled: false
isolation:
  enabled: false
personal_agents:
  enabled: {enabled}
  templates:
    {system}: [helper]
  models: [m2]
  max_agents: 2
  max_instructions_chars: 200
prompt_templates:
  base: You are a careful assistant.
tools:
  file_read:
    type: function
    description: Read a file
  bash_tool:
    type: function
    description: Run a command
agents:
  helper:
    name: Helper
    model: m1
    tools: [file_read, bash_tool]
  admin_only:
    name: Admin only
    model: m1
    tools: [bash_tool]
models:
  m1:
    name: model-one
    provider: openrouter
  m2:
    name: model-two
    provider: openrouter
  m3:
    name: model-three
    provider: openrouter
providers:
  openrouter:
    name: openrouter
    base_url: https://example.com/v1
    api_key_env: TEST_OPENROUTER_KEY
"""


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    system = tmp_path / "solo"
    system.mkdir()
    (system / "config.yaml").write_text(CONFIG.format(enabled="true", system="solo"), encoding="utf-8")
    return Deployment(config_path=str(system / "config.yaml"), routing_path=None)


def space_of(deployment, user_id: str) -> UserSpace:
    root = Path(deployment.config_path).parent.parent / "users" / user_id
    return UserSpace(deployment, user_id=user_id, layout=SpaceLayout.under(root))


def spec(**changes) -> PersonalAgentSpec:
    fields = {"name": "Reviewer", "system": "solo", "template": "helper", "instructions": "Review code only."}
    return PersonalAgentSpec(**{**fields, **changes})


def create(space: UserSpace, **changes):
    return space.change_personal_agents(lambda agents: agents.create(spec(**changes), space.registry.config))


# -- deriving --------------------------------------------------------------------


def test_an_agent_keeps_its_template_and_adds_the_owners_instructions(deployment):
    space = space_of(deployment, "u1")

    agent = create(space, tools=["file_read"], model="m2")
    derived = space.registry.agents("solo")[agent.key]

    assert agent.key.startswith("my_")
    assert derived.name == "Reviewer"
    assert derived.tools == ["file_read"]
    assert derived.model == "m2"
    assert derived.custom_prompt.startswith("You are a careful assistant.")
    assert derived.custom_prompt.endswith(OWNER_INSTRUCTIONS + "Review code only.")
    assert derived.routable is False


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"template": "admin_only"}, "cannot be used as a template"),
        ({"tools": ["file_read", "web_fetch"]}, "only use tools of its template"),
        ({"model": "m3"}, "model is not available"),
        ({"system": "elsewhere"}, "cannot be used as a template"),
        ({"instructions": "x" * 201}, "limited to 200"),
    ],
)
def test_nothing_beyond_the_policy_and_the_template_is_allowed(deployment, changes, message):
    space = space_of(deployment, "u1")

    with pytest.raises(PersonalAgentError, match=message):
        create(space, **changes)
    assert space.personal_agents.list() == []


def test_the_number_of_agents_is_limited(deployment):
    space = space_of(deployment, "u1")
    create(space)
    create(space)

    with pytest.raises(PersonalAgentError, match="at most 2"):
        create(space)


def test_an_edit_applies_to_the_next_turn(deployment):
    space = space_of(deployment, "u1")
    agent = create(space)
    factory = space.registry.factory("solo")
    factory._agent_cache[agent.key] = object()  # built for an earlier turn

    space.change_personal_agents(
        lambda agents: agents.update(agent.key, spec(name="Tester", instructions="Write tests."), space.registry.config)
    )

    assert space.registry.agents("solo")[agent.key].name == "Tester"
    assert agent.key not in factory._agent_cache


def test_a_deleted_agent_leaves_the_configs(deployment):
    space = space_of(deployment, "u1")
    agent = create(space)

    space.change_personal_agents(lambda agents: agents.delete(agent.key))

    assert agent.key not in space.registry.agents("solo")
    assert "helper" in space.registry.agents("solo")


# -- one space only, kept across restarts ----------------------------------------


def test_an_agent_exists_only_in_its_owners_space(deployment):
    alice, bob = space_of(deployment, "alice"), space_of(deployment, "bob")

    agent = create(alice)

    assert agent.key in alice.registry.agents("solo")
    assert agent.key not in bob.registry.agents("solo")
    with pytest.raises(PersonalAgentError, match="No such agent"):
        bob.change_personal_agents(lambda agents: agents.delete(agent.key))


def test_agents_are_loaded_again_by_a_new_space(deployment):
    agent = create(space_of(deployment, "u1"))

    rebuilt = space_of(deployment, "u1")

    assert rebuilt.registry.agents("solo")[agent.key].name == "Reviewer"


def test_a_withdrawn_template_takes_its_agents_out(deployment, tmp_path):
    agent = create(space_of(deployment, "u1"))
    config = Path(deployment.config_path)
    config.write_text(config.read_text(encoding="utf-8").replace("[helper]", "[]"), encoding="utf-8")
    deployment.load()

    rebuilt = space_of(deployment, "u1")

    assert agent.key not in rebuilt.registry.agents("solo")
    assert [kept.key for kept in rebuilt.personal_agents.list()] == [agent.key]  # kept, not applied


def test_a_single_user_space_has_no_personal_agents(deployment):
    space = UserSpace(deployment)

    with pytest.raises(PersonalAgentError, match="server with accounts"):
        space.change_personal_agents(lambda agents: agents.list())


# -- over HTTP -------------------------------------------------------------------


def two_user_client(deployment):
    async def identify(connection):
        name = connection.headers["X-Test-User"]
        return User(id=name, username=name, role="user")

    server = WebChatServer(deployment, SpacePool(lambda user_id: space_of(deployment, user_id)), identify=identify, warm_user=None)
    return TestClient(server.app, headers=SAME_SITE)


def test_the_agents_api(deployment):
    client = two_user_client(deployment)
    alice, bob = {"X-Test-User": "alice"}, {"X-Test-User": "bob"}

    offered = client.get("/api/agents", headers=alice).json()
    assert offered["enabled"] is True
    [template] = offered["templates"]
    assert (template["system"], template["agent"]) == ("solo", "helper")
    assert [model["key"] for model in template["models"]] == ["m1", "m2"]
    assert [tool["key"] for tool in template["tools"]] == ["file_read", "bash_tool"]

    created = client.post("/api/agents", headers=alice, json=spec().model_dump())
    assert created.status_code == 201
    key = created.json()["key"]

    systems = client.get("/api/chat/bootstrap", headers=alice).json()["systems"]
    mine = {agent["key"]: agent for agent in systems[0]["agents"]}
    assert mine[key]["personal"] is True and mine["helper"]["personal"] is False

    assert client.put(f"/api/agents/{key}", headers=bob, json=spec().model_dump()).status_code == 404
    assert client.delete(f"/api/agents/{key}", headers=bob).status_code == 404
    refused = client.post("/api/agents", headers=alice, json=spec(model="m3").model_dump())
    assert refused.status_code == 400 and "model" in refused.json()["detail"]
    assert client.delete(f"/api/agents/{key}", headers=alice).status_code == 200
    assert client.get("/api/agents", headers=alice).json()["agents"] == []


def test_the_api_says_when_personal_agents_are_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    (tmp_path / "config.yaml").write_text(CONFIG.format(enabled="false", system="solo"), encoding="utf-8")
    deployment = Deployment(config_path=str(tmp_path / "config.yaml"), routing_path=None)
    client = two_user_client(deployment)

    offered = client.get("/api/agents", headers={"X-Test-User": "alice"}).json()
    created = client.post("/api/agents", headers={"X-Test-User": "alice"}, json=spec().model_dump())

    assert offered["enabled"] is False and offered["templates"] == []
    assert created.status_code == 400 and "not enabled" in created.json()["detail"]


def test_users_who_are_not_admins_see_no_server_paths(deployment):
    client = two_user_client(deployment)

    boot = client.get("/api/chat/bootstrap", headers={"X-Test-User": "alice"}).json()

    assert boot["workspace_path"] == ""
    assert all(system["config_path"] == "" and system["description"] == "" for system in boot["systems"])
