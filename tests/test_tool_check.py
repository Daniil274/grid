"""Tests for the pre-run tool health check (core.tool_check)."""

from types import SimpleNamespace

import pytest
import yaml

from core.config import Config
from core.managers.project_tools_loader import set_project_loader
from core.routing import check_system
from core.tool_check import CONFIG, ENVIRONMENT, agent_issues, diagnose, reachable_agents, summarize
from tools.beads_tools import _resolve_directory
from utils.tool_requirements import Requires, unmet

GOOD_TOOL = '''
from agents import function_tool
from utils.tool_requirements import Requires

TOOL_REQUIREMENTS = {
    "needs_key": Requires(env=("GRID_TEST_MISSING_KEY",), hint="export GRID_TEST_MISSING_KEY"),
    "needs_program": Requires(programs=("definitely-not-a-real-program",), hint="install it"),
}


@function_tool
def needs_key() -> str:
    """Needs a key."""
    return "ok"


@function_tool
def needs_program() -> str:
    """Needs a program."""
    return "ok"
'''

# Declares its requirement before the import that fails, like the Windows tools.
EXPLAINED_FAILURE = '''
from utils.tool_requirements import Requires

TOOL_REQUIREMENTS = {"far_tool": Requires(platform="plan9", hint="run it on Plan 9")}

import definitely_not_a_real_module  # noqa: E402
'''

BARE_FAILURE = "import definitely_not_a_real_module\n"


def _write(path, data):
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _config(tmp_path, *, agents, tools, api_key="k", mcp_enabled=False):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / "good.py").write_text(GOOD_TOOL, encoding="utf-8")
    (tools_dir / "far_tool.py").write_text(EXPLAINED_FAILURE, encoding="utf-8")
    (tools_dir / "broken_tool.py").write_text(BARE_FAILURE, encoding="utf-8")
    provider = {"name": "p", "base_url": "http://localhost"}
    provider.update({"api_key": api_key} if api_key else {"api_key_env": "GRID_TEST_MISSING_KEY"})
    data = {
        "settings": {
            "default_agent": next(iter(agents)),
            "mcp_enabled": mcp_enabled,
            "project_tools": {"enabled": True, "tools_directory": "./tools"},
        },
        "providers": {"p": provider},
        "models": {"m": {"name": "m", "provider": "p"}},
        "tools": tools,
        "agents": {
            key: {"name": key, "model": "m", "description": f"{key} agent", "tools": agent_tools}
            for key, agent_tools in agents.items()
        },
    }
    try:
        return Config(str(_write(tmp_path / "config.yaml", data)))
    finally:
        set_project_loader(None)


def _fn(*names):
    return {name: {"type": "function", "description": name} for name in names}


@pytest.fixture(autouse=True)
def _no_test_key(monkeypatch):
    monkeypatch.delenv("GRID_TEST_MISSING_KEY", raising=False)


def test_unmet_requirements_are_environment_issues_with_hints(tmp_path):
    config = _config(tmp_path, agents={"a": ["needs_key", "needs_program"]}, tools=_fn("needs_key", "needs_program"))

    issues = diagnose(config)

    by_tool = {issue.tool: issue for issue in issues}
    assert by_tool["needs_key"].kind == ENVIRONMENT
    assert "GRID_TEST_MISSING_KEY" in by_tool["needs_key"].problem
    assert by_tool["needs_key"].hint == "export GRID_TEST_MISSING_KEY"
    assert "definitely-not-a-real-program" in by_tool["needs_program"].problem
    # Environment problems never fail the config check the administrator relies on.
    assert check_system(config) == []


def test_tools_with_problems_stay_available_to_the_agent(tmp_path):
    config = _config(tmp_path, agents={"a": ["needs_key"]}, tools=_fn("needs_key"))
    assert diagnose(config)

    assert config.project_tools_loader.has_tool("needs_key")


def test_failed_import_is_explained_by_requirements_declared_before_it(tmp_path):
    config = _config(tmp_path, agents={"a": ["far_tool"]}, tools=_fn("far_tool"))

    [issue] = diagnose(config)

    assert issue.kind == ENVIRONMENT and issue.tool == "far_tool"
    assert issue.problem.startswith("not loaded, works only on plan9")
    assert issue.hint == "run it on Plan 9"


def test_failed_import_without_requirements_shows_the_import_error(tmp_path):
    (tmp_path / "requirements.txt").write_text("somepackage\n", encoding="utf-8")
    config = _config(tmp_path, agents={"a": ["broken_tool"]}, tools=_fn("broken_tool"))

    [issue] = diagnose(config)

    assert issue.kind == ENVIRONMENT
    assert "broken_tool.py failed to import" in issue.problem
    assert "definitely_not_a_real_module" in issue.problem
    assert issue.hint.startswith("pip install -r ") and issue.hint.endswith("requirements.txt")


def test_unimplemented_tool_names_the_files_that_failed_to_import(tmp_path):
    config = _config(tmp_path, agents={"a": ["ghost"]}, tools=_fn("ghost"))

    [issue] = diagnose(config)

    assert issue.kind == CONFIG
    assert "is not implemented" in issue.problem
    assert "broken_tool.py" in issue.problem and "far_tool.py" in issue.problem
    assert check_system(config) == [issue.text()]


def test_agent_tool_without_target_calls_the_agent_named_like_the_tool(tmp_path):
    tools = {
        "helper": {"type": "agent", "description": "calls helper"},
        "ghost_helper": {"type": "agent", "description": "calls nobody"},
    }
    config = _config(tmp_path, agents={"a": ["helper", "ghost_helper"], "helper": []}, tools=tools)

    [issue] = diagnose(config)

    assert issue.kind == CONFIG and issue.tool == "ghost_helper"
    assert "calls unknown agent 'ghost_helper'" in issue.problem


def test_mcp_tool_of_an_agent_without_mcp_is_reported(tmp_path):
    tools = {"srv": {"type": "mcp", "description": "server", "server_command": ["definitely-not-a-real-program"]}}
    config = _config(tmp_path, agents={"a": ["srv"]}, tools=tools)

    problems = {issue.problem for issue in diagnose(config)}

    assert any("MCP is off" in problem for problem in problems)
    assert any("needs 'definitely-not-a-real-program' on PATH" in problem for problem in problems)


def test_missing_api_key_means_the_agent_and_its_callers_cannot_run(tmp_path):
    tools = {"call_b": {"type": "agent", "target_agent": "b", "description": "calls b"}}
    config = _config(tmp_path, agents={"a": ["call_b"], "b": []}, tools=tools, api_key=None)

    issues = diagnose(config)

    assert {issue.agent for issue in issues if "cannot run" in issue.problem} == {"a", "b"}
    assert any(issue.tool == "call_b" and "subagent 'b'" in issue.problem for issue in issues)
    assert all(issue.kind == ENVIRONMENT for issue in issues)


def test_agent_issues_cover_the_subagents_it_can_call(tmp_path):
    tools = {
        "call_b": {"type": "agent", "target_agent": "b", "description": "calls b"},
        **_fn("needs_key", "needs_program"),
    }
    config = _config(tmp_path, agents={"a": ["call_b"], "b": ["needs_key"], "c": ["needs_program"]}, tools=tools)
    issues = diagnose(config)

    assert reachable_agents(config, "a") == ["a", "b"]
    assert {issue.tool for issue in agent_issues(config, issues, "a")} == {"needs_key"}


def test_summary_merges_one_tool_failing_for_several_agents(tmp_path):
    config = _config(tmp_path, agents={"a": ["needs_key"], "b": ["needs_key"]}, tools=_fn("needs_key"))

    [line] = summarize(diagnose(config))

    assert line.startswith("tool 'needs_key' (agents a, b): environment variable not set")
    assert line.endswith("(fix: export GRID_TEST_MISSING_KEY)")


def test_unreachable_service_is_reported():
    # Port 9 (discard) is closed on test machines; the probe must not hang.
    requires = Requires(service=("GRID_TEST_SERVICE_URL", "http://127.0.0.1:9"))

    [reason] = unmet(requires)

    assert "http://127.0.0.1:9" in reason and "not reachable" in reason


def test_custom_check_and_platform():
    assert unmet(Requires(check=lambda: "down for maintenance")) == ["down for maintenance"]
    assert unmet(Requires(platform="plan9"))[0].startswith("works only on plan9")
    assert unmet(Requires()) == []


def test_beads_directory_is_relative_to_the_working_directory(tmp_path):
    factory = SimpleNamespace(config=SimpleNamespace(get_working_directory=lambda: str(tmp_path)), container_id=None)
    context = SimpleNamespace(context=SimpleNamespace(factory=factory))

    assert _resolve_directory(context, ".") == str(tmp_path.resolve())
    assert _resolve_directory(context, "sub") == str((tmp_path / "sub").resolve())
    with pytest.raises(ValueError):
        _resolve_directory(context, "../outside")
