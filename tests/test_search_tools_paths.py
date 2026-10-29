"""Where the search and shell tools point in a run's container.

A run's container mounts its workspace at ``/workspace`` and every other space
at its own path (``/user``, ``/project``). A tool that builds that path by
gluing the workspace mount onto a display path searched ``/workspace//user/grid``
- a directory that does not exist - instead of the ``/user/grid`` mount.
Every container path comes from ``utils.path_utils.container_path``, so a space
is addressed at the path it is mounted at, with no doubled separator and never
under the workspace mount.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils.path_utils import (
    CONTAINER_WORKDIR,
    container_path,
    reset_current_factory,
    resolve_agent_path,
    set_current_factory,
)

REPO = Path(__file__).resolve().parents[1]
CODER_TOOLS = REPO / "examples" / "coder" / "tools"

if str(CODER_TOOLS) not in sys.path:
    sys.path.insert(0, str(CODER_TOOLS))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class _Config:
    def __init__(self, working_directory: Path):
        self._working_directory = working_directory

    def get_working_directory(self) -> str:
        return str(self._working_directory)


def _factory(spaces, container_id="c0ffee"):
    """A run like a chat's: a workspace of its own plus the project's and user's spaces."""
    workspace, project, user = spaces
    return SimpleNamespace(
        config=_Config(workspace),
        container_id=container_id,
        # The container's mount points, as web_chat.space hands them to a factory.
        extra_roots={"/project": str(project), "/user": str(user)},
    )


@pytest.fixture
def spaces(tmp_path):
    workspace, project, user = tmp_path / "workspace", tmp_path / "project", tmp_path / "user"
    for directory in (workspace / "src", project / "src", user / "grid"):
        directory.mkdir(parents=True)
    (workspace / "a.py").write_text("needle\n", encoding="utf-8")
    (project / "src" / "b.py").write_text("needle\n", encoding="utf-8")
    (user / "grid" / "c.py").write_text("needle\n", encoding="utf-8")
    return workspace, project, user


def _run_grep(search_tools, spaces, directory):
    """Invoke grep_tool with a factory bound, recording the command it would run."""
    set_current_factory(_factory(spaces))
    try:
        return _invoke(search_tools.grep_tool, "grep_tool", {"pattern": "needle", "directory": directory})
    finally:
        reset_current_factory()


def _invoke(tool, name, arguments):
    import asyncio

    return asyncio.run(
        tool.on_invoke_tool(
            SimpleNamespace(context=None, tool_name=name, tool_call_id="t"),
            json.dumps(arguments),
        )
    )


def _ripgrep_calls(monkeypatch, search_tools):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setattr(search_tools, "run_host_command", run)
    return calls


@pytest.mark.parametrize(
    "directory, expected",
    [
        (".", CONTAINER_WORKDIR),
        ("src", f"{CONTAINER_WORKDIR}/src"),
        ("/workspace", CONTAINER_WORKDIR),
        ("/workspace/src", f"{CONTAINER_WORKDIR}/src"),
        # The other spaces are mounted at their own paths, not under /workspace.
        ("/user", "/user"),
        ("/user/grid", "/user/grid"),
        ("/project", "/project"),
        ("/project/src", "/project/src"),
    ],
)
def test_grep_addresses_a_directory_where_the_container_mounts_it(
    spaces, monkeypatch, directory, expected
):
    import search_tools

    calls = _ripgrep_calls(monkeypatch, search_tools)
    _run_grep(search_tools, spaces, directory)

    [command] = calls
    assert command[0] == "docker" and command[-1] == expected
    # The search itself runs from the workspace mount; only the target varies.
    assert command[command.index("-w") + 1] == CONTAINER_WORKDIR
    assert "//" not in command[-1]
    assert all("//" not in argument for argument in command)


def test_another_spaces_directory_is_never_put_under_the_workspace_mount(spaces, monkeypatch):
    """The reported failure: grep searched /workspace//user/grid and found nothing."""
    import search_tools

    calls = _ripgrep_calls(monkeypatch, search_tools)
    _run_grep(search_tools, spaces, "/user/grid")

    [command] = calls
    assert "/workspace//user/grid" not in command
    assert not any(str(argument).startswith(CONTAINER_WORKDIR + "/") for argument in command[7:])
    assert command[-1] == "/user/grid"


def test_grep_takes_its_target_from_the_one_path_mapper(spaces, monkeypatch):
    """Whatever directory the agent names, the container path is container_path()'s."""
    import search_tools

    factory = _factory(spaces)
    calls = _ripgrep_calls(monkeypatch, search_tools)
    for directory in (".", "src", "/workspace/src", "/user/grid", "/project/src", "/user"):
        calls.clear()
        _run_grep(search_tools, spaces, directory)
        resolved = resolve_agent_path(directory, factory)
        assert calls[0][-1] == container_path(resolved, factory)


def test_glob_lists_another_space_without_a_container_prefix(spaces):
    """glob_tool walks host paths: it names files relative to the directory it was given."""
    import search_tools

    set_current_factory(_factory(spaces))
    try:
        listing = _invoke(search_tools.glob_tool, "glob_tool", {"pattern": "*.py", "directory": "/user/grid"})
    finally:
        reset_current_factory()

    assert "c.py" in listing
    assert "needle" not in listing
    assert CONTAINER_WORKDIR not in listing
    assert "/user/grid" not in listing.replace("in '/user/grid'", "")


def test_bash_runs_in_the_container_path_of_its_directory(spaces, monkeypatch):
    """bash_tool's docker exec -w comes from container_path(), not from a display path."""
    import bash_tool

    seen = []

    def run_in_container(command, container_id, relative_dir, timeout):
        seen.append((command, container_id, relative_dir, timeout))
        return 0, "", ""

    monkeypatch.setattr(bash_tool, "_run_in_container", run_in_container)
    factory = _factory(spaces)
    set_current_factory(factory)
    try:
        for working_dir in (".", "src", "/user/grid", "/project/src"):
            seen.clear()
            _invoke(bash_tool.bash_tool, "bash_tool", {"command": "true", "working_dir": working_dir})
            [call] = seen
            expected = container_path(resolve_agent_path(working_dir, factory), factory)
            assert call[2] == expected
            assert "//" not in call[2]
    finally:
        reset_current_factory()


def test_the_tools_hold_no_mount_prefix_of_their_own():
    """One definition of where the workspace is mounted: utils.path_utils."""
    import bash_tool
    import search_tools

    assert search_tools.CONTAINER_WORKDIR == CONTAINER_WORKDIR
    assert bash_tool.CONTAINER_WORKDIR == CONTAINER_WORKDIR
    assert not hasattr(search_tools, "CONTAINER_ROOT")
    assert not hasattr(bash_tool, "CONTAINER_ROOT")

    from tools import beads_tools

    assert beads_tools.CONTAINER_WORKDIR == CONTAINER_WORKDIR
    assert not hasattr(beads_tools, "_CONTAINER_ROOT")
