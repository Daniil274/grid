"""Which code this server actually runs: its version and its commit.

An operator restarts the server and expects the change to be there. When it is
not, the first question is which code is running at all - so the answer is read
once, at startup, and only shown: this never decides anything else.

The commit comes from git. A built image usually has no repository, so
``GRID_BUILD_COMMIT`` (set by the deployment) and the ``build_commit.txt`` file
next to this module are the fallbacks; when none of them knows, the honest
answer is ``"unknown"``, not a guess.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Optional

UNKNOWN = "unknown"

#: The repository this code was checked out from: ``web_chat/`` sits at its root.
ROOT = Path(__file__).resolve().parent.parent
#: An explicit commit, for deployments that build an image without ``.git``.
COMMIT_ENV = "GRID_BUILD_COMMIT"
#: A commit written by a build step, next to this module.
COMMIT_FILE = Path(__file__).resolve().parent / "build_commit.txt"

#: What a short commit is: enough to recognize, short enough for a footer line.
SHORT = 7

_HEX = frozenset("0123456789abcdef")


def _version() -> str:
    try:
        from core import __version__
    except Exception:  # a broken install must not take the status call down
        return UNKNOWN
    version = str(__version__ or "").strip()
    return version or UNKNOWN


def _git_commit(root: Path) -> Optional[str]:
    """The full commit of *root*'s checkout, or None when git cannot tell."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = done.stdout.strip().lower()
    if done.returncode != 0 or len(commit) < SHORT or not set(commit) <= _HEX:
        return None
    return commit


def _file_commit(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def read_build_info(
    root: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    commit_file: Optional[Path] = None,
) -> dict[str, str]:
    """Version, commit and where the commit came from.

    An explicit ``GRID_BUILD_COMMIT`` wins: a deployment that stamps its own
    build knows better than the checkout it happens to carry. Then git, then
    the build file, then ``"unknown"``.
    """
    env = os.environ if env is None else env
    raw = (env.get(COMMIT_ENV) or "").strip()
    source = "env" if raw else ""
    if not raw:
        raw = _git_commit(root or ROOT) or ""
        source = "git" if raw else ""
    if not raw:
        raw = _file_commit(commit_file or COMMIT_FILE) or ""
        source = "file" if raw else ""
    if not raw:
        return {"version": _version(), "commit": UNKNOWN, "commit_full": UNKNOWN, "source": "unknown"}
    commit = raw.lower()
    return {"version": _version(), "commit": commit[:SHORT], "commit_full": commit, "source": source}
