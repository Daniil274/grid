"""An isolating space gives its agents only tools that keep to the user's container or workspace."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agent_factory import AgentFactory
from core.managers.project_tools_loader import ProjectToolsLoader
from tools.function_tools import tool_isolation
from utils.path_utils import reset_current_factory, set_current_factory
from utils.tool_isolation import CONTAINER, WORKSPACE, is_confined

CODER_TOOLS = Path(__file__).resolve().parents[1] / "examples" / "coder" / "tools"


def factory_over(loader, *, confine):
    factory = object.__new__(AgentFactory)
    factory.config = SimpleNamespace(project_tools_loader=loader)
    factory.confine_tools = confine
    factory._wrap_tool_with_output_limit = lambda tool, key: tool
    return factory


@pytest.fixture(scope="module")
def coder_loader():
    loader = ProjectToolsLoader(str(CODER_TOOLS.parent), "./tools")
    loader.load_project_tools()
    return loader


def test_the_shipped_tools_declare_where_they_act(coder_loader):
    assert tool_isolation("file_read") == WORKSPACE
    assert tool_isolation("git_log") == CONTAINER
    assert tool_isolation("bash_tool", coder_loader) == CONTAINER
    assert tool_isolation("grep_tool", coder_loader) == WORKSPACE
    assert tool_isolation("web_fetch", coder_loader) == WORKSPACE
    # Tools that act on the server itself declare nothing: never confined.
    assert not is_confined(tool_isolation("control_trial"))
    assert not is_confined(tool_isolation("grid_systems_catalog"))


def test_an_isolated_factory_withholds_tools_that_act_on_the_host(coder_loader):
    keys = ["bash_tool", "file_read", "control_trial", "grid_systems_catalog"]

    confined = [tool.name for tool in factory_over(coder_loader, confine=True)._resolve_function_tools(keys)]
    open_ = [tool.name for tool in factory_over(coder_loader, confine=False)._resolve_function_tools(keys)]

    assert confined == ["bash_tool", "file_read"]
    assert len(open_) == 4


def test_an_undeclared_project_tool_is_withheld(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "host_things.py").write_text("def reboot():\n    return 'rebooted'\n", encoding="utf-8")
    loader = ProjectToolsLoader(str(tmp_path), "./tools")
    loader.load_project_tools()

    assert factory_over(loader, confine=True)._resolve_function_tools(["reboot"]) == []
    assert len(factory_over(loader, confine=False)._resolve_function_tools(["reboot"])) == 1


# -- bash_tool in the container ---------------------------------------------------


@pytest.fixture
def bash_tool_module():
    sys.path.insert(0, str(CODER_TOOLS))
    import bash_tool

    return bash_tool


def test_with_a_container_bash_runs_there_not_here(bash_tool_module, tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="uid=1000(agent)\n", stderr="")

    monkeypatch.setattr(bash_tool_module.subprocess, "run", run)
    factory = SimpleNamespace(container_id="c0ffee", config=SimpleNamespace(get_working_directory=lambda: str(tmp_path)))
    set_current_factory(factory)
    try:
        result = bash_tool_module._run_in_container("id", "c0ffee", "src", 30)
    finally:
        reset_current_factory()

    assert result[1] == "uid=1000(agent)\n"
    assert calls == [["docker", "exec", "-i", "-w", "/workspace/src", "c0ffee", "timeout", "-k", "5", "30", "sh", "-c", "id"]]


@pytest.mark.asyncio
async def test_the_tool_takes_the_container_of_the_current_run(bash_tool_module, tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")

    monkeypatch.setattr(bash_tool_module.subprocess, "run", run)
    factory = SimpleNamespace(container_id="c0ffee", config=SimpleNamespace(get_working_directory=lambda: str(tmp_path)))
    set_current_factory(factory)
    try:
        result = await bash_tool_module.bash_tool.on_invoke_tool(
            SimpleNamespace(context=None, tool_name="bash_tool", tool_call_id="t"), '{"command": "ls"}'
        )
    finally:
        reset_current_factory()

    assert "done" in result
    assert calls and calls[0][:2] == ["docker", "exec"]


# -- ripgrep --------------------------------------------------------------------


def test_a_search_pattern_is_never_a_ripgrep_option(tmp_path, monkeypatch):
    sys.path.insert(0, str(CODER_TOOLS))
    import search_tools

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setattr(search_tools.subprocess, "run", run)

    search_tools._grep_with_ripgrep("--pre=sh", tmp_path, "", True, True, 10)

    [command] = calls
    assert "--no-config" in command
    assert command[command.index("--regexp") + 1] == "--pre=sh"
    assert command[-2:] == ["--", str(tmp_path)]
