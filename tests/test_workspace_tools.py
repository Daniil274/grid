"""workspace_overview states what a directory holds, so an empty code index is not read as an empty folder."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

CODER_TOOLS = Path(__file__).resolve().parents[1] / "examples" / "coder" / "tools"
sys.path.insert(0, str(CODER_TOOLS))

import workspace_tools  # noqa: E402


class _Config:
    def __init__(self, working_directory: Path):
        self._working_directory = str(working_directory)

    def get_working_directory(self) -> str:
        return self._working_directory


def _context(working_directory: Path, extra=None):
    factory = SimpleNamespace(config=_Config(working_directory), container_id=None, extra_roots=extra or {})
    return SimpleNamespace(
        context=SimpleNamespace(factory=factory, container_id=None),
        tool_name="workspace_overview",
        tool_call_id="t",
    )


def _invoke(context, args="{}") -> str:
    return asyncio.run(workspace_tools.workspace_overview.on_invoke_tool(context, args))


def test_a_folder_of_service_entries_only_has_no_user_files(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("x")

    result = _invoke(_context(tmp_path))

    assert "2 entries, 0 of them not service entries - no user files" in result
    assert ".git/" in result and "[service]" in result


def test_user_entries_are_listed_with_directories_first(tmp_path):
    (tmp_path / "b.txt").write_text("abc")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("")

    result = _invoke(_context(tmp_path))

    assert "no user files" not in result
    assert result.index("docs/  (1 items)") < result.index("b.txt  (3 B)")


def test_a_long_listing_is_capped_and_counted(tmp_path):
    for i in range(workspace_tools.MAX_ENTRIES + 5):
        (tmp_path / f"f{i:03}.txt").write_text("")

    assert "... and 5 more" in _invoke(_context(tmp_path))


def test_other_spaces_of_the_run_are_listed_too(tmp_path):
    workspace = tmp_path / "chat"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    (user / "grid").mkdir()

    result = _invoke(_context(workspace, {"user": str(user)}))

    assert "/user: 1 entries" in result and "grid/" in result


def test_a_path_outside_the_workspace_is_refused(tmp_path):
    assert _invoke(_context(tmp_path), '{"directory": "../.."}').startswith("❌")


def test_a_file_is_not_a_directory(tmp_path):
    (tmp_path / "a.txt").write_text("")

    assert _invoke(_context(tmp_path), '{"directory": "a.txt"}').startswith("❌ Not a directory")
