"""The version and the commit of the code the server actually runs.

A restart the operator cannot verify is a restart they cannot trust: the admin
Server card shows the version and the short commit read once at startup, and
this covers both halves - reading them (git, an explicit variable, a build
file, or an honest "unknown") and carrying them through the API.
"""

import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from tests.test_web_spaces import FakeSpace
from web_chat import build_info
from web_chat.accounts import passwords
from web_chat.accounts.http import SessionAuth
from web_chat.accounts.service import Accounts
from web_chat.accounts.store import AccountStore
from web_chat.build_info import UNKNOWN, read_build_info
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool

PASSWORD = "correct horse battery"
SAME_SITE = {"Origin": "http://testserver"}
SHORT_COMMIT = re.compile(r"[0-9a-f]{7}")


class Deployment:
    """A server with accounts reads nothing of the deployment until a space is built."""


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "LOG2_N", 10)


def admin_client(tmp_path):
    store = AccountStore(tmp_path / "accounts.db")
    accounts = Accounts(store)
    accounts.create_user("admin", PASSWORD, role="admin")
    server = WebChatServer(Deployment(), SpacePool(FakeSpace), auth=SessionAuth(accounts), warm_user=None,
                           trusted_hosts=("testserver",))
    client = TestClient(server.app, headers=SAME_SITE)
    response = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return server, client, store


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()


# -- reading the build ------------------------------------------------------------


def test_the_package_version_is_reported():
    from core import __version__

    info = read_build_info(env={}, commit_file="/nonexistent/build_commit.txt")

    assert info["version"] == __version__


def test_git_is_the_source_of_a_checkout(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "--quiet", "-m", "x")
    head = git(tmp_path, "rev-parse", "HEAD")

    info = read_build_info(tmp_path, env={}, commit_file=tmp_path / "none")

    assert info == {"version": info["version"], "commit": head[:7], "commit_full": head, "source": "git"}
    assert SHORT_COMMIT.fullmatch(info["commit"])


def test_an_explicit_commit_wins_over_the_checkout(tmp_path):
    # A deployment that stamps its image knows better than the checkout under it.
    info = read_build_info(tmp_path, env={"GRID_BUILD_COMMIT": "ABCDEF0123456789"}, commit_file=tmp_path / "none")

    assert info["commit"] == "abcdef0"
    assert info["commit_full"] == "abcdef0123456789"
    assert info["source"] == "env"


def test_a_build_file_stands_in_for_git(tmp_path):
    stamp = tmp_path / "build_commit.txt"
    stamp.write_text("0123456789abcdef\n", encoding="utf-8")

    info = read_build_info(tmp_path, env={}, commit_file=stamp)

    assert (info["commit"], info["commit_full"], info["source"]) == ("0123456", "0123456789abcdef", "file")


def test_an_unknown_build_is_said_honestly(tmp_path):
    info = read_build_info(tmp_path, env={}, commit_file=tmp_path / "none")

    assert info["commit"] == UNKNOWN and info["commit_full"] == UNKNOWN
    assert info["source"] == "unknown"
    assert info["version"] != UNKNOWN


def test_a_repository_without_commits_is_not_read_as_a_commit(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    git(tmp_path, "init", "--quiet")

    info = read_build_info(tmp_path, env={}, commit_file=tmp_path / "none")

    assert info["commit"] == UNKNOWN and info["source"] == "unknown"


# -- carrying it through the API --------------------------------------------------


def test_the_admin_server_status_reports_the_build_once(tmp_path, monkeypatch):
    calls = []
    real = build_info.read_build_info

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    import web_chat.server as server_module

    monkeypatch.setattr(server_module, "read_build_info", counting)
    server, client, store = admin_client(tmp_path)
    try:
        for _ in range(3):
            response = client.get("/api/admin/server")
            assert response.status_code == 200
            build = response.json()["build"]
            assert build["version"] and build["commit"]
            assert SHORT_COMMIT.fullmatch(build["commit"]) or build["commit"] == UNKNOWN
        # Read at startup, not per request: the answer cannot drift while running.
        assert len(calls) == 1
    finally:
        store.close()


def test_only_the_admin_server_status_carries_the_build(tmp_path):
    server, client, store = admin_client(tmp_path)
    try:
        public = client.get("/api/server/status")
        admin = client.get("/api/admin/server")

        assert public.status_code == 200 and admin.status_code == 200
        # The reconnect endpoint keeps its small, fixed shape; the build is admin-only.
        assert "build" not in public.json()
        assert admin.json()["build"] == server.build_info
        assert set(server.build_info) == {"version", "commit", "commit_full", "source"}
    finally:
        store.close()
