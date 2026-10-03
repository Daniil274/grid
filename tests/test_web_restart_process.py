"""Exercise the CLI's real Uvicorn shutdown/re-exec and persistent sign-in."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from tests.test_web_settings import SYSTEM
from web_chat.accounts import open_accounts


def test_cli_restart_reexecutes_and_preserves_sign_in(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.yaml"
    config.write_text(SYSTEM)
    data = tmp_path / "data"
    accounts = open_accounts(data)
    password = "local test password only"
    accounts.create_user("admin", password, role="admin")
    accounts._store.close()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen([
            sys.executable, "-m", "web_chat", "--accounts", "--trusted-users",
            "--config", str(config), "--data-dir", str(data), "--port", str(port),
        ], cwd=root, env={**os.environ, "PYTHONPATH": str(root)}, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=url, headers={"Origin": url}, timeout=2) as client:
                def wait_for(check):
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            log.seek(0)
                            raise AssertionError("server exited instead of re-executing:\n" + log.read())
                        try:
                            if result := check():
                                return result
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    log.seek(0)
                    raise AssertionError(log.read())

                wait_for(lambda: client.get("/login").status_code == 200)
                assert client.post("/api/auth/login", json={"username": "admin", "password": password}).status_code == 200
                before = client.get("/api/admin/server").json()
                assert before["enabled"]
                cookies = dict(client.cookies)
                assert client.post("/api/admin/server/restart").status_code == 202

                def restarted():
                    response = client.get("/api/admin/server")
                    assert response.status_code == 200  # same cookie still authenticates
                    status = response.json()
                    return status if status["instance_id"] != before["instance_id"] else None

                after = wait_for(restarted)
                assert after["phase"] == "running"
                assert after["recovery_errors"] == 0
                assert dict(client.cookies) == cookies
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
