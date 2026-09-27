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


def test_build_context_does_not_inherit_git_archive_headers(tmp_path):
    """git archive's global pax header (commit ID) must not reach BuildKit."""
    import io
    import tarfile

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@local"]
    subprocess.run([*git, "init", "--quiet"], check=True)
    (repo / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (repo / "notes.md").write_text("x\n", encoding="utf-8")
    subprocess.run([*git, "add", "--all"], check=True)
    subprocess.run([*git, "update-index", "--chmod=+x", "run.sh"], check=True)
    subprocess.run([*git, "commit", "--quiet", "-m", "c"], check=True)
    commit = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    contexts = []

    class Recording(DockerRuntime):
        def command(self, args, *, timeout=30, data=None):
            if args[0] == "git":
                return super().command(args, timeout=timeout, data=data)
            if args[:2] == ["docker", "build"]:
                contexts.append(data)
                return b""
            return b"sha256:" + b"0" * 64

    Recording().build(repo, commit, "sha256:" + "a" * 64, "0" * 32, 60)

    with tarfile.open(fileobj=io.BytesIO(contexts[0])) as context:
        members = {member.name: member for member in context}
        recipe = context.extractfile("Dockerfile").read().decode()
    # BuildKit cannot use a bare image ID in FROM; the pinned tag carries it whole.
    assert recipe.splitlines()[0] == "FROM grid-control-runtime:" + "a" * 64
    assert set(members) == {"source/run.sh", "source/notes.md", "Dockerfile"}
    assert all(not member.pax_headers for member in members.values())
    assert members["source/run.sh"].mode == 0o755
    assert members["source/notes.md"].mode == 0o644
