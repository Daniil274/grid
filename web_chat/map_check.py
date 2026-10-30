"""Diagnose the map's tracker read the way the server performs it.

    python -m web_chat.map_check <project-dir> [--project-id ID] [--json]

The command runs the *server's own* functions - ``_project_space``,
``_own_tracker``, ``_resolve_runner``, ``_read_tracker`` and the task shaping
of ``_tasks`` - through a stand-in space whose only project is the directory
named. It therefore cannot drift from what ``GET /api/projects/{id}/map``
would do: it *is* that path, with a trace attached.

It prints which bd runner was chosen and why, the bd path or isolation image,
the command, the reason code, the task count, bd's real stderr (a terminal,
not a browser) and whether the project tree changed during the read. The exit
code is 0 only when the tracker was read, its tasks belong to the project and
the project directory is byte-for-byte as it was.

The read itself follows the server's rule: bd opens a bounded temporary copy,
so pointing this at a real project must not change it. Never point the command
at ``/project`` or a real user space in place; copy ``.beads`` first.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # the real import waits for main(), which pins logging first
    from web_chat import map_api

#: How much of a file the snapshot hashes; bigger files are compared by size
#: and moment alone, so a diagnostic on a large space stays quick.
HASH_LIMIT_BYTES = 4 * 1024 * 1024


class _ProjectSpace:
    """The one field ``_project_space`` reads: where the project spaces live."""

    chat_spaces = True

    def __init__(self, project_spaces: Path) -> None:
        self.layout = SimpleNamespace(project_spaces=project_spaces)


def _digest(path: Path) -> str:
    """The sha256 of one regular file, bounded by :data:`HASH_LIMIT_BYTES`."""
    try:
        size = path.stat().st_size
        if size > HASH_LIMIT_BYTES:
            return "large"
        with path.open("rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return "unreadable"


def snapshot(root: Path) -> Dict[str, tuple]:
    """Every path of *root* with mode, size, moment and content: a tree, exactly."""
    state: Dict[str, tuple] = {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            entry: tuple = (stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns)
            if stat.S_ISREG(info.st_mode):
                entry += (_digest(path),)
            state[str(path.relative_to(root))] = entry
    return state


def _changed(before: Dict[str, tuple], after: Dict[str, tuple]) -> List[str]:
    """Which paths appeared, vanished or differ - relative, never absolute."""
    return sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )


def _copy_hint(directory: Path) -> Optional[str]:
    """Why a copy may read as no tracker at all: its directory name changed.

    bd takes the embedded Dolt database's name from the directory it sits in,
    and ``_own_tracker`` refuses a tracker whose name disagrees. A copy under
    a different name therefore has no tracker the map would admit - the hint
    says so instead of leaving "no_tracker" unexplained.
    """
    metadata = directory / ".beads" / "metadata.json"
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    named = data.get("dolt_database") if isinstance(data, dict) else None
    expected = directory.name.replace("-", "_")
    if isinstance(named, str) and named and named != expected:
        return (f"the tracker names its database {named!r} but this directory is {directory.name!r}: "
                "copy the project under its own directory name")
    return None


def _report(directory: Path, project_id: str, trace: map_api.ReadTrace,
            tasks: map_api.TasksSlice, before: Dict[str, tuple],
            after: Dict[str, tuple]) -> Dict[str, Any]:
    """The whole diagnosis as one object: every field a terminal needs."""
    changed = _changed(before, after)
    runner = trace.runner
    return {
        "project_id": project_id,
        "directory": str(directory),
        "runner": {
            "kind": runner.kind,
            "executable": runner.executable,
            "image": runner.image,
            "why": runner.why,
        },
        "command": list(trace.command) if trace.command else None,
        "returncode": trace.returncode,
        "reason_code": tasks.reason.value if tasks.reason else None,
        "reason": tasks.payload.get("reason"),
        "available": bool(tasks.payload.get("available")),
        "tasks": tasks.payload.get("total") or 0,
        "project_tree": {
            "changed": bool(changed),
            "changed_paths": changed,
            "entries": len(before),
        },
        "note": _copy_hint(directory),
        "stderr": trace.stderr or "",
    }


def _emit_text(report: Dict[str, Any]) -> None:
    """The same diagnosis, line by line, for a person on a terminal."""
    runner = report["runner"]
    print(f"project:      {report['project_id']}")
    print(f"directory:    {report['directory']}")
    selected = runner["kind"] or "none"
    print(f"runner:       {selected}")
    print(f"  why:        {runner['why']}")
    print(f"  bd:         {runner['executable'] or '-'}")
    print(f"  image:      {runner['image'] or '-'}")
    print(f"command:      {' '.join(report['command']) if report['command'] else '-'}")
    print(f"exit:         {report['returncode'] if report['returncode'] is not None else '-'}")
    print(f"reason_code:  {report['reason_code'] or 'none'}")
    if report["reason"]:
        print(f"reason:       {report['reason']}")
    if report["note"]:
        print(f"note:         {report['note']}")
    print(f"tasks:        {report['tasks']}")
    tree = report["project_tree"]
    if tree["changed"]:
        print(f"project_tree: CHANGED ({len(tree['changed_paths'])} path(s)): "
              + ", ".join(tree["changed_paths"][:10]))
    else:
        print(f"project_tree: unchanged ({tree['entries']} entries)")
    if report["stderr"]:
        print("stderr:")
        for line in report["stderr"].rstrip("\n").splitlines():
            print(f"  {line}")


def main(argv: Optional[List[str]] = None) -> int:
    """Run the diagnosis; 0 only when the map would show these tasks cleanly."""
    # bd and the map log through the root logger; configure it for a terminal
    # *before* the map's own imports, so nothing of theirs lands in stdout and
    # corrupts --json.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m web_chat.map_check",
        description="Read a project's map tracker exactly as the server does, and say what happened.",
    )
    parser.add_argument("project_dir", help="the project's space directory (it holds .beads)")
    parser.add_argument("--project-id", default=None,
                        help="the project's id; defaults to the directory's name")
    parser.add_argument("--json", action="store_true", help="print the diagnosis as JSON")
    args = parser.parse_args(argv)

    # The project's own logger configures a root handler on stdout at import;
    # swap the stream for the duration so no log line can corrupt --json.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        from web_chat import map_api
        from web_chat.space import SPACE_ID
    finally:
        sys.stdout = real_stdout

    directory = Path(args.project_dir).expanduser().absolute()
    if not directory.is_dir():
        print(f"not a directory: {directory}", file=sys.stderr)
        return 2
    project_id = args.project_id or directory.name
    # _project_space composes <project_spaces>/<project_id>: the two must agree.
    if not SPACE_ID.fullmatch(project_id) or project_id != directory.name:
        print(f"project id {project_id!r} does not name the directory {directory.name!r}: "
              "pass the project's own space directory, or its matching --project-id",
              file=sys.stderr)
        return 2

    before = snapshot(directory)
    trace = map_api.ReadTrace()
    # The same choice _read_tracker makes; asking early keeps the runtime
    # visible even when the read stops before bd (no tracker, say).
    trace.runner = map_api._resolve_runner()
    space = _ProjectSpace(directory.parent)
    try:
        tasks = asyncio.run(map_api._tasks(space, project_id, trace))
    except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never crashes
        print(f"the read raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    after = snapshot(directory)

    report = _report(directory, project_id, trace, tasks, before, after)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _emit_text(report)
    if not report["available"] or report["project_tree"]["changed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
