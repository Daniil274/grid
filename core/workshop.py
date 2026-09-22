"""Workshop: the isolated clone where the administrator system prepares candidates.

A workshop is a git repository marked with ``grid.workshop=true``. It starts from
the controller's ``stable`` branch (received as a git bundle), holds one
experiment at a time on the ``experiment`` branch and hands the result back to
the controller as a bundle with only the new commits. Nothing here pushes, and
every operation refuses to run in a repository that is not a workshop, so an
agent cannot commit into someone's working copy by accident.

Usage in the workshop container: ``grid-workshop init /workspace/grid``.
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict

import httpx

BRANCH = "experiment"
MARKER = "grid.workshop"
BASELINE = "grid.baseline"
CONTROLLER_STABLE = "refs/remotes/controller/stable"


class WorkshopError(RuntimeError):
    """An operation the workshop refuses, with a message meant for the agent."""


class ControlClient:
    """Authenticated client of the controller's restricted API."""

    def __init__(self, http: httpx.Client):
        self.http = http

    @classmethod
    def from_env(cls) -> "ControlClient":
        url = os.environ.get("GRID_CONTROL_URL", "").rstrip("/")
        token = os.environ.get("GRID_CONTROL_TOKEN", "")
        if not url or not token:
            raise WorkshopError("The workshop requires GRID_CONTROL_URL and GRID_CONTROL_TOKEN")
        return cls(
            httpx.Client(
                base_url=url,
                headers={"Authorization": "Bearer " + token},
                timeout=120,
                trust_env=False,
                follow_redirects=False,
            )
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise WorkshopError(f"Controller is unreachable: {error}") from error
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise WorkshopError(f"Controller refused ({response.status_code}): {detail}")
        return response

    def source(self) -> bytes:
        return self._call("GET", "/source").content

    def submit(self, baseline: str, candidate: str, bundle: bytes) -> Dict[str, Any]:
        body = {
            "baseline": baseline,
            "candidate": candidate,
            "bundle": base64.b64encode(bundle).decode("ascii"),
        }
        return self._call("POST", "/experiments", json=body).json()

    def status(self, experiment: str) -> Dict[str, Any]:
        return self._call("GET", f"/experiments/{experiment}").json()


class Workshop:
    def __init__(self, path: Path):
        self.path = Path(path)

    # -- git ---------------------------------------------------------------
    def git(self, *args: str, data: bytes | None = None, timeout: int = 120) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            input=data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-2000:]
            raise WorkshopError(f"git {args[0]} failed: {detail}")
        return result.stdout

    def _text(self, *args: str) -> str:
        return self.git(*args).decode("utf-8", errors="replace").strip()

    def _config(self, key: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), "config", "--local", "--get", key],
            capture_output=True,
            check=False,
        )
        return result.stdout.decode().strip() if result.returncode == 0 else ""

    # -- invariants --------------------------------------------------------
    def is_workshop(self) -> bool:
        return (self.path / ".git").exists() and self._config(MARKER) == "true"

    def _require_workshop(self) -> None:
        if not self.is_workshop():
            raise WorkshopError(
                f"{self.path} is not a workshop. Experiments run only in the workshop "
                "container's clone (grid-workshop init), never in a working copy."
            )

    def _changes(self) -> list[str]:
        return self._text("status", "--porcelain", "--untracked-files=all").splitlines()

    # -- lifecycle ---------------------------------------------------------
    def init(self, client: ControlClient) -> str:
        """Create the workshop clone if needed and return the stable commit it tracks."""
        if not (self.path / ".git").exists():
            if self.path.exists() and any(self.path.iterdir()):
                raise WorkshopError(f"{self.path} is not empty and not a workshop")
            self.path.mkdir(parents=True, exist_ok=True)
            self.git("init", "--quiet", "--initial-branch", BRANCH)
            for key, value in (
                (MARKER, "true"),
                ("user.name", "Grid Workshop"),
                ("user.email", "workshop@grid.local"),
                ("core.autocrlf", "false"),
            ):
                self.git("config", "--local", key, value)
            return self.begin(client)
        self._require_workshop()
        return self._fetch_stable(client)

    def _fetch_stable(self, client: ControlClient) -> str:
        with tempfile.TemporaryDirectory(prefix="grid-source-") as temp:
            bundle = Path(temp) / "stable.bundle"
            bundle.write_bytes(client.source())
            self.git("bundle", "verify", "--quiet", str(bundle))
            self.git(
                "fetch", "--quiet", "--no-tags", "--force", str(bundle),
                f"refs/heads/stable:{CONTROLLER_STABLE}",
            )
        return self._text("rev-parse", CONTROLLER_STABLE)

    def begin(self, client: ControlClient) -> str:
        """Start an experiment on the current stable; returns the baseline commit."""
        self._require_workshop()
        if self._changes():
            raise WorkshopError(
                "The workshop has uncommitted changes from an unfinished experiment. "
                "Submit them with control_submit or report them before starting anew."
            )
        baseline = self._fetch_stable(client)
        self.git("checkout", "--quiet", "--force", "-B", BRANCH, baseline)
        self.git("config", "--local", BASELINE, baseline)
        return baseline

    def submit(self, client: ControlClient, message: str) -> Dict[str, Any]:
        """Commit every change of the experiment and send the candidate to the controller."""
        self._require_workshop()
        baseline = self._config(BASELINE)
        if not baseline:
            raise WorkshopError("No experiment is open: call control_begin first")
        if self._text("rev-parse", "--abbrev-ref", "HEAD") != BRANCH:
            raise WorkshopError(f"The workshop must stay on the '{BRANCH}' branch")
        message = message.strip()
        if self._changes():
            if len(message) < 10:
                raise WorkshopError("Describe the change: what and why, at least 10 characters")
            self.git("add", "--all")
            self.git("commit", "--quiet", "--no-verify", "--file", "-", data=message.encode("utf-8"))
        candidate = self._text("rev-parse", "HEAD")
        if candidate == baseline:
            raise WorkshopError("Nothing to submit: the experiment has no changes")
        bundle = self.git("bundle", "create", "-", f"refs/heads/{BRANCH}", f"^{baseline}")
        report = client.submit(baseline, candidate, bundle)
        return {**report, "baseline": baseline, "candidate": candidate}


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare the Grid workshop clone")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Clone stable from the controller if needed")
    init.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        stable = Workshop(args.path).init(ControlClient.from_env())
    except WorkshopError as error:
        parser.exit(2, f"grid-workshop: {error}\n")
    print(f"Workshop {args.path} tracks stable {stable}")


if __name__ == "__main__":
    main()
