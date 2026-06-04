"""Tests for standalone timeline CLI entry point."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from timeline.__main__ import _resolve_config_path, main
from timeline.server import create_app


def test_timeline_main_module_imports():
    assert callable(main)


def test_resolve_config_path_prefers_project_root():
    resolved = _resolve_config_path("pyproject.toml")
    assert resolved.name == "pyproject.toml"
    assert resolved.exists()


def test_timeline_cli_help_exits_zero():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "timeline", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "Agent Timeline Dashboard" in proc.stdout


def test_create_app_without_factory():
    client = TestClient(create_app())
    response = client.get("/api/traces")
    assert response.status_code == 200
    assert "traces" in response.json()
