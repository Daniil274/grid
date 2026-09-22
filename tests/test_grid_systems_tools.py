"""Tests for the administrator's read-only view of Grid systems."""

import json
from pathlib import Path

import pytest
import yaml

from core.config import Config
from core.routing import check_system
from tools.grid_systems_tools import grid_check_system, grid_systems_catalog

REPO = Path(__file__).resolve().parents[1]


def _system(agents):
    return {
        "settings": {"default_agent": next(iter(agents))},
        "providers": {"p": {"name": "p", "base_url": "http://localhost", "api_key": "k"}},
        "models": {"m": {"name": "m", "provider": "p"}},
        "tools": {"file_read": {"type": "function", "description": "Read a file"}},
        "agents": {
            key: {"name": key, "model": "m", "description": desc, "tools": ["file_read"]}
            for key, desc in agents.items()
        },
    }


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository with one registered system and one unregistered, broken one."""
    _write(tmp_path / "examples" / "notes" / "config.yaml", _system({"writer": "Writes notes"}))
    broken = _system({"helper": ""})
    broken["agents"]["helper"]["system_skills"] = ["missing"]
    _write(tmp_path / "examples" / "draft" / "config.yaml", broken)
    _write(
        tmp_path / "routing.yaml",
        {
            "routing": {
                "default_system": "notes",
                "systems": {"notes": {"config": "examples/notes/config.yaml", "description": "Notes"}},
            }
        },
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def _call(tool, **arguments):
    return json.loads(await tool.on_invoke_tool(None, json.dumps(arguments)))


async def test_catalog_lists_registered_systems_with_agents(repo):
    catalog = await _call(grid_systems_catalog)

    assert catalog["default_system"] == "notes"
    [notes] = catalog["systems"]
    assert notes["name"] == "notes" and notes["description"] == "Notes"
    assert notes["default_agent"] == "writer"
    assert notes["agents"][0]["key"] == "writer"
    assert notes["agents"][0]["tools"] == ["file_read"]


async def test_check_reports_healthy_registered_system(repo):
    report = await _call(grid_check_system, config_path="examples/notes/config.yaml")

    assert report == {
        "config": report["config"],
        "registered_as": "notes",
        "healthy": True,
        "issues": [],
    }


async def test_check_reports_problems_of_unregistered_system(repo):
    report = await _call(grid_check_system, config_path="examples/draft/config.yaml")

    assert report["registered_as"] is None
    assert report["healthy"] is False
    assert any("no description" in issue for issue in report["issues"])
    assert any("missing skill 'missing'" in issue for issue in report["issues"])


async def test_check_reports_missing_and_unloadable_configs(repo):
    missing = await _call(grid_check_system, config_path="examples/ghost/config.yaml")
    assert missing["issues"] == ["config file not found"]

    (repo / "examples" / "notes" / "config.yaml").write_text("agents: [broken", encoding="utf-8")
    unloadable = await _call(grid_check_system, config_path="examples/notes/config.yaml")
    assert unloadable["healthy"] is False
    assert unloadable["issues"][0].startswith("config failed to load")


def test_system_admin_is_healthy_and_only_reachable_in_the_workshop():
    config = Config(str(REPO / "examples" / "system-admin" / "config.yaml"))
    routing = Config(str(REPO / "routing.yaml")).config.routing

    assert check_system(config) == []
    assert [key for key, agent in config.config.agents.items() if agent.routable] == ["administrator"]
    # It changes Grid, so the host router must never send a message to it.
    assert all("system-admin" not in system.config for system in routing.systems.values())
    # Commits happen only through the workshop's control_submit.
    tools = {tool for agent in config.config.agents.values() for tool in agent.tools}
    assert {"git_commit", "git_add_file", "git_add_all"}.isdisjoint(tools)
