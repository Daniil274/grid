"""
Workspace tools - what a directory holds, stated as a fact.

An empty CodeGraph index says only that there is no source code to index; it
says nothing about the other files of the directory. workspace_overview lists
the directory itself, so an agent never has to infer its contents.
"""

import os
import sys
from itertools import islice
from pathlib import Path
from typing import Any, List

_GRID_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_GRID_ROOT))

from agents import RunContextWrapper, function_tool
from utils import tool_effects as _effects
from utils.path_utils import display_agent_path_from_ctx, extra_roots, resolve_agent_path_from_ctx
from utils.tool_isolation import WORKSPACE as _WORKSPACE

#: Entries listed per directory; the rest is counted.
MAX_ENTRIES = 60
#: Children counted per listed directory before it reads "N+".
COUNT_LIMIT = 1000
#: Names that tools and the framework create, not the user.
SERVICE_NAMES = {
    ".git", ".gitignore", ".beads", ".beads.gate.lock", ".codegraph", ".grid",
    "__pycache__", "node_modules", ".venv", "venv",
}


def _children(path: Path) -> str:
    """How many entries a directory holds, capped."""
    try:
        with os.scandir(path) as it:
            count = sum(1 for _ in islice(it, COUNT_LIMIT + 1))
    except OSError:
        return "unreadable"
    return f"{COUNT_LIMIT}+ items" if count > COUNT_LIMIT else f"{count} items"


def _listing(path: Path, label: str) -> List[str]:
    """One directory as lines: its summary first, then dirs, then files."""
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return [f"{label}: unreadable ({exc.strerror or exc})"]
    user = [e for e in entries if e.name not in SERVICE_NAMES]
    summary = f"{label}: {len(entries)} entries, {len(user)} of them not service entries"
    if not user:
        summary += " - no user files"
    lines = [summary]
    for entry in entries[:MAX_ENTRIES]:
        mark = "  [service]" if entry.name in SERVICE_NAMES else ""
        if entry.is_dir():
            lines.append(f"  {entry.name}/  ({_children(entry)}){mark}")
        else:
            try:
                size = f"{entry.stat().st_size} B"
            except OSError:
                size = "unreadable"
            lines.append(f"  {entry.name}  ({size}){mark}")
    if len(entries) > MAX_ENTRIES:
        lines.append(f"  ... and {len(entries) - MAX_ENTRIES} more")
    return lines


@function_tool
def workspace_overview(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Lists what a directory and the other spaces of the run (such as /user) hold.

    Args:
        directory: Directory to list (default: the workspace)
    """
    try:
        base = Path(resolve_agent_path_from_ctx(directory or ".", context))
    except ValueError as exc:
        return f"❌ Error: {exc}"
    shown = display_agent_path_from_ctx(directory or ".", context)
    if not base.is_dir():
        return f"❌ Not a directory: {shown}"
    lines = _listing(base, shown)
    factory: Any = getattr(getattr(context, "context", None), "factory", None)
    for prefix, host in extra_roots(factory):
        if host != base.resolve():
            lines.append("")
            lines.extend(_listing(host, prefix))
    return "\n".join(lines)


# Where this tool acts (utils.tool_isolation): the server, inside the run's spaces
TOOL_ISOLATION = {
    "workspace_overview": _WORKSPACE,
}

# What this tool does (utils.tool_effects), for the action policy
TOOL_EFFECTS = {
    "workspace_overview": _effects.read("directory"),
}
