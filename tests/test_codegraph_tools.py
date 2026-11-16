"""init_codegraph builds or updates the workspace's CodeGraph index where the run's commands run."""

import asyncio
import json
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


def _status(files=3, nodes=10, edges=20) -> str:
    return json.dumps({"fileCount": files, "nodeCount": nodes, "edgeCount": edges})


def _fake_run(monkeypatch, returncode=0, stdout="", status=None):
    """Fake `codegraph`: *stdout* for init/sync, *status* (JSON text) for `status --json`."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "status" in cmd:
            return SimpleNamespace(returncode=0, stdout=status if status is not None else _status(), stderr="")
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

    (cmd, kwargs), status_call = calls
    assert "status" in status_call[0] and "--json" in status_call[0]
    assert cmd[:4] == ["docker", "exec", "-w", "/workspace"]
    assert "CODEGRAPH_TELEMETRY=0" in cmd
    timeout_command = cmd[cmd.index("c0ffee") + 1:]
    assert timeout_command[:3] == ["timeout", "-k", "5"]
    assert 0 < float(timeout_command[3]) <= codegraph_tools.CODEGRAPH_TIMEOUT_SECONDS
    assert kwargs["stdin"] is codegraph_tools.subprocess.DEVNULL
    assert result.startswith("✅") and "3 files, 10 nodes, 20 edges" in result and "Indexed 3 files" in result


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


def test_an_index_of_no_files_is_not_reported_as_ready(tmp_path, monkeypatch):
    _fake_run(monkeypatch, stdout="No files found to index", status=_status(0, 0, 0))

    result = _invoke(_context(tmp_path))

    assert not result.startswith("✅") and "ready" not in result
    assert "is empty: 0 files indexed" in result and "grep_tool" in result


def test_an_index_that_cannot_be_read_back_is_not_reported_as_ready(tmp_path, monkeypatch):
    _fake_run(monkeypatch, status="not json")

    result = _invoke(_context(tmp_path))

    assert not result.startswith("✅") and "unknown whether" in result


def test_a_locked_index_is_retried_until_the_other_codegraph_lets_go(tmp_path, monkeypatch):
    results = [
        SimpleNamespace(returncode=1, stdout="", stderr="Failed to sync: database is locked"),
        SimpleNamespace(returncode=0, stdout="Indexed 3 files", stderr=""),
    ]

    def run(cmd, **kwargs):
        if "status" in cmd:
            return SimpleNamespace(returncode=0, stdout=_status(), stderr="")
        return results.pop(0)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(codegraph_tools.subprocess, "run", run)
    monkeypatch.setattr(codegraph_tools.asyncio, "sleep", no_sleep)

    result = _invoke(_context(tmp_path))

    assert result.startswith("\u2705") and not results


def test_operator_startup_has_a_short_budget_inside_the_container(tmp_path, monkeypatch):
    calls = _fake_run(monkeypatch, returncode=124)
    context = _context(tmp_path, container_id="c0ffee")
    context.operator_configured = True

    result = _invoke(context)

    cmd, kwargs = calls[0]
    container_timeout = float(cmd[cmd.index("timeout") + 3])
    assert 0 < container_timeout <= codegraph_tools.CODEGRAPH_STARTUP_TIMEOUT_SECONDS
    assert kwargs["timeout"] <= codegraph_tools.CODEGRAPH_STARTUP_TIMEOUT_SECONDS + 10
    assert "grep_tool" in result and "explicitly" in result
    assert len(calls) == 1  # No status call after a timed-out sync.


def test_sync_lock_retries_and_status_share_one_startup_budget(tmp_path, monkeypatch):
    clock = [0.0]
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        clock[0] += codegraph_tools.CODEGRAPH_STARTUP_TIMEOUT_SECONDS
        return SimpleNamespace(returncode=1, stdout="database is locked", stderr="")

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(codegraph_tools.subprocess, "run", run)
    monkeypatch.setattr(codegraph_tools.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(codegraph_tools.asyncio, "sleep", no_sleep)
    context = _context(tmp_path, container_id="c0ffee")
    context.operator_configured = True

    result = _invoke(context)

    assert "did not finish within" in result
    assert len(calls) == 1


def test_out_of_memory_has_an_actionable_fallback_without_a_gc_dump(tmp_path, monkeypatch):
    _fake_run(monkeypatch, returncode=1, stdout="GC DUMP\nFATAL ERROR: JavaScript heap out of memory")

    result = _invoke(_context(tmp_path))

    assert "codegraph.json" in result and "grep_tool" in result
    assert "GC DUMP" not in result
