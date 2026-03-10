from pathlib import Path

import pytest

from utils.path_utils import display_agent_path, resolve_agent_path, sanitize_text_for_agent


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
