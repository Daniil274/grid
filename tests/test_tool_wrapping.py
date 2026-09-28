"""Each factory wraps its own copy of a shared tool, never the shared object."""

from pathlib import Path

from core.agent_factory import AgentFactory
from core.config.config import Config
from core.context import ContextManager
from tools.function_tools import resolve_tool

CODER = Path(__file__).resolve().parent.parent / "examples" / "coder" / "config.yaml"


def _factory(root: Path) -> AgentFactory:
    root.mkdir()
    return AgentFactory(
        config=Config(str(CODER), str(root)),
        working_directory=str(root),
        context_manager=ContextManager(persist_path=str(root / "c.json")),
        session_db_path=str(root / "s.db"),
        logs_directory=str(root / "logs"),
    )


def test_two_factories_do_not_wrap_each_others_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    first, second = _factory(tmp_path / "alice"), _factory(tmp_path / "bob")
    shared = resolve_tool("file_read", first.config.project_tools_loader)
    original = shared.on_invoke_tool

    [alices] = first._resolve_function_tools(["file_read"])
    [bobs] = second._resolve_function_tools(["file_read"])

    # One loader serves both configs, so both resolved the same shared object...
    assert resolve_tool("file_read", second.config.project_tools_loader) is shared
    # ...which stays as it was: each factory wrapped a copy of its own.
    assert shared.on_invoke_tool is original
    assert alices is not shared and bobs is not shared and alices is not bobs
    assert alices.on_invoke_tool is not bobs.on_invoke_tool
