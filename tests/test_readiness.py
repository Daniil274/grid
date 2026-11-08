"""Readiness: declared system skills the system does not have, and init_tools scope.

A ``system_skills`` name with no file is not a warning: the agent would run
without the instructions its system promises. ``core.tool_check.readiness``
(the same rule ``web_chat.systems.SystemRegistry.resolve`` applies before any
model runs) reports it, and ``core.system_store.inspect_system`` marks the
system not ready. Explicit ``init_tools`` may only use tools the target was
given (plus, for a configured agent, its own auto_run_tools); nothing is
resolved from the registry just because an init_tool names it.

Run explicitly: ``pytest tests/test_readiness.py``.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from core.agent_factory import AgentFactory
from core.config import Config
from core.system_store import inspect_system
from core.tool_check import CONFIG, readiness, run_readiness


PROVIDERS = """
models:
  m1: {name: model-one, provider: openrouter}
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
  mcp_enabled: false
isolation:
  enabled: false
prompt_templates:
  base: You are a careful assistant.
tools: {}
agents:
  helper:
    name: Helper
    description: Does the work
    model: m1
""" + PROVIDERS


def write_config(tmp_path, *, skills=("present",), declared=("present",)):
    directory = tmp_path / "system"
    (directory / "skills").mkdir(parents=True)
    document = yaml.safe_load(BASE)
    document["agents"]["helper"]["system_skills"] = list(declared)
    (directory / "config.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    for name in skills:
        (directory / "skills" / f"{name}.md").write_text("Do it plainly.", encoding="utf-8")
    return Config(str(directory / "config.yaml"))


def test_missing_skill_is_a_config_readiness_issue(tmp_path):
    # "present" has its file; only "absent" is declared without one, so the
    # single issue is about exactly that name.
    config = write_config(tmp_path, skills=("present",), declared=("present", "absent"))

    [issue] = readiness(config)
    assert issue.kind == CONFIG
    assert issue.agent == "helper"
    assert "absent" in issue.text()
    assert config.missing_system_skills("helper") == ["absent"]


def test_a_present_skill_is_ready(tmp_path):
    config = write_config(tmp_path, skills=("present",), declared=("present",))
    assert readiness(config) == []
    assert run_readiness(config, "helper") == []


def test_inspect_reports_a_missing_skill_and_is_not_ready(tmp_path):
    config = write_config(tmp_path, skills=(), declared=("present",))

    report = inspect_system(lambda: config)

    assert report["loaded"] is True
    assert report["ready"] is False
    assert any("present" in gap for gap in report["readiness"])


def test_run_readiness_only_covers_reachable_agents(tmp_path):
    config = write_config(tmp_path, skills=(), declared=("present",))
    assert run_readiness(config, "helper") == readiness(config)
    # An agent the system does not have cannot be judged reachable.
    assert run_readiness(config, "nobody") == []


# -- init_tools scope -------------------------------------------------------


def dynamic_factory(tools):
    factory = object.__new__(AgentFactory)
    factory.action_gate = None
    factory._stream_observer = None
    factory.container_id = None
    factory.config = SimpleNamespace(get_working_directory=lambda: ".")
    factory._resolve_tools_for_names = AsyncMock(return_value=([], [], []))
    factory._tools = tools
    return factory


def test_a_dynamic_agents_init_tools_cannot_reach_an_unissued_tool():
    issued = SimpleNamespace(name="lazy_get", on_invoke_tool=AsyncMock(return_value="ok"))
    factory = dynamic_factory([issued])

    pool = asyncio.run(
        factory._resolve_init_tool_pool(
            [{"name": "lazy_preview", "parameters": {}}], [issued]
        )
    )

    assert [tool.name for tool in pool] == ["lazy_get"]
    factory._resolve_tools_for_names.assert_not_awaited()


def test_an_unissued_init_tool_is_refused_not_run():
    issued = SimpleNamespace(name="lazy_get", on_invoke_tool=AsyncMock(return_value="ok"))

    async def scenario():
        factory = dynamic_factory([issued])
        pool = await factory._resolve_init_tool_pool(
            [{"name": "lazy_preview", "parameters": {}}], [issued]
        )
        return await factory._execute_init_tools(
            [{"name": "lazy_preview", "parameters": {}}], pool, SimpleNamespace()
        )

    info = asyncio.run(scenario())

    assert not info.ok
    assert "lazy_preview" in info.failure_report()
    assert "not available" in info.failure_report()
    issued.on_invoke_tool.assert_not_awaited()


def test_an_issued_init_tool_runs():
    issued = SimpleNamespace(name="lazy_get", on_invoke_tool=AsyncMock(return_value="found"))

    async def scenario():
        factory = dynamic_factory([issued])
        pool = await factory._resolve_init_tool_pool(
            [{"name": "lazy_get", "parameters": {}}], [issued]
        )
        return await factory._execute_init_tools(
            [{"name": "lazy_get", "parameters": {}}], pool, SimpleNamespace()
        )

    info = asyncio.run(scenario())

    assert info.ok
    assert "=== CONTEXT [lazy_get] ===" in info
    issued.on_invoke_tool.assert_awaited_once()


def test_a_configured_agents_own_auto_run_tools_join_the_pool():
    issued = SimpleNamespace(name="lazy_get", on_invoke_tool=AsyncMock(return_value="ok"))
    auto = SimpleNamespace(name="beads_init", on_invoke_tool=AsyncMock(return_value="ok"))
    factory = dynamic_factory([issued])
    factory._resolve_tools_for_names = AsyncMock(return_value=([auto], [], []))
    factory.config = SimpleNamespace(
        get_working_directory=lambda: ".",
        get_agent=lambda key: SimpleNamespace(
            auto_run_tools=[{"name": "beads_init"}]
        ),
    )

    pool = asyncio.run(
        factory._resolve_init_tool_pool(
            [{"name": "beads_init", "parameters": {}}], [issued], agent_key="helper"
        )
    )

    assert {tool.name for tool in pool} == {"lazy_get", "beads_init"}
    factory._resolve_tools_for_names.assert_awaited_once_with(["beads_init"])
