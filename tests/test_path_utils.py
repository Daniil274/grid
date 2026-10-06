from pathlib import Path

import pytest

from utils.path_utils import container_path, display_agent_path, resolve_agent_path, sanitize_text_for_agent


class _DummyConfig:
    def __init__(self, working_directory: str):
        self._working_directory = working_directory

    def get_working_directory(self) -> str:
        return self._working_directory


class _DummyFactory:
    def __init__(self, working_directory: str, container_id: str | None = None):
        self.config = _DummyConfig(working_directory)
        self.container_id = container_id


def test_resolve_agent_path_maps_host_absolute_inside_working_dir(tmp_path: Path):
    factory = _DummyFactory(str(tmp_path), container_id="container-1")
    target = tmp_path / "docs" / "guide.md"

    resolved = resolve_agent_path(str(target), factory)

    assert resolved == str(target.resolve())


def test_resolve_agent_path_maps_agent_root_absolute_to_working_dir(tmp_path: Path):
    factory = _DummyFactory(str(tmp_path), container_id="container-1")

    resolved = resolve_agent_path("/docs/guide.md", factory)

    assert resolved == str((tmp_path / "docs" / "guide.md").resolve())


def test_container_shell_paths_resolve_to_the_workspace(tmp_path: Path):
    # The shell of a run with a container works in /workspace: `pwd` shows it,
    # and the agent passes such paths on to the file tools.
    factory = _DummyFactory(str(tmp_path), container_id="container-1")

    assert resolve_agent_path("/workspace", factory) == str(tmp_path.resolve())
    assert resolve_agent_path("/workspace/", factory) == str(tmp_path.resolve())
    assert resolve_agent_path("/workspace/grid/README.md", factory) == str(
        (tmp_path / "grid" / "README.md").resolve()
    )
    assert display_agent_path("/workspace", factory) == "."
    assert display_agent_path("/workspace/grid", factory) == "grid"


def test_container_path_maps_resolved_host_paths_into_the_workspace(tmp_path: Path):
    # A configured working directory that differs from its resolved form only in
    # spelling (drive-letter case on Windows, "..") still maps to /workspace,
    # never to the host path, which `docker exec -w` rejects.
    spelled = str(tmp_path / "sub" / "..")
    if len(spelled) > 1 and spelled[1] == ":":
        spelled = spelled[0].swapcase() + spelled[1:]
    factory = _DummyFactory(spelled, container_id="container-1")

    assert container_path(resolve_agent_path("/workspace", factory), factory) == "/workspace"
    assert container_path(None, factory) == "/workspace"
    assert container_path(resolve_agent_path("src/a", factory), factory) == "/workspace/src/a"
    with pytest.raises(ValueError, match="escapes working directory"):
        container_path(str(tmp_path.parent), factory)


def test_container_workdir_prefix_is_matched_by_whole_name(tmp_path: Path):
    factory = _DummyFactory(str(tmp_path), container_id="container-1")

    assert resolve_agent_path("/workspace-evil/x", factory) == str(
        (tmp_path / "workspace-evil" / "x").resolve()
    )
    with pytest.raises(ValueError, match="escapes working directory"):
        resolve_agent_path("/workspace/../../etc/passwd", factory)


def test_without_a_container_workspace_is_an_ordinary_name(tmp_path: Path):
    factory = _DummyFactory(str(tmp_path))

    assert resolve_agent_path("/workspace/a", factory) == str((tmp_path / "workspace" / "a").resolve())


def test_resolve_agent_path_blocks_escape_above_working_dir(tmp_path: Path):
    factory = _DummyFactory(str(tmp_path))

    with pytest.raises(ValueError, match="escapes working directory"):
        resolve_agent_path("../secret.txt", factory)


def test_display_agent_path_hides_working_directory_prefix(tmp_path: Path):
    factory = _DummyFactory(str(tmp_path))
    target = tmp_path / "nested" / "file.txt"

    visible = display_agent_path(str(target), factory)

    assert visible == "nested/file.txt"


def test_sanitize_text_for_agent_scrubs_absolute_paths(tmp_path: Path):
    factory = _DummyFactory(str(tmp_path))
    text = f"fatal: repository at {tmp_path}/.git not found"

    sanitized = sanitize_text_for_agent(text, factory)

    assert str(tmp_path) not in sanitized
    assert "./.git" in sanitized


def test_without_an_agent_paths_stay_inside_the_process_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_agent_path("notes.txt", None) == str(tmp_path.resolve() / "notes.txt")
    with pytest.raises(ValueError):
        resolve_agent_path("../outside.txt", None)
