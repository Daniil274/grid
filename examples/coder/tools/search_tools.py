"""
Search Tools — file search (glob) and text search in files (grep).
All paths are restricted to the agent's working directory.
"""

import fnmatch
import json
import os
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Optional

import sys
_GRID_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_GRID_ROOT))

from agents import function_tool
from utils.path_utils import display_agent_path_auto, get_current_factory, resolve_agent_path_auto
from utils.process_limits import run_host_command


MAX_RESULTS = 100
MAX_OUTPUT_LINES = 100
MAX_PER_FILE_MATCHES = 5

#: A recursive glob stops after this many directory entries or seconds: a walk
#: of a huge tree would hold a tool thread - and the server's CPU - for minutes.
GLOB_SCAN_LIMIT = 200_000
GLOB_SECONDS = 20

# Directories always skipped during traversal
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache"}


def _resolve_dir(directory: str) -> tuple:
    """
    Returns (visible, resolved_path).
    Raises ValueError if directory escapes workspace.
    """
    visible = display_agent_path_auto(directory)
    resolved = resolve_agent_path_auto(directory)  # raises ValueError on escape
    path = Path(resolved)
    if not path.exists():
        raise FileNotFoundError(f"❌ Directory not found: {visible}")
    if not path.is_dir():
        raise NotADirectoryError(f"❌ Not a directory: {visible}")
    return visible, path


# ---------------------------------------------------------------------------
# glob_tool
# ---------------------------------------------------------------------------

@function_tool
def glob_tool(
    pattern: str,
    directory: str = ".",
    max_results: int = MAX_RESULTS,
) -> str:
    """
    Searches for files and directories by glob pattern within the working directory.

    Supports:
      *.py              — Python files in the root of the directory
      src/**/*.js       — all JS files in src recursively
      **/test_*.py      — all test files recursively

    Args:
        pattern:     Glob pattern
        directory:   Search directory (default current)
        max_results: Maximum number of results

    Returns:
        List of found files
    """
    try:
        visible, base_path = _resolve_dir(directory)
    except ValueError as exc:
        return str(exc)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return str(exc)

    try:
        results = []
        truncated = False
        is_recursive = "**" in pattern

        if is_recursive:
            # Split at the first '**/' to get optional dir prefix and file pattern.
            # e.g. "src/**/*.py"  → dir_prefix="src", file_pat="*.py"
            #      "**/*.py"      → dir_prefix="",    file_pat="*.py"
            #      "**"           → dir_prefix="",    file_pat="*"
            star_idx = pattern.index("**")
            dir_prefix = pattern[:star_idx].rstrip("/\\")
            remainder = pattern[star_idx + 2:].lstrip("/\\")
            file_pat = remainder if remainder else "*"

            walk_root = (base_path / dir_prefix) if dir_prefix else base_path
            if not walk_root.exists():
                return f"❌ Subdirectory not found: {dir_prefix or '.'}"

            deadline = time.monotonic() + GLOB_SECONDS
            scanned = 0
            for root, dirs, files in os.walk(walk_root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                scanned += len(dirs) + len(files)
                if scanned > GLOB_SCAN_LIMIT or time.monotonic() > deadline:
                    truncated = True
                    break

                for dirname in dirs:
                    if fnmatch.fnmatch(dirname, file_pat):
                        full = Path(root) / dirname
                        try:
                            results.append(str(full.relative_to(base_path)) + "/")
                        except ValueError:
                            results.append(str(full) + "/")
                        if len(results) >= max_results:
                            break

                if len(results) >= max_results:
                    break

                for filename in files:
                    if fnmatch.fnmatch(filename, file_pat):
                        full = Path(root) / filename
                        try:
                            results.append(str(full.relative_to(base_path)))
                        except ValueError:
                            results.append(str(full))
                        if len(results) >= max_results:
                            break

                if len(results) >= max_results:
                    break
        else:
            # Non-recursive: only items directly inside base_path
            for item in sorted(base_path.iterdir()):
                if item.is_file() and fnmatch.fnmatch(item.name, pattern):
                    results.append(item.name)
                elif item.is_dir() and fnmatch.fnmatch(item.name, pattern):
                    results.append(item.name + "/")
                if len(results) >= max_results:
                    break

        partial = (
            "\n\n⚠️ The directory is too large to walk whole: stopped early. "
            "Narrow the directory or the pattern." if truncated else ""
        )
        if not results:
            return f"🔍 Nothing found for pattern '{pattern}' in '{visible}'{partial}"

        results.sort()
        header = f"🔍 Found {len(results)} results for pattern '{pattern}':"
        if len(results) >= max_results:
            header += f" (showing first {max_results})"

        displayed = results[:MAX_OUTPUT_LINES]
        output = "\n".join(displayed)
        if len(results) > MAX_OUTPUT_LINES:
            output += f"\n\n... and {len(results) - MAX_OUTPUT_LINES} more results ..."

        return f"{header}\n\n{output}{partial}"

    except Exception as exc:
        return f"❌ Search error: {exc}"


# ---------------------------------------------------------------------------
# grep_tool
# ---------------------------------------------------------------------------

def _normalize_grep_args(
    pattern: str,
    file_extensions: Optional[str],
    max_results: int,
) -> tuple[str, str, int, Optional[str]]:
    """Normalize public grep arguments and return (pattern, extensions, limit, error)."""
    if pattern is None:
        return "", "", MAX_RESULTS, "❌ Pattern is required"

    pattern = str(pattern)
    if pattern == "":
        return "", "", MAX_RESULTS, "❌ Pattern must not be empty"

    extensions = "" if file_extensions is None else str(file_extensions)

    try:
        limit = int(max_results)
    except (TypeError, ValueError):
        limit = MAX_RESULTS
    limit = max(1, min(limit, MAX_RESULTS))

    return pattern, extensions, limit, None


#: Threads one ripgrep search may use.
RG_THREADS = 2
#: Seconds one search may take, wherever it runs.
GREP_TIMEOUT = 30
#: Exit code of `timeout` when it ended the command.
_TIMED_OUT = 124
#: Where a run's container mounts its workspace.
CONTAINER_ROOT = "/workspace"


def _ripgrep_args(
    pattern: str,
    target: str,
    file_extensions: str,
    case_sensitive: bool,
    use_regex: bool,
) -> list:
    """ripgrep's command line, --json output (handles Windows paths correctly)."""
    # --no-config: no ripgrep config file of this machine applies.
    cmd = [
        "rg", "--no-config", "--json", "--line-number",
        "--threads", str(RG_THREADS), "--max-count", str(MAX_PER_FILE_MATCHES),
    ]

    if not case_sensitive:
        cmd.append("--ignore-case")
    if not use_regex:
        cmd.append("--fixed-strings")

    # Use --glob instead of --type-add so multiple extensions work correctly
    if file_extensions:
        for ext in file_extensions.split(","):
            ext = ext.strip()
            if ext:
                glob_pat = f"*{ext}" if ext.startswith(".") else f"*.{ext}"
                cmd.extend(["--glob", glob_pat])

    # The pattern and the path are never options: a pattern such as
    # "--pre=sh" would make ripgrep run a program on every file it searches.
    cmd.extend(["--regexp", pattern, "--", target])
    return cmd


def _ripgrep_matches(stdout: str, base: str, max_results: int, posix: bool = False) -> list:
    """``path:line: text`` of each match in ripgrep's JSON output, relative to *base*."""
    path_type = PurePosixPath if posix else Path
    results = []
    for line in stdout.strip().splitlines():
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if data.get("type") != "match":
            continue

        match_data = data.get("data", {})
        file_path_raw = match_data.get("path", {}).get("text", "")
        lineno = match_data.get("line_number", 0)
        line_text = match_data.get("lines", {}).get("text", "").rstrip()

        if len(line_text) > 200:
            line_text = line_text[:200] + "..."

        try:
            rel = str(path_type(file_path_raw).relative_to(path_type(base)))
        except ValueError:
            rel = file_path_raw

        results.append(f"{rel}:{lineno}: {line_text}")
        if len(results) >= max_results:
            break
    return results


def _report(pattern: str, results: list, max_results: int) -> str:
    if not results:
        return f"🔍 Nothing found for pattern '{pattern}'"

    header = f"🔍 Found {len(results)} matches for '{pattern}'"
    if len(results) >= max_results:
        header += f" (showing first {max_results})"
    return f"{header}\n\n" + "\n".join(results)


def _grep_in_container(
    container_id: str,
    relative_dir: str,
    pattern: str,
    file_extensions: str,
    case_sensitive: bool,
    use_regex: bool,
    max_results: int,
) -> str:
    """ripgrep in the run's container: the user's search takes that user's
    CPUs, not the server's (core.managers.container_manager)."""
    target = CONTAINER_ROOT if relative_dir in ("", ".") else f"{CONTAINER_ROOT}/{relative_dir}"
    cmd = [
        "docker", "exec", "-w", CONTAINER_ROOT, container_id,
        # `timeout` ends the search inside the container too.
        "timeout", "-k", "5", str(GREP_TIMEOUT),
        *_ripgrep_args(pattern, target, file_extensions, case_sensitive, use_regex),
    ]
    try:
        result = run_host_command(cmd, timeout=GREP_TIMEOUT + 15)
    except subprocess.TimeoutExpired:
        return f"⏱️ Search exceeded {GREP_TIMEOUT} seconds: narrow the pattern or the directory"
    if result.returncode == _TIMED_OUT:
        return f"⏱️ Search exceeded {GREP_TIMEOUT} seconds: narrow the pattern or the directory"
    # 0 = matches, 1 = none, 2 = an error - possibly with matches in other files.
    results = _ripgrep_matches(result.stdout, target, max_results, posix=True)
    if result.returncode not in (0, 1) and not results:
        detail = (result.stderr or "").strip()[-500:] or f"exit code {result.returncode}"
        return f"❌ Search error: {detail}"
    return _report(pattern, results, max_results)


def _grep_with_ripgrep(
    pattern: str,
    base_path: Path,
    file_extensions: str,
    case_sensitive: bool,
    use_regex: bool,
    max_results: int,
) -> str:
    """ripgrep on this machine (a run without a container)."""
    cmd = _ripgrep_args(pattern, str(base_path), file_extensions, case_sensitive, use_regex)
    result = run_host_command(cmd, timeout=GREP_TIMEOUT)

    # returncode 0=matches found, 1=no match, 2=error
    if result.returncode == 2:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return _report(pattern, _ripgrep_matches(result.stdout, str(base_path), max_results), max_results)


def _grep_with_python(
    pattern: str,
    base_path: Path,
    file_extensions: str,
    case_sensitive: bool,
    use_regex: bool,
    max_results: int,
) -> str:
    """Without ripgrep: Python's search, in a process killed after GREP_TIMEOUT
    (utils.grep_worker) - a runaway regular expression cannot be stopped in a thread."""
    extensions = []
    if file_extensions:
        for ext in file_extensions.split(","):
            ext = ext.strip().lower()
            if ext:
                extensions.append(ext if ext.startswith(".") else "." + ext)

    request = {
        "pattern": pattern,
        "base": str(base_path),
        "extensions": extensions,
        "case_sensitive": case_sensitive,
        "use_regex": use_regex,
        "max_results": max_results,
        "max_per_file": MAX_PER_FILE_MATCHES,
        "skip_dirs": sorted(SKIP_DIRS),
    }
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(_GRID_ROOT), os.environ.get("PYTHONPATH")])))
    try:
        result = run_host_command(
            [sys.executable, "-m", "utils.grep_worker"],
            input=json.dumps(request),
            env=env,
            timeout=GREP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"⏱️ Search exceeded {GREP_TIMEOUT} seconds: narrow the pattern or the directory"
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        detail = (result.stderr or "").strip()[-500:] or f"exit code {result.returncode}"
        return f"❌ Search error: {detail}"
    if answer.get("error"):
        return f"❌ {answer['error']}"
    return _report(pattern, answer.get("results", []), max_results)


@function_tool(strict_mode=False)
def grep_tool(
    pattern: str,
    directory: str = ".",
    file_extensions: str = "",
    case_sensitive: bool = False,
    use_regex: bool = False,
    max_results: int = MAX_RESULTS,
) -> str:
    """
    Searches for text in files within the working directory.

    Args:
        pattern:        Text or regular expression to search
        directory:      Directory to search
        file_extensions: Comma-separated extensions (e.g. "py,js,ts")
        case_sensitive: Case sensitive matching
        use_regex:      Use regular expression
        max_results:    Maximum number of matches

    Returns:
        Found lines with file names and line numbers
    """
    pattern, file_extensions, max_results, arg_error = _normalize_grep_args(
        pattern, file_extensions, max_results
    )
    if arg_error:
        return arg_error

    try:
        visible, base_path = _resolve_dir(directory)
    except ValueError as exc:
        return str(exc)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return str(exc)

    container_id = getattr(get_current_factory(), "container_id", None)
    if container_id:
        return _grep_in_container(
            container_id, display_agent_path_auto(directory),
            pattern, file_extensions, case_sensitive, use_regex, max_results,
        )

    try:
        return _grep_with_ripgrep(
            pattern, base_path, file_extensions, case_sensitive, use_regex, max_results
        )
    except subprocess.TimeoutExpired:
        return f"⏱️ Search exceeded {GREP_TIMEOUT} seconds: narrow the pattern or the directory"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass  # ripgrep not available or error — fall through to Python

    return _grep_with_python(
        pattern, base_path, file_extensions, case_sensitive, use_regex, max_results
    )


# Where these tools act (utils.tool_isolation): in the server, inside the run's workspace
from utils.tool_isolation import WORKSPACE as _WORKSPACE  # noqa: E402

TOOL_ISOLATION = {
    "glob_tool": _WORKSPACE,
    "grep_tool": _WORKSPACE,
}

# What these tools do (utils.tool_effects), for the action policy
from utils import tool_effects as _effects  # noqa: E402

TOOL_EFFECTS = {
    "glob_tool": _effects.read("directory"),
    "grep_tool": _effects.read("directory"),
}
