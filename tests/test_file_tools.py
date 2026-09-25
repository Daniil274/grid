"""Contract tests for the agent file tools in tools/file_tools.py.

Tools are invoked the way the SDK invokes them - JSON arguments through
``on_invoke_tool`` - inside a factory path context bound to a temporary
working directory, so the workspace sandbox is part of what is tested.
"""

import json
from types import SimpleNamespace

import pytest

from tools.file_tools import FILE_TOOLS, get_file_tools
from utils.path_utils import factory_path_context


@pytest.fixture
def workspace(tmp_path):
    factory = SimpleNamespace(
        config=SimpleNamespace(get_working_directory=lambda: str(tmp_path)),
        container_id=None,
    )
    with factory_path_context(factory):
        yield tmp_path


async def run(tool_name, **arguments):
    tool = FILE_TOOLS[tool_name]
    return await tool.on_invoke_tool(None, json.dumps(arguments))


async def test_write_then_read_round_trips_unicode(workspace):
    assert "Created" in await run("file_write", filepath="notes/a.txt", content="привет\n")
    assert (workspace / "notes" / "a.txt").read_text(encoding="utf-8") == "привет\n"
    assert "привет" in await run("file_read", filepath="notes/a.txt")


async def test_rewriting_a_file_keeps_its_line_endings(workspace):
    (workspace / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    assert "Replaced" in await run("file_write", filepath="crlf.txt", content="one\nthree\n")
    assert (workspace / "crlf.txt").read_bytes() == b"one\r\nthree\r\n"


async def test_append_adds_to_the_end(workspace):
    (workspace / "log.txt").write_text("a", encoding="utf-8")
    await run("file_append", filepath="log.txt", content="b")
    assert (workspace / "log.txt").read_text(encoding="utf-8") == "ab"


async def test_missing_file_and_directory_are_reported(workspace):
    (workspace / "dir").mkdir()
    assert "not found" in await run("file_read", filepath="missing.txt")
    assert "is not a file" in await run("file_read", filepath="dir")
    (workspace / "file.txt").write_text("", encoding="utf-8")
    assert "not found" in await run("file_list", directory="missing")
    assert "is not a directory" in await run("file_list", directory="file.txt")


async def test_list_shows_directories_before_files(workspace):
    (workspace / "b.txt").write_text("", encoding="utf-8")
    (workspace / "a_dir").mkdir()
    listing = (await run("file_list", directory=".")).splitlines()
    assert listing[1:] == ["📁 a_dir", "📄 b.txt"]


async def test_search_matches_names_and_skips_caches(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("", encoding="utf-8")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "main.cpython.pyc").write_text("", encoding="utf-8")

    result = await run("file_search", directory=".", pattern=r"main\.")

    assert "src/main.py" in result
    assert "__pycache__" not in result


async def test_invalid_search_regex_is_an_error_not_a_crash(workspace):
    assert "❌" in await run("file_search", directory=".", pattern="(")


async def test_paths_cannot_escape_the_workspace(workspace):
    outside = workspace.parent / "outside.txt"
    result = await run("file_write", filepath="../outside.txt", content="x")
    assert "❌" in result
    assert not outside.exists()


async def test_git_internals_are_refused(workspace):
    result = await run("file_write", filepath=".git/config", content="x")
    assert "git internals cannot be changed" in result
    assert not (workspace / ".git" / "config").exists()


def test_registry_exposes_every_tool_under_its_name():
    tools = get_file_tools()
    assert len(tools) == len(FILE_TOOLS)
    assert all(hasattr(tool, "on_invoke_tool") for tool in tools)
