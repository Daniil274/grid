"""Workshop: the isolated clone where the administrator system prepares candidates.

A workshop is a git repository marked with ``grid.workshop=true``. It starts from
the controller's ``stable`` branch (received as a git bundle), holds one
experiment at a time on the ``experiment`` branch and hands the result back to
the controller as a bundle with only the new commits. Nothing here pushes, and
every operation refuses to run in a repository that is not a workshop, so an
agent cannot commit into someone's working copy by accident. Between those two
points the workshop may send snapshots of unfinished work to development trials.

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
# Candidates already sent for acceptance in the open experiment.
SUBMITTED = "grid.submitted"
CONTROLLER_STABLE = "refs/remotes/controller/stable"
TRIAL_REF = "refs/trials/latest"
# What the running administrator writes into its own working directory: trace
# databases, logs, caches. Never part of a candidate, whatever stable ignores.
RUNTIME_EXCLUDES = (
    ".grid/", "logs/", "traces/", "__pycache__/", ".pytest_cache/",
    "*.pyc", "*.db", "*.db-shm", "*.db-wal", "*.db-journal",
)
MAX_DIFF_CHARS = 30_000


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

    def submit(
        self,
        baseline: str,
        candidate: str,
        bundle: bytes | None,
        *,
        trial: bool = False,
        repetitions: int = 1,
        scenarios: list | None = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"baseline": baseline, "candidate": candidate}
        if bundle is not None:
            body["bundle"] = base64.b64encode(bundle).decode("ascii")
        if trial:
            body["repetitions"] = repetitions
            body["scenarios"] = list(scenarios or [])
        return self._call("POST", "/trials" if trial else "/experiments", json=body).json()

    def scenarios(self) -> list:
        return self._call("GET", "/scenarios").json()

    def status(self, experiment: str) -> Dict[str, Any]:
        return self._call("GET", f"/experiments/{experiment}").json()


class Workshop:
    def __init__(self, path: Path):
        self.path = Path(path)

    # -- git ---------------------------------------------------------------
    def git(
        self,
        *args: str,
        data: bytes | None = None,
        timeout: int = 120,
        env: Dict[str, str] | None = None,
    ) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            input=data,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-2000:]
            raise WorkshopError(f"git {args[0]} failed: {detail}")
        return result.stdout

    def _text(self, *args: str, env: Dict[str, str] | None = None) -> str:
        return self.git(*args, env=env).decode("utf-8", errors="replace").strip()

    def _config_all(self, key: str) -> list[str]:
        result = subprocess.run(
            ["git", "-C", str(self.path), "config", "--local", "--get-all", key],
            capture_output=True,
            check=False,
        )
        return result.stdout.decode().split() if result.returncode == 0 else []

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
        self._exclude_runtime()
        return self._text("status", "--porcelain", "--untracked-files=all").splitlines()

    def _exclude_runtime(self) -> None:
        """Keep the administrator's own runtime files out of every commit."""
        exclude = self.path / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        present = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        missing = [pattern for pattern in RUNTIME_EXCLUDES if pattern not in present]
        if missing:
            with exclude.open("a", encoding="utf-8") as file:
                file.write("\n# Grid workshop runtime files\n" + "\n".join(missing) + "\n")

    def _snapshot_tree(self) -> str:
        """Tree of the work as it is now, committed or not, via a throwaway index."""
        self._exclude_runtime()
        with tempfile.TemporaryDirectory(prefix="grid-tree-") as temp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp) / "index")}
            self.git("read-tree", "HEAD", env=env)
            self.git("add", "--all", env=env)
            return self._text("write-tree", env=env)

    def _relative(self, raw: str) -> str:
        """A repository path the agent named, refused when it leaves the workshop."""
        root = self.path.resolve()
        target = (root / raw).resolve()
        if target != root and root not in target.parents:
            raise WorkshopError(f"{raw} is outside the workshop")
        relative = target.relative_to(root).as_posix()
        if relative in {"", "."} or relative.split("/")[0] == ".git":
            raise WorkshopError(f"{raw} cannot be reverted: name files or directories of the repository")
        return relative

    # -- review ------------------------------------------------------------
    def files(self) -> list[Dict[str, str]]:
        """Every path the experiment changes against its baseline, committed or not."""
        baseline = self._open_experiment()
        tree = self._snapshot_tree()
        rows = self._text("diff", "--no-renames", "--name-status", baseline, tree).splitlines()
        binary = {
            line.split("\t")[-1]
            for line in self._text("diff", "--no-renames", "--numstat", baseline, tree).splitlines()
            if line.startswith("-\t-\t")
        }
        names = {"A": "added", "M": "modified", "D": "deleted", "T": "type changed"}
        return [
            {"path": path, "change": names.get(status[:1], status), **({"binary": "yes"} if path in binary else {})}
            for status, path in (row.split("\t", 1) for row in rows)
        ]

    def diff(self, path: str = "") -> Dict[str, Any]:
        """The whole experiment against its baseline, or one path of it."""
        baseline = self._open_experiment()
        tree = self._snapshot_tree()
        args = ["diff", "--no-renames", "--no-color", baseline, tree]
        if path:
            args += ["--", self._relative(path)]
        text = self.git(*args).decode("utf-8", errors="replace")
        report: Dict[str, Any] = {"baseline": baseline, "files": self.files()}
        if len(text) > MAX_DIFF_CHARS:
            report["diff"] = text[:MAX_DIFF_CHARS]
            report["truncated"] = f"{len(text)} characters; ask for one path at a time"
        else:
            report["diff"] = text
        return report

    def revert(self, paths: list[str]) -> Dict[str, Any]:
        """Return paths to their baseline state; paths new in the experiment disappear."""
        baseline = self._open_experiment()
        if not paths:
            raise WorkshopError("Name the files or directories to revert")
        for raw in paths:
            path = self._relative(raw)
            in_baseline = subprocess.run(
                ["git", "-C", str(self.path), "cat-file", "-e", f"{baseline}:{path}"],
                capture_output=True, check=False,
            ).returncode == 0
            self.git("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", path)
            if in_baseline:
                self.git("checkout", baseline, "--", path)
            self.git("clean", "-f", "-d", "-q", "--", path)
        return {"reverted": list(paths), "files": self.files()}

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
        # A new experiment: nothing of it has been submitted yet (fails harmlessly
        # when the key is absent).
        subprocess.run(
            ["git", "-C", str(self.path), "config", "--local", "--unset-all", SUBMITTED],
            capture_output=True, check=False,
        )
        return baseline

    def _open_experiment(self) -> str:
        self._require_workshop()
        baseline = self._config(BASELINE)
        if not baseline:
            raise WorkshopError("No experiment is open: call control_begin first")
        if self._text("rev-parse", "--abbrev-ref", "HEAD") != BRANCH:
            raise WorkshopError(f"The workshop must stay on the '{BRANCH}' branch")
        return baseline

    def trial(
        self, client: ControlClient, repetitions: int = 1, scenarios: list | None = None
    ) -> Dict[str, Any]:
        """Send the current work, committed or not, to a development trial.

        The snapshot is a commit built in a throwaway index: the experiment
        branch, the index and the files stay exactly as they are. Without any
        change the trial runs the baseline itself, which shows how the current
        stable behaves before deciding what to change.
        """
        baseline = self._open_experiment()
        tree = self._snapshot_tree()
        head = self._text("rev-parse", "HEAD")
        if head == baseline and tree == self._text("rev-parse", "HEAD^{tree}"):
            report = client.submit(
                baseline, baseline, None, trial=True, repetitions=repetitions, scenarios=scenarios
            )
            return {**report, "baseline": baseline, "candidate": baseline, "files": []}
        snapshot = self._text(
            "commit-tree", tree, "-p", head, "-m", "Development trial snapshot"
        )
        self.git("update-ref", TRIAL_REF, snapshot)
        bundle = self.git("bundle", "create", "-", TRIAL_REF, f"^{baseline}")
        files = self.files()
        report = client.submit(
            baseline, snapshot, bundle, trial=True, repetitions=repetitions, scenarios=scenarios
        )
        return {**report, "baseline": baseline, "candidate": snapshot, "files": files}

    def submit(self, client: ControlClient, message: str) -> Dict[str, Any]:
        """Commit every change of the experiment and send the candidate to the controller."""
        baseline = self._open_experiment()
        message = message.strip()
        # Before committing: a refused submission must leave the workshop as it was.
        if self._snapshot_tree() in self._config_all(SUBMITTED):
            raise WorkshopError(
                "This exact content was already submitted in this experiment. Evaluating "
                "it again is not new evidence: change the candidate first, or report the "
                "verdict you have."
            )
        if self._changes():
            if len(message) < 10:
                raise WorkshopError("Describe the change: what and why, at least 10 characters")
            self.git("add", "--all")
            self.git("commit", "--quiet", "--no-verify", "--file", "-", data=message.encode("utf-8"))
        candidate = self._text("rev-parse", "HEAD")
        if candidate == baseline:
            raise WorkshopError("Nothing to submit: the experiment has no changes")
        tree = self._text("rev-parse", "HEAD^{tree}")
        files = self.files()
        bundle = self.git("bundle", "create", "-", f"refs/heads/{BRANCH}", f"^{baseline}")
        report = client.submit(baseline, candidate, bundle)
        self.git("config", "--local", "--add", SUBMITTED, tree)
        return {**report, "baseline": baseline, "candidate": candidate, "files": files}


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
