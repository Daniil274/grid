"""Docker adapter. Only this module knows about processes and container flags."""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from .models import Policy, Trial

# This code comes from the controller installation, never from the candidate.
# It runs in a separate container without the candidate's filesystem or secrets.
VERIFIER = r"""
import json, time, urllib.request, urllib.error

def request(check):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open("http://candidate:8000" + check["path"], timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if response.status != check["expected_status"]:
            return False
        pointer = check["json_pointer"]
        if pointer is None:
            return True
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return False
        value = json.loads(body)
        if pointer:
            for key in pointer[1:].split("/"):
                key = key.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
        return type(value) is type(check["expected"]) and value == check["expected"]

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

started = time.monotonic()
deadline = started + PLAN["timeout_seconds"] - 5
ready = False
while time.monotonic() < deadline:
    try:
        ready = request({"path": "/", "expected_status": 200, "json_pointer": None})
    except Exception:
        pass
    if ready:
        break
    time.sleep(0.25)
results = {}
for check in PLAN["checks"]:
    try:
        results[check["name"]] = ready and request(check)
    except Exception:
        results[check["name"]] = False
print(json.dumps({"checks": results, "elapsed_seconds": time.monotonic() - started}))
"""


class DockerRuntime:
    def command(
        self, args: list[str], *, timeout: int = 30, data: bytes | None = None
    ) -> bytes:
        try:
            result = subprocess.run(
                args, input=data, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"{args[0]} operation timed out after {timeout}s"
            ) from error
        if result.returncode:
            # Only CLI diagnostics: application logs are never read here. Commands
            # carry environment variable names, never credential values.
            detail = result.stderr.decode("utf-8", errors="replace")[-2000:].strip()
            raise RuntimeError(
                f"{args[0]} {args[1]} failed with exit code {result.returncode}: {detail}"
            )
        return result.stdout

    def revision(self, repo: Path, ref: str) -> str:
        value = (
            self.command(
                [
                    "git",
                    "-C",
                    str(repo),
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    ref + "^{commit}",
                ]
            )
            .decode()
            .strip()
        )
        if not re.fullmatch(r"[a-f0-9]{40,64}", value):
            raise ValueError("Expected a Git commit")
        return value

    def image(self, ref: str) -> str:
        value = (
            self.command(["docker", "image", "inspect", "--format", "{{.Id}}", ref])
            .decode()
            .strip()
        )
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
            raise ValueError(
                "Expected a local immutable image ID; build or pull the image first"
            )
        return value

    def build(
        self, repo: Path, commit: str, runtime: str, experiment: str, timeout: int
    ) -> str:
        # git archive includes committed files only, never .env from the worktree.
        with tempfile.TemporaryDirectory(prefix="grid-source-") as temp:
            archive = Path(temp) / "source.tar"
            self.command(
                [
                    "git",
                    "-C",
                    str(repo),
                    "archive",
                    "--format=tar",
                    "--output",
                    str(archive),
                    commit,
                ],
                timeout=timeout,
            )
            context = io.BytesIO()
            with (
                tarfile.open(archive) as source,
                tarfile.open(fileobj=context, mode="w") as target,
            ):
                for member in source:
                    if not member.isfile():
                        if member.isdir():
                            continue
                        raise ValueError(
                            "Candidate source may contain only regular files and directories"
                        )
                    if member.name.startswith("/") or ".." in member.name.split("/"):
                        raise ValueError("Unsafe archive path")
                    # Fresh headers: git's global pax header (the commit ID) is
                    # copied into every re-added member otherwise, and BuildKit
                    # misreads such a context (Dockerfile parse and gRPC errors).
                    copy = tarfile.TarInfo("source/" + member.name)
                    copy.size = member.size
                    copy.mode = 0o755 if member.mode & 0o111 else 0o644
                    copy.mtime = member.mtime
                    target.addfile(copy, source.extractfile(member))
                recipe = (
                    f"FROM {self._pinned(runtime)}\n"
                    "COPY source/ /opt/grid/candidate/\n"
                    "ENV PYTHONPATH=/opt/grid/candidate PYTHONDONTWRITEBYTECODE=1 HOME=/workspace\n"
                    "WORKDIR /workspace\n"
                    "USER 65532:65532\n"
                    "ENTRYPOINT []\n"
                    'CMD ["python", "-m", "web_chat", "--routing", "/opt/grid/candidate/routing.yaml", "--path", "/workspace", "--host", "0.0.0.0", "--port", "8000"]\n'
                ).encode()
                member = tarfile.TarInfo("Dockerfile")
                member.size = len(recipe)
                target.addfile(member, io.BytesIO(recipe))
            tag = f"grid-candidate:{experiment}-{commit[:12]}"
            self.command(
                [
                    "docker",
                    "build",
                    "--quiet",
                    "--network=none",
                    "--label",
                    f"grid.experiment={experiment}",
                    "--tag",
                    tag,
                    "-",
                ],
                timeout=timeout,
                data=context.getvalue(),
            )
            return self.image(tag)

    def _pinned(self, image: str) -> str:
        """A local tag naming *image* by its full ID, usable in FROM.

        BuildKit reads ``FROM sha256:...`` as a registry name and tries to pull
        it. The tag spells out the whole immutable ID, so it can only ever point
        to that image.
        """
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Expected a local immutable image ID")
        tag = "grid-control-runtime:" + image.removeprefix("sha256:")
        self.command(["docker", "tag", image, tag])
        return tag

    @staticmethod
    def limits() -> list[str]:
        return [
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=256",
            "--memory=1g",
            "--cpus=2",
            "--user=65532:65532",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=128m,mode=1777",
            "--tmpfs=/workspace:rw,nosuid,nodev,size=256m,mode=1777",
            "--log-driver=none",
        ]

    def trial(
        self, image: str, verifier: str, experiment: str, index: int, policy: Policy
    ) -> Trial:
        name = f"grid-eval-{experiment}-{index}"
        label = f"grid.experiment={experiment}"
        network_args = [] if policy.allow_network else ["--internal"]
        self.command(
            ["docker", "network", "create", *network_args, "--label", label, name]
        )
        env = [part for key in policy.environment_names for part in ("--env", key)]
        self.command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--label",
                label,
                "--network",
                name,
                "--network-alias=candidate",
                *self.limits(),
                *env,
                image,
            ]
        )
        plan = {
            "checks": [asdict(check) for check in policy.checks],
            "timeout_seconds": policy.timeout_seconds,
        }
        script = "PLAN = " + repr(plan) + "\n" + VERIFIER
        output = self.command(
            [
                "docker",
                "run",
                "--interactive",
                "--name",
                name + "-verifier",
                "--label",
                label,
                "--network",
                name,
                *self.limits(),
                "--entrypoint=python",
                verifier,
                "-",
            ],
            timeout=policy.timeout_seconds,
            data=script.encode(),
        )
        result = json.loads(output)
        return Trial(**result)

    def cleanup(self, experiment: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", experiment):
            raise ValueError("Invalid experiment ID")
        label = f"label=grid.experiment={experiment}"
        errors = []
        for kind, list_args, remove_args in (
            (
                "container",
                ["ps", "--all", "--quiet", "--filter", label],
                ["rm", "--force"],
            ),
            (
                "network",
                ["network", "ls", "--quiet", "--filter", label],
                ["network", "rm"],
            ),
        ):
            try:
                ids = self.command(["docker", *list_args]).decode().split()
                for resource in ids:
                    if not re.fullmatch(r"[a-f0-9]+", resource):
                        raise ValueError("Invalid Docker resource ID")
                    self.command(["docker", *remove_args, resource])
            except Exception as error:
                errors.append(f"{kind}: {error}")
        if errors:
            raise RuntimeError("Cleanup incomplete: " + "; ".join(errors))
