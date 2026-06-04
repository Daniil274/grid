from pathlib import Path

from utils.grid_paths import get_default_logs_dir, get_grid_home, resolve_logs_directory


def test_default_logs_dir_under_grid_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert get_grid_home() == tmp_path / ".grid"
    assert get_default_logs_dir() == tmp_path / ".grid" / "logs"


def test_resolve_logs_directory_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert resolve_logs_directory(None) == (tmp_path / ".grid" / "logs").resolve()


def test_resolve_logs_directory_absolute():
    path = resolve_logs_directory("/var/log/grid")
    assert path == Path("/var/log/grid").resolve()


def test_resolve_logs_directory_relative_to_workdir(tmp_path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    path = resolve_logs_directory("logs", working_directory=str(workdir))
    assert path == (workdir / "logs").resolve()


def test_resolve_logs_directory_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    custom = tmp_path / "my-logs"
    path = resolve_logs_directory("~/my-logs")
    assert path == custom.resolve()


def test_resolve_logs_directory_blank_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert resolve_logs_directory("") == (tmp_path / ".grid" / "logs").resolve()
    assert resolve_logs_directory("   ") == (tmp_path / ".grid" / "logs").resolve()


def test_config_get_logs_directory_honors_override(tmp_path):
    config_yaml = tmp_path / "config.yaml"
    custom_logs = tmp_path / "custom-logs"
    config_yaml.write_text(
        f"""
settings:
  default_agent: test
  working_directory: .
  logs_directory: "{custom_logs.as_posix()}"
agents:
  test:
    name: Test
    model: m1
    tools: []
models:
  m1:
    name: m1
    provider: p1
providers:
  p1:
    name: p1
    base_url: https://example.com/v1
""",
        encoding="utf-8",
    )

    from core.config import Config

    config = Config(str(config_yaml), working_directory=str(tmp_path))
    assert Path(config.get_logs_directory()).resolve() == custom_logs.resolve()

