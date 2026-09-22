"""The controller-owned evolution repository.

Grid evolves on the ``stable`` branch of a bare repository that only the
controller writes to. A workshop receives ``stable`` as a git bundle, commits a
candidate in its own clone and sends back a bundle with just the new commits.
Candidates land under ``refs/candidates/`` and never touch ``stable`` until an
accepted experiment is promoted with a compare-and-swap update.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

STABLE = "refs/heads/stable"
CANDIDATES = "refs/candidates/"
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
_SHA = re.compile(r"[a-f0-9]{40,64}")


class Repository:
    def __init__(self, path: Path):
        self.path = path

    def git(self, *args: str, timeout: int = 60, data: bytes | None = None) -> bytes:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.path), *args],
                input=data,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"git {args[0]} timed out after {timeout}s") from error
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace")[-2000:].strip()
            raise RuntimeError(f"git {args[0]} failed: {detail}")
        return result.stdout

    @classmethod
    def create(cls, path: Path, source: Path, ref: str = "HEAD") -> "Repository":
        """Make a bare evolution repository whose ``stable`` is *ref* of *source*."""
        if path.exists() and any(path.iterdir()):
            raise ValueError(f"{path} already exists and is not empty")
        path.mkdir(parents=True, exist_ok=True)
        repository = cls(path)
        repository.git("init", "--bare", "--quiet")
        commit = cls(source).resolve(ref)
        repository.git("fetch", "--quiet", "--no-tags", str(source.resolve()), commit)
        repository.git("update-ref", STABLE, commit)
        repository.git("symbolic-ref", "HEAD", STABLE)
        return repository

    def resolve(self, ref: str) -> str:
        value = self.git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
        sha = value.decode().strip()
        if not _SHA.fullmatch(sha):
            raise ValueError("Expected a Git commit")
        return sha

    def stable(self) -> str:
        return self.resolve(STABLE)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.path), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            timeout=60,
            check=False,
        )
        return result.returncode == 0

    def export(self) -> bytes:
        """Bundle of ``stable`` with full history: everything a workshop needs to start."""
        return self.git("bundle", "create", "-", STABLE, timeout=300)

    def receive(self, bundle: bytes, baseline: str, candidate: str) -> str:
        """Import a workshop bundle and return the verified candidate commit.

        The bundle must add *candidate* on top of *baseline*, a commit this
        repository already has. It is fetched into ``refs/candidates/<sha>`` only.
        """
        if len(bundle) > MAX_BUNDLE_BYTES:
            raise ValueError("Candidate bundle is too large")
        if not (_SHA.fullmatch(baseline) and _SHA.fullmatch(candidate)):
            raise ValueError("Baseline and candidate must be full commit SHAs")
        try:
            baseline = self.resolve(baseline)
        except RuntimeError as error:
            raise ValueError("Baseline is not a commit of the evolution repository") from error
        with tempfile.TemporaryDirectory(prefix="grid-bundle-") as temp:
            path = Path(temp) / "candidate.bundle"
            path.write_bytes(bundle)
            self.git("bundle", "verify", "--quiet", str(path))
            heads = [
                line.split()
                for line in self.git("bundle", "list-heads", str(path)).decode().splitlines()
            ]
            if [sha for sha, _ in heads] != [candidate]:
                raise ValueError("Bundle must carry exactly the declared candidate commit")
            target = CANDIDATES + candidate
            self.git("fetch", "--quiet", "--no-tags", str(path), f"{heads[0][1]}:{target}")
        if self.resolve(target) != candidate:
            raise ValueError("Imported commit does not match the declared candidate")
        if candidate == baseline or not self.is_ancestor(baseline, candidate):
            raise ValueError("Candidate must be a new commit on top of the baseline")
        return candidate

    def promote(self, baseline: str, candidate: str) -> None:
        """Move ``stable`` from *baseline* to *candidate*, refusing if it moved meanwhile."""
        if self.stable() != baseline:
            raise ValueError(
                "Stable moved since this experiment's baseline; evaluate the candidate again"
            )
        if not self.is_ancestor(baseline, candidate):
            raise ValueError("Candidate does not descend from the baseline")
        self.git("update-ref", STABLE, candidate, baseline)
