"""Tool packages: read without running, served over MCP by the runtime, tested in a sandbox."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import Config
from core.tool_check import diagnose
from core.tool_packages import PackageError, RUNTIME, analyze, digest, package_dir, sandbox_test

TOOLS = '''
from typing import Literal, Optional
from grid_tool import tool


@tool(read_only=True)
def count(path: str, unique: bool = False, mode: Literal["words", "lines"] = "words", limit: Optional[int] = None) -> dict:
    """Count the words or lines of a workspace file.

    Args:
        path: the file, relative to the workspace
        unique: count distinct items
            instead of all of them
    """
    print("printed by the tool")
    text = open(path, encoding="utf-8").read()
    items = text.split() if mode == "words" else text.splitlines()
    return {"count": len(set(items)) if unique else len(items)}


@tool(name="shout", destructive=True)
async def loud(text: str) -> str:
    """Upper-case a text."""
    return text.upper()
'''

TESTS = '''
import os, tempfile
from counting import count


def test_counts():
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "a.txt")
    open(path, "w").write("a b a")
    assert count(path) == {"count": 3}


def test_fails():
    assert 1 == 2
'''


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "system" / "tools" / "counting"
    root.mkdir(parents=True)
    (root / "counting.py").write_text(TOOLS, encoding="utf-8")
    (root / "test_counting.py").write_text(TESTS, encoding="utf-8")
    return root


# -- reading ------------------------------------------------------------------------


def test_a_package_is_read_without_running_it(package):
    (package / "counting.py").write_text(TOOLS + "\nraise SystemExit('imported!')\n", encoding="utf-8")
    info = analyze(package)
    assert info.issues == []
    assert [tool.name for tool in info.tools] == ["count", "shout"]
    count, shout = info.tools
    assert count.read_only and not count.destructive and shout.destructive
    assert [(p["name"], p["annotation"], p["required"]) for p in count.params][:2] == [
        ("path", "str", True),
        ("unique", "bool", False),
    ]
    assert count.description.startswith("Count the words")
    assert info.tests == 2


@pytest.mark.parametrize(
    "source, problem",
    [
        ("from grid_tool import tool\n@tool\ndef f(x: str): return x\n", "no docstring"),
        ("from grid_tool import tool\n@tool\ndef f(*args):\n    '''Doc.'''\n", "*args"),
        ("from grid_tool import tool\n@tool(name='9bad')\ndef f():\n    '''Doc.'''\n", "a name is"),
        ("def f(): pass\n", "no @tool functions"),
        ("def f(:\n", "invalid syntax"),
    ],
)
def test_what_an_agent_could_not_use_is_reported(tmp_path, source, problem):
    (tmp_path / "mod.py").write_text(source, encoding="utf-8")
    assert any(problem in issue for issue in analyze(tmp_path).issues)


def test_requirements_are_pinned_from_the_index(package):
    (package / "requirements.txt").write_text(
        "tabulate==0.9.0\n# a comment\nrequests\n-e git+https://x\nhttps://x/y.whl\n", encoding="utf-8"
    )
    info = analyze(package)
    assert info.requirements == ["tabulate==0.9.0"]
    assert sum("pin one version" in issue for issue in info.issues) == 3


def test_a_package_stays_inside_its_system(tmp_path, package):
    system = tmp_path / "system"
    assert package_dir(system, "tools/counting") == package.resolve()
    with pytest.raises(PackageError, match="leaves"):
        package_dir(system, "../../etc")


def test_the_deployment_name_follows_the_content(package):
    before = digest(package)
    assert digest(package) == before
    (package / "counting.py").write_text(TOOLS + "\n# changed\n", encoding="utf-8")
    assert digest(package) != before


def test_the_health_check_reports_a_packages_problems(tmp_path):
    (tmp_path / "tools" / "bad").mkdir(parents=True)
    (tmp_path / "tools" / "bad" / "mod.py").write_text("def f(): pass\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        """
settings: {default_agent: a, mcp_enabled: true}
tools:
  bad: {type: mcp, tool_package: tools/bad, description: Bad}
  away: {type: mcp, tool_package: ../outside, description: Away}
agents:
  a: {name: A, model: m, tools: [bad, away], description: Does things}
models:
  m: {name: model, provider: p}
providers:
  p: {name: p, base_url: https://example.com/v1, api_key: k}
""",
        encoding="utf-8",
    )
    problems = [issue.text() for issue in diagnose(Config(str(tmp_path / "config.yaml")))]
    assert any("tool package: no @tool functions" in text for text in problems)
    assert any("leaves the system's directory" in text for text in problems)


# -- the runtime ----------------------------------------------------------------------


def run_runtime(mode, package, stdin=""):
    return subprocess.run(
        [sys.executable, str(RUNTIME), mode, str(package)],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=package,
        timeout=60,
    )


def test_the_runtime_describes_and_tests_a_package(package):
    described = json.loads(run_runtime("describe", package).stdout)
    assert described["errors"] == []
    by_name = {tool["name"]: tool for tool in described["tools"]}
    schema = by_name["count"]["inputSchema"]
    assert schema["required"] == ["path"]
    assert schema["properties"]["mode"] == {"enum": ["words", "lines"], "default": "words"}
    assert schema["properties"]["unique"]["description"] == "count distinct items instead of all of them"
    assert by_name["shout"]["annotations"]["destructiveHint"] is True

    tested = json.loads(run_runtime("test", package).stdout)
    assert (tested["passed"], tested["failed"]) == (1, 1)
    assert "AssertionError" in [t for t in tested["tests"] if not t["ok"]][0]["error"]


def test_the_runtime_speaks_mcp_and_keeps_prints_off_the_protocol(package):
    (package / "a.txt").write_text("x y x", encoding="utf-8")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "count", "arguments": {"path": "a.txt", "unique": "true"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "count", "arguments": {"path": "none.txt"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "shout", "arguments": {"text": "hi"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "count", "arguments": {}}},
    ]
    result = run_runtime("serve", package, "\n".join(json.dumps(m) for m in messages) + "\n")
    answers = {answer["id"]: answer for answer in map(json.loads, result.stdout.splitlines())}
    assert answers[1]["result"]["protocolVersion"] == "2025-06-18"
    assert {tool["name"] for tool in answers[2]["result"]["tools"]} == {"count", "shout"}
    assert json.loads(answers[3]["result"]["content"][0]["text"]) == {"count": 2}
    assert answers[4]["result"]["isError"] and "FileNotFoundError" in answers[4]["result"]["content"][0]["text"]
    assert answers[5]["result"]["content"][0]["text"] == "HI"
    assert answers[6]["result"]["isError"] and "missing arguments: path" in answers[6]["result"]["content"][0]["text"]
    assert "printed by the tool" in result.stderr


# -- the MCP factory ------------------------------------------------------------------


def test_the_factory_serves_a_package_from_its_deployment(package, monkeypatch):
    from core.factory import mcp as mcp_module
    from core.tool_packages import Deployment

    deployed = Deployment(root="/opt/grid-tools/abc", python="python3")
    seen = {}

    async def in_container(container, path):
        seen["args"] = (container, path)
        return deployed

    monkeypatch.setattr("core.tool_packages.ensure_in_container", in_container)
    config = SimpleNamespace(config_path=str(package.parent.parent / "config.yaml"))
    servers = mcp_module.McpServers(config, None, "cid", "/workspace")
    command, env = asyncio.run(servers._package_command("counting", "tools/counting"))
    assert seen["args"] == ("cid", package.resolve())
    assert command == ["python3", "/opt/grid-tools/abc/grid_tool.py", "serve", "/opt/grid-tools/abc/pkg"]
    assert env["PYTHONPATH"] == "/opt/grid-tools/abc:/opt/grid-tools/abc/deps:/opt/grid-tools/abc/pkg"

    assert asyncio.run(servers._package_command("away", "../../elsewhere")) == ([], {})


# -- the sandbox ----------------------------------------------------------------------


def _docker_image(image="grid-agent:latest") -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_image(), reason="needs Docker and the grid-agent image")
def test_a_package_is_tested_in_a_sandbox_without_network(package):
    (package / "test_counting.py").write_text(
        TESTS.replace("def test_fails():\n    assert 1 == 2\n", "")
        + '''

def test_offline():
    import urllib.request
    try:
        urllib.request.urlopen("https://example.com", timeout=3)
    except Exception:
        return
    raise AssertionError("the network is reachable")
''',
        encoding="utf-8",
    )
    result = asyncio.run(sandbox_test("grid-agent:latest", package))
    assert result["ok"], result
    assert result["offline"] is True
    assert {tool["name"] for tool in result["describe"]["tools"]} == {"count", "shout"}
    assert result["tests"]["passed"] == 2
    leftover = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=grid-tooltest", "--format", "{{.Names}}"], capture_output=True, text=True
    )
    assert leftover.stdout.strip() == ""


def test_a_failed_network_disconnect_never_runs_package_code(package, monkeypatch):
    import core.tool_packages as tool_packages

    calls = []

    async def fake_run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["docker", "network", "disconnect"]:
            return 1, "", "network failure"
        return 0, "", ""

    async def fake_deploy(*_args, **_kwargs):
        return tool_packages.Deployment(root="/opt/grid-tools/test", python="python3")

    monkeypatch.setattr(tool_packages, "_run", fake_run)
    monkeypatch.setattr(tool_packages, "ensure_in_container", fake_deploy)
    result = asyncio.run(sandbox_test("grid-agent:latest", package))

    assert result["stage"] == "network" and not result["ok"]
    assert not any(call[:2] == ["docker", "exec"] for call in calls)
    assert calls[-1][:3] == ["docker", "rm", "-f"]
