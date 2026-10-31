"""Project tools: idempotent, tenant-bound by the host's factory, no model-named identity."""

import json
import threading
from types import SimpleNamespace

import pytest

from tools import project_tools
from web_chat.projects import ProjectConflict, ProjectParentNotFound, ProjectStore


def _ctx(store):
    return SimpleNamespace(context=SimpleNamespace(factory=SimpleNamespace(project_store=store)))


async def _call(tool, store, **arguments):
    return json.loads(await tool.on_invoke_tool(_ctx(store), json.dumps(arguments)))


def test_ensure_is_idempotent_and_never_overwrites(tmp_path):
    store = ProjectStore(tmp_path / "projects.json")
    first, created = store.ensure("  Мой  проект ", description="one")
    again, created_again = store.ensure("мой проект", description="other")
    assert created and not created_again
    assert again.id == first.id and again.description == "one"
    assert len(store.list()) == 1


def test_ensure_same_title_under_other_parent_is_distinct(tmp_path):
    store = ProjectStore(tmp_path / "projects.json")
    root, _ = store.ensure("root")
    child, _ = store.ensure("x", parent_id=root.id)
    top, created = store.ensure("x")
    assert created and top.id != child.id
    with pytest.raises(ProjectParentNotFound):
        store.ensure("y", parent_id="prj-ghost")


def test_ensure_race_makes_one_project(tmp_path):
    store = ProjectStore(tmp_path / "projects.json")
    results = []

    def go():
        results.append(store.ensure("Race")[0].id)

    threads = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(results)) == 1 and len(store.list()) == 1


def test_notes_are_never_replaced_silently(tmp_path):
    store = ProjectStore(tmp_path / "projects.json")
    project, _ = store.ensure("p")
    assert store.add_notes(project.id, "ctx")[1] is True
    assert store.add_notes(project.id, "ctx")[1] is False
    with pytest.raises(ProjectConflict):
        store.add_notes(project.id, "different")
    assert store.get(project.id).notes == "ctx"
    assert store.add_notes(project.id, "more", mode="append")[1] is True
    assert store.get(project.id).notes == "ctx\n\nmore"


async def test_tools_list_ensure_get_and_typed_errors(tmp_path):
    store = ProjectStore(tmp_path / "projects.json")
    made = await _call(project_tools.project_ensure, store, title="A")
    assert made["created"] is True
    pid = made["project"]["id"]
    assert (await _call(project_tools.project_ensure, store, title="a"))["created"] is False
    assert [p["id"] for p in (await _call(project_tools.project_list, store))["projects"]] == [pid]
    await _call(project_tools.project_add_notes, store, project_id=pid, text="n")
    assert (await _call(project_tools.project_get, store, project_id=pid))["project"]["notes"] == "n"
    conflict = await _call(project_tools.project_add_notes, store, project_id=pid, text="other")
    assert conflict["code"] == "conflict" and conflict["isError"]
    assert (await _call(project_tools.project_get, store, project_id="prj-none"))["code"] == "not_found"
    bad = await _call(project_tools.project_ensure, store, title="z", parent_id="prj-none")
    assert bad["code"] == "parent_not_found"
    assert (await _call(project_tools.project_ensure, store, title="  "))["code"] == "title_required"


async def test_tools_are_isolated_between_users(tmp_path):
    a = ProjectStore(tmp_path / "a" / "projects.json")
    b = ProjectStore(tmp_path / "b" / "projects.json")
    await _call(project_tools.project_ensure, a, title="Secret of A")
    assert (await _call(project_tools.project_list, b))["projects"] == []


async def test_no_store_means_refusal_not_a_guess():
    out = json.loads(await project_tools.project_list.on_invoke_tool(SimpleNamespace(context=None), "{}"))
    assert out["isError"] and out["code"] == "unavailable"


def test_tools_take_no_identity_or_path_and_cannot_delete():
    for name, tool in project_tools.PROJECT_TOOLS.items():
        schema = set(tool.params_json_schema.get("properties", {}))
        assert not schema & {"user_id", "path", "directory", "tenant", "workspace"}, name
    assert not any("delete" in name for name in project_tools.PROJECT_TOOLS)
    assert set(project_tools.TOOL_EFFECTS) == set(project_tools.PROJECT_TOOLS)


async def test_standalone_factory_without_store_is_unavailable_not_guessed():
    # grid_mcp / agent_chat build a bare AgentFactory: no user context, so no store.
    ctx = SimpleNamespace(context=SimpleNamespace(factory=SimpleNamespace(project_store=None)))
    out = json.loads(await project_tools.project_ensure.on_invoke_tool(ctx, json.dumps({"title": "X"})))
    assert out["code"] == "unavailable" and out["isError"]


def test_append_dedups_only_whole_blocks_not_substrings(tmp_path):
    store = ProjectStore(tmp_path / "projects.json")
    p = store.create("A")
    store.add_notes(p.id, "alpha beta gamma")
    assert store.add_notes(p.id, "beta", mode="append")[1] is True
    assert store.add_notes(p.id, "beta", mode="append")[1] is False
    assert store.get(p.id).notes == "alpha beta gamma\n\nbeta"


def test_not_found_message_has_no_quotes_or_id(tmp_path):
    from web_chat.projects import ProjectNotFound

    store = ProjectStore(tmp_path / "projects.json")
    with pytest.raises(ProjectNotFound) as info:
        store.add_notes("prj-zzz", "x")
    assert str(info.value) == "Project not found"


async def test_ensure_archived_reports_status_without_duplicate_or_change(tmp_path):
    store = ProjectStore(tmp_path / "projects.json")
    p = store.create("Old", description="d")
    store.update(p.id, archived=True)
    out = await _call(project_tools.project_ensure, store, title="old", description="new")
    assert out["created"] is False and out["status"] == "archived" and out["hint"]
    assert len(store.list()) == 1 and store.get(p.id).archived and store.get(p.id).description == "d"


def test_two_store_objects_on_one_file_make_one_project(tmp_path):
    path = tmp_path / "projects.json"
    stores = [ProjectStore(path) for _ in range(6)]
    results = []
    threads = [threading.Thread(target=lambda s=s: results.append(s.ensure("Same")[1])) for s in stores]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1
    assert len(ProjectStore(path).list()) == 1


def test_write_failure_is_unavailable_and_log_has_no_path(tmp_path, monkeypatch, caplog):
    from web_chat.projects import ProjectUnavailable

    store = ProjectStore(tmp_path / "secretdir" / "projects.json")

    def boom(*a, **k):
        raise OSError(28, "No space")

    monkeypatch.setattr("web_chat.projects.os.replace", boom)
    with caplog.at_level("ERROR"), pytest.raises(ProjectUnavailable):
        store.create("A")
    assert "secretdir" not in caplog.text and "projects.json" in caplog.text


def test_space_wires_its_own_store_into_the_factory(tmp_path, monkeypatch):
    from tests.test_web_space import MINIMAL_CONFIG
    from web_chat.deployment import Deployment
    from web_chat.space import SpaceLayout, UserSpace

    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    spaces = [
        UserSpace(Deployment(config_path=str(config), routing_path=None), user_id=uid,
                  layout=SpaceLayout.under(tmp_path / "users" / uid))
        for uid in ("u1", "u2")
    ]
    f1, f2 = (s.registry.factory(s.registry.default_key()) for s in spaces)
    assert f1.project_store is spaces[0].project_store
    assert f2.project_store is spaces[1].project_store
    assert f1.project_store is not f2.project_store


def test_coder_config_gives_engineer_all_tools_and_general_purpose_read_only():
    import yaml

    agents = yaml.safe_load(open("examples/coder/config.yaml", encoding="utf-8"))["agents"]
    assert {"project_list", "project_get", "project_ensure", "project_add_notes"} <= set(agents["engineer"]["tools"])
    gp = set(agents["general_purpose"]["tools"])
    assert {"project_list", "project_get"} <= gp
    assert not gp & {"project_ensure", "project_add_notes"}


def test_standalone_agent_factory_has_no_project_store():
    # grid_mcp / agent_chat / evals build AgentFactory(config=...) with no space: no identity, no store.
    import inspect

    from core.agent_factory import AgentFactory

    assert "self.project_store = None" in inspect.getsource(AgentFactory.__init__)
