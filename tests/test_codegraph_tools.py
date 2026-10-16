"""init_codegraph builds or updates the workspace's CodeGraph index where the run's commands run."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from tools import codegraph_tools


class _Config:
    def __init__(self, working_directory: Path):
        self._working_directory = str(working_directory)

    def get_working_directory(self) -> str:
        return self._working_directory


def _context(working_directory: Path, container_id=None):
    factory = SimpleNamespace(config=_Config(working_directory), container_id=container_id)
    return SimpleNamespace(
        context=SimpleNamespace(factory=factory, container_id=container_id),
        tool_name="init_codegraph",
        tool_call_id="t",
    )


def _invoke(context) -> str:
    return asyncio.run(codegraph_tools.init_codegraph.on_invoke_tool(context, "{}"))


def _fake_run(monkeypatch, returncode=0, stdout=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(codegraph_tools.subprocess, "run", run)
    return calls


def test_a_project_without_an_index_is_initialised_and_one_with_it_synced(tmp_path):
    assert codegraph_tools._command(tmp_path)[:2] == ["codegraph", "init"]
    (tmp_path / ".codegraph").mkdir()
    assert codegraph_tools._command(tmp_path)[:2] == ["codegraph", "sync"]


def test_in_a_container_it_runs_there_in_the_workspace_and_is_ended_there(tmp_path, monkeypatch):
    calls = _fake_run(monkeypatch, stdout="◆  Indexed 3 files\n")

    result = _invoke(_context(tmp_path, container_id="c0ffee"))

    [(cmd, kwargs)] = calls
    assert cmd[:4] == ["docker", "exec", "-w", "/workspace"]
    assert "CODEGRAPH_TELEMETRY=0" in cmd
    assert cmd[cmd.index("c0ffee") + 1:][:4] == ["timeout", "-k", "5", str(codegraph_tools.CODEGRAPH_TIMEOUT_SECONDS)]
    assert kwargs["stdin"] is codegraph_tools.subprocess.DEVNULL
    assert result.startswith("✅") and "Indexed 3 files" in result


def test_a_failure_says_codegraph_is_not_available(tmp_path, monkeypatch):
    _fake_run(monkeypatch, returncode=1, stdout="error: something")

    assert _invoke(_context(tmp_path)).startswith("❌ CodeGraph is not available (exit 1)")


def test_a_timeout_in_the_container_is_named_as_such(tmp_path, monkeypatch):
    _fake_run(monkeypatch, returncode=124)

    assert "did not finish within" in _invoke(_context(tmp_path, container_id="c0ffee"))


def test_console_output_is_plain_text():
    output = (
        "\x1b[2m|\x1b[0m\n"
        "\x1b[K\x1b[2m|\x1b[0m  \x1b[32m*\x1b[0m Scanning files - 1 found\n"
        "\x1b[K\x1b[2m|\x1b[0m  . Parsing code  #########################  100%\n"
        "│\n◆  Indexed 1 files\n"
    )

    assert codegraph_tools._summary(output) == "Scanning files - 1 found\nIndexed 1 files"
