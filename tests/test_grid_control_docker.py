"""Opt-in real Docker test: GRID_CONTROL_DOCKER_TEST=1 pytest this file."""

import os
import subprocess

import pytest

from grid_control.controller import Controller
from grid_control.docker import DockerRuntime
from grid_control.models import Check, Policy
from grid_control.store import Store


@pytest.mark.skipif(
    os.environ.get("GRID_CONTROL_DOCKER_TEST") != "1",
    reason="requires local Docker and python:3.11-slim",
)
def test_real_commits_build_run_compare_and_cleanup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init")
    git("config", "user.name", "Grid Controller Test")
    git("config", "user.email", "test@example.invalid")
    app = repo / "web_chat"
    app.mkdir()
    (app / "__init__.py").write_text("")
    source = """from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"routing_enabled": VALUE}')
HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
"""
    (app / "__main__.py").write_text(source.replace("VALUE", "false"))
    git("add", ".")
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")
    (app / "__main__.py").write_text(source.replace("VALUE", "true"))
    git("add", ".")
    git("commit", "-m", "candidate")
    candidate = git("rev-parse", "HEAD")
    runtime = DockerRuntime()
    policy = Policy(
        os.environ.get("GRID_CONTROL_TEST_IMAGE", "python:3.11-slim"),
        os.environ.get("GRID_CONTROL_TEST_IMAGE", "python:3.11-slim"),
        (
            Check(
                "routing",
                "/api/chat/bootstrap",
                json_pointer="/routing_enabled",
                expected=True,
            ),
        ),
        repetitions=1,
        timeout_seconds=30,
        min_improvement=1.0,
    )
    report = Controller(Store(tmp_path / "control.db"), runtime).evaluate(
        repo, baseline, candidate, policy
    )
    try:
        assert report["status"] == "accepted", report
        label = "label=grid.experiment=" + report["id"]
        assert not runtime.command(["docker", "ps", "-aq", "--filter", label]).strip()
        assert not runtime.command(
            ["docker", "network", "ls", "-q", "--filter", label]
        ).strip()
    finally:
        runtime.cleanup(report["id"])
        images = (
            runtime.command(
                [
                    "docker",
                    "image",
                    "ls",
                    "-q",
                    "--filter",
                    "label=grid.experiment=" + report["id"],
                ]
            )
            .decode()
            .split()
        )
        for image in set(images):
            runtime.command(["docker", "image", "rm", "--force", image])
