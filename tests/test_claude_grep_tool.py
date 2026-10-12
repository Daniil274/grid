import importlib.util
import json
from pathlib import Path

import pytest


def _load_search_tools_module():
    module_path = Path(__file__).resolve().parents[1] / "examples" / "coder" / "tools" / "search_tools.py"
    spec = importlib.util.spec_from_file_location("claude_search_tools_for_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_grep_tool_schema_keeps_default_arguments_optional():
    module = _load_search_tools_module()

    assert module.grep_tool.strict_json_schema is False
    assert module.grep_tool.params_json_schema["required"] == ["pattern"]


@pytest.mark.asyncio
async def test_grep_tool_invocation_accepts_minimal_arguments(agent_workspace):
    tmp_path = agent_workspace
    module = _load_search_tools_module()
    sample = tmp_path / "sample.py"
    sample.write_text("def useful_function():\n    return 42\n", encoding="utf-8")

    result = await module.grep_tool.on_invoke_tool(
        None,
        json.dumps({"pattern": "useful_function", "directory": str(tmp_path)}),
    )

    assert "sample.py:1:" in result
    assert "useful_function" in result


@pytest.mark.asyncio
async def test_grep_tool_rejects_empty_pattern(tmp_path):
    module = _load_search_tools_module()

    result = await module.grep_tool.on_invoke_tool(
        None,
        json.dumps({"pattern": "", "directory": str(tmp_path)}),
    )

    assert "Pattern must not be empty" in result


@pytest.mark.asyncio
async def test_glob_tool_finds_directories_recursively(agent_workspace):
    tmp_path = agent_workspace
    module = _load_search_tools_module()
    (tmp_path / "ocr_output_TLV_123").mkdir()
    (tmp_path / "ocr_output_TLV_123" / "file.txt").write_text("x", encoding="utf-8")

    result = await module.glob_tool.on_invoke_tool(
        None,
        json.dumps({"pattern": "**/ocr_output_TLV*", "directory": str(tmp_path)}),
    )

    assert "ocr_output_TLV_123/" in result


@pytest.mark.asyncio
async def test_ripgrep_timeout_does_not_start_another_search(agent_workspace, monkeypatch):
    module = _load_search_tools_module()

    def timed_out(*args, **kwargs):
        raise module.subprocess.TimeoutExpired("rg", module.GREP_TIMEOUT)

    def unexpected_fallback(*args, **kwargs):
        pytest.fail("A timed-out search must not consume a second timeout budget")

    monkeypatch.setattr(module, "_grep_with_ripgrep", timed_out)
    monkeypatch.setattr(module, "_grep_with_python", unexpected_fallback)
    result = await module.grep_tool.on_invoke_tool(
        None, json.dumps({"pattern": "needle", "directory": str(agent_workspace)})
    )

    assert "Search exceeded" in result
