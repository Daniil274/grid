"""Checkpoints of a workspace: what it looked like before a turn, to go back to.

Whatever tool changed the files - an edit tool, a shell command, a script the
agent ran - the files themselves are what is compared, so going back works the
same for all of them. A snapshot stages the workspace into a shadow git store
kept outside it (``git add -A`` with a ``GIT_DIR`` of its own, ``write-tree``):
the workspace's own repositories, their history and index are never touched,
and unchanged files cost a stat, not a copy. Each repository found inside the
workspace (a directory with ``.git``) is snapshotted as a root of its own, so
its files are not hidden behind a submodule link. What a root's ``.gitignore``
ignores is not snapshotted, nor are :data:`ALWAYS_EXCLUDED` directories.

A checkpoint is ``{"workspace": path, "roots": {relative root: tree id}}``. Restoring one brings
every file changed since back to how it was and removes files created since.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("grid.checkpoints")

#: Never part of a checkpoint: rebuilt, not authored, or the tools' own state.
ALWAYS_EXCLUDED = (
    "node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", ".mypy_cache/", ".pytest_cache/",
    ".codegraph/", ".beads/", ".beads.gate.lock",
)
#: How deep nested repositories are looked for.
MAX_ROOT_DEPTH = 4
#: Directories not searched for nested repositories.
_SKIP_SEARCH = {".git", "node_modules", ".venv", "venv", "__pycache__", ".codegraph", ".beads"}
_TIMEOUT = 300


class CheckpointError(RuntimeError):
    """A snapshot or a restore that could not be made."""


class Checkpoints:
    """Snapshots of workspaces in shadow git stores under one directory."""

    def __init__(self, store: Path) -> None:
        self.store = Path(store)
        # One git index per root: two chats of one workspace take turns.
        self._lock = threading.Lock()

    # -- snapshot --------------------------------------------------------------
    def snapshot(self, workspace: Path) -> dict:
        """The checkpoint of *workspace* now."""
        workspace = Path(workspace).resolve()
        with self._lock:
            roots = self._roots(workspace)
            return {
                "workspace": str(workspace),
                "roots": {self._label(root, workspace): self._tree(root, roots) for root in roots},
            }

    def changes(self, before: dict, after: dict) -> List[dict]:
        """Files that differ between two checkpoints: ``{"path", "status"}``,
        status ``added``, ``modified`` or ``deleted``."""
        found: List[dict] = []
        workspace = Path(before["workspace"])
        with self._lock:
            for label, old in (before.get("roots") or {}).items():
                new = (after.get("roots") or {}).get(label)
                if new is None or new == old:
                    continue
                for status, path in self._diff(self._git_dir(_root_of(workspace, label)), old, new):
                    found.append({"path": _join(label, path), "status": status})
        return found

    # -- restore ---------------------------------------------------------------
    def restore(self, workspace: Path, checkpoint: dict, *, only: Optional[set] = None) -> List[dict]:
        """Bring *workspace* back to *checkpoint*; the files it changed back.

        *only*: the files to bring back - paths as :meth:`changes` names them -
        when the workspace is shared and not every change since is the caller's.
        """
        workspace = Path(workspace).resolve()
        restored: List[dict] = []
        with self._lock:
            roots = self._roots(workspace)
            for label, old in (checkpoint.get("roots") or {}).items():
                root = _root_of(workspace, label)
                if not root.is_dir():
                    logger.warning("Checkpoint root %s is gone; its files are not restored", root)
                    continue
                now = self._tree(root, roots)
                if now == old:
                    continue
                git_dir = self._git_dir(root)
                owner = root.stat()
                for status, path in self._diff(git_dir, old, now):
                    if only is not None and _join(label, path) not in only:
                        continue
                    target = root / path
                    if status == "added":
                        # Created since the checkpoint: it goes.
                        target.unlink(missing_ok=True)
                        _prune_empty(target.parent, root)
                        restored.append({"path": _join(label, path), "status": "removed"})
                    else:
                        self._git(git_dir, root, "checkout", old, "--", path)
                        _own_like(target, owner)
                        restored.append({"path": _join(label, path), "status": "restored"})
                # The shadow index follows the files it put back.
                self._tree(root, roots)
        return restored

    # -- git -------------------------------------------------------------------
    def _roots(self, workspace: Path) -> List[Path]:
        roots = [workspace]
        for current, dirs, _files in os.walk(workspace):
            depth = len(Path(current).relative_to(workspace).parts)
            dirs[:] = [d for d in dirs if d not in _SKIP_SEARCH and depth < MAX_ROOT_DEPTH]
            for name in list(dirs):
                candidate = Path(current) / name
                if (candidate / ".git").exists():
                    roots.append(candidate)
        return roots

    @staticmethod
    def _label(root: Path, workspace: Path) -> str:
        relative = root.relative_to(workspace).as_posix()
        return relative or "."

    def _git_dir(self, root: Path) -> Path:
        git_dir = self.store / (hashlib.sha256(str(root).encode()).hexdigest()[:20] + ".git")
        if not (git_dir / "HEAD").exists():
            git_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "--git-dir", str(git_dir), "init", "--quiet"], check=True, timeout=_TIMEOUT)
            (git_dir / "info").mkdir(exist_ok=True)
            (git_dir / "info" / "exclude").write_text("\n".join(ALWAYS_EXCLUDED) + "\n")
        return git_dir

    def _tree(self, root: Path, roots: List[Path]) -> str:
        """Stage *root* and return its tree; the repositories inside it are
        roots of their own, left out here."""
        git_dir = self._git_dir(root)
        inner = [
            f":(exclude){other.relative_to(root).as_posix()}"
            for other in roots
            if other != root and other.is_relative_to(root)
        ]
        self._git(git_dir, root, "add", "--all", "--", ".", *inner)
        return self._git(git_dir, root, "write-tree").strip()

    def _diff(self, git_dir: Path, old: str, new: str) -> List[tuple]:
        out = self._git(git_dir, None, "diff-tree", "-r", "--no-renames", "--name-status", "-z", old, new)
        fields = [field for field in out.split("\0") if field]
        statuses = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}
        return [(statuses.get(fields[i][0], "modified"), fields[i + 1]) for i in range(0, len(fields) - 1, 2)]

    @staticmethod
    def _git(git_dir: Path, root: Optional[Path], *args: str) -> str:
        command = ["git", "--git-dir", str(git_dir)]
        if root is not None:
            command += ["--work-tree", str(root)]
        # Never the user's hooks, signing, or global config of the workspace.
        environment = {
            **os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "grid", "GIT_AUTHOR_EMAIL": "grid@localhost",
            "GIT_COMMITTER_NAME": "grid", "GIT_COMMITTER_EMAIL": "grid@localhost",
        }
        result = subprocess.run(
            [*command, "-c", "core.safecrlf=false", "-c", "core.autocrlf=false", *args],
            capture_output=True, text=True, env=environment, timeout=_TIMEOUT,
        )
        if result.returncode != 0:
            raise CheckpointError(f"git {args[0]} failed: {result.stderr.strip()[:500]}")
        return result.stdout


def _root_of(workspace: Path, label: str) -> Path:
    return workspace if label == "." else workspace / label


def _join(label: str, path: str) -> str:
    return path if label == "." else f"{label}/{path}"


def _own_like(path: Path, owner: os.stat_result) -> None:
    """A restored file belongs to the workspace's owner, not to the server."""
    if os.name != "posix" or os.geteuid() != 0:
        return
    try:
        os.chown(path, owner.st_uid, owner.st_gid)
    except OSError:
        logger.warning("Could not give %s back to its owner", path)


def _prune_empty(directory: Path, root: Path) -> None:
    """Remove directories a removed file left empty, up to *root*."""
    while directory != root and directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
        directory = directory.parent
