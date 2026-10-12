"""grep_tool's search when ripgrep is missing, in a process of its own.

A regular expression can take exponential time on one line, and a Python
thread cannot be stopped: a search left running in the server would hold its
CPU - and its GIL, every user's chat - for good. A process can be killed, so
grep_tool runs this one with a timeout (utils.process_limits).

Reads a JSON request on stdin, writes the matches as JSON on stdout:

    python -m utils.grep_worker < {"pattern", "base", "extensions",
                                   "case_sensitive", "use_regex", "max_results",
                                   "max_per_file", "skip_dirs"}
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from utils import confined_fs
from utils.path_utils import factory_path_context


def _confined_to(base: Path) -> Any:
    """A stand-in factory rooting :mod:`utils.confined_fs` at *base*.

    The worker is a process of its own and cannot see the run's factory, but a
    link inside the tree can still point at files outside it: the walk must not
    follow one. ``confined_fs`` only takes its no-follow walk for a workspace a
    container shares; asking for it here keeps every read inside *base*, which
    is the boundary the search may not leave."""
    return SimpleNamespace(
        container_id="grep-worker",
        config=SimpleNamespace(get_working_directory=lambda: str(base)),
    )


def search(request: Dict[str, Any]) -> Dict[str, Any]:
    flags = 0 if request.get("case_sensitive") else re.IGNORECASE
    pattern = request["pattern"]
    try:
        regex = re.compile(pattern if request.get("use_regex") else re.escape(pattern), flags)
    except re.error as exc:
        return {"error": f"Invalid regular expression: {exc}"}

    base = Path(request["base"]).resolve()
    extensions = [ext.lower() for ext in request.get("extensions", [])]
    skip = set(request.get("skip_dirs", []))
    max_results = int(request.get("max_results", 100))
    max_per_file = int(request.get("max_per_file", 5))

    results: List[str] = []
    with factory_path_context(_confined_to(base)):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
            for filename in files:
                if extensions and Path(filename).suffix.lower() not in extensions:
                    continue
                filepath = Path(root) / filename
                found = 0
                try:
                    # A link is refused (LinkRefused), never followed: nothing
                    # outside *base* is read, even if the tree changed meanwhile.
                    with confined_fs.open_file(filepath, "r", encoding="utf-8", errors="replace") as handle:
                        for lineno, line in enumerate(handle, 1):
                            if not regex.search(line):
                                continue
                            content = line.rstrip()
                            if len(content) > 200:
                                content = content[:200] + "..."
                            results.append(f"{filepath.relative_to(base).as_posix()}:{lineno}: {content}")
                            found += 1
                            if found >= max_per_file or len(results) >= max_results:
                                break
                except OSError:
                    continue
                if len(results) >= max_results:
                    return {"results": results}
    return {"results": results}


def main() -> int:
    json.dump(search(json.load(sys.stdin)), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
