"""
CodeGraph tools - keep the workspace's code graph index ready.

The codegraph MCP server answers only for a project with a .codegraph/ index
and builds none itself. init_codegraph creates the index, or brings an
existing one up to date, so an agent's auto_run_tools can make CodeGraph
usable before the model is first asked. The server finds an index created
after it started.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, List, Optional

from agents import RunContextWrapper, function_tool

from utils import tool_effects as _effects
from utils.path_utils import container_path, resolve_agent_path_from_ctx, sanitize_text_for_agent_from_ctx
from utils.tool_isolation import CONTAINER as _CONTAINER
from utils.tool_requirements import Requires

#: A first index of a large repository takes minutes; a sync takes seconds.
CODEGRAPH_TIMEOUT_SECONDS = 600

# Startup is optional preparation, so a large index must not delay the first
# model response for minutes. An explicit tool call retains the full budget.
CODEGRAPH_STARTUP_TIMEOUT_SECONDS = 20

#: No usage statistics and no colour codes in what the agent reads.
_ENV = {"CODEGRAPH_TELEMETRY": "0", "NO_COLOR": "1"}

#: Exit code of `timeout` when it ended the command.
_TIMED_OUT = 124

#: What codegraph prints when another codegraph process holds the index; it passes in seconds.
_LOCKED = "database is locked"
_LOCK_RETRIES = 3
_LOCK_PAUSE_SECONDS = 2.0

#: What `codegraph status --json` calls the counts of the index.
_COUNTS = {"files": "fileCount", "nodes": "nodeCount", "edges": "edgeCount"}

#: Colour and cursor codes, which codegraph 0.9 writes despite NO_COLOR.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
#: Frame and status marks of codegraph's console output.
_FRAME = "│┌└◆●▲■◇|*. "
#: A progress bar line: "Parsing code ##### 100%".
_PROGRESS = re.compile(r"[#-]{5,}\s*\d+%")


def _container_id(context: Any) -> Optional[str]:
    """The container the run's commands go to: the run's own, else its factory's."""
    run = getattr(context, "context", None)
    return getattr(run, "container_id", None) or getattr(getattr(run, "factory", None), "container_id", None)


def _command(project: Path) -> List[str]:
    """`init` for a project without an index, else `sync`; both are idempotent.

    `-i` indexes on init in every codegraph version: 0.9 needs it, 1.x
    indexes anyway and accepts it. Its prompts read a closed stdin.
    """
    if (project / ".codegraph").is_dir():
        return ["codegraph", "sync", "."]
    return ["codegraph", "init", "-i", "."]


def _status_command() -> List[str]:
    """The index's own account of itself: files, nodes, edges, as JSON."""
    return ["codegraph", "status", "--json", "."]


def _run(command: List[str], project: Path, container_id: Optional[str], context: Any,
         timeout_seconds: float = CODEGRAPH_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run *command* in *project*: in the run's container when there is one."""
    if container_id:
        env = [arg for name, value in _ENV.items() for arg in ("-e", f"{name}={value}")]
        command = [
            "docker", "exec", "-w", container_path(str(project), getattr(context.context, "factory", None)),
            *env, container_id,
            # `timeout` ends codegraph inside the container too.
            "timeout", "-k", "5", str(timeout_seconds), *command,
        ]
        env_vars = None
    else:
        command = [shutil.which(command[0]) or command[0], *command[1:]]
        env_vars = {**os.environ, **_ENV}
    return subprocess.run(
        command,
        cwd=None if container_id else project,
        env=env_vars,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds + 10,
    )


def _summary(output: str) -> str:
    """codegraph's console output as plain lines: no colours, frame or progress bars."""
    lines = (line.strip().lstrip(_FRAME).strip() for line in _ANSI.sub("", output).splitlines())
    return "\n".join(line for line in lines if line and not _PROGRESS.search(line))


@function_tool
async def init_codegraph(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Build the CodeGraph index of the project, or update an existing one. Idempotent.

    Args:
        directory: Path to the project directory (default: ".")
    """
    try:
        project = Path(resolve_agent_path_from_ctx(directory or ".", context))
    except ValueError as exc:
        return f"❌ Error: {exc}"
    startup = bool(getattr(context, "operator_configured", False))
    budget = CODEGRAPH_STARTUP_TIMEOUT_SECONDS if startup else CODEGRAPH_TIMEOUT_SECONDS
    deadline = time.monotonic() + budget

    def timed_out() -> str:
        message = f"❌ CodeGraph indexing did not finish within {budget} s; CodeGraph is not available."
        if startup:
            message += (
                " Continue with grep_tool/glob_tool/file_read. Exclude virtual environments"
                " in codegraph.json, or call init_codegraph explicitly for a longer indexing budget."
            )
        return message

    async def run(command: List[str]) -> subprocess.CompletedProcess:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, budget)
        return await asyncio.to_thread(_run, command, project, _container_id(context), context, remaining)

    try:
        result = await run(_command(project))
        for _ in range(_LOCK_RETRIES):
            if result.returncode == 0 or _LOCKED not in result.stdout + result.stderr:
                break
            await asyncio.sleep(min(_LOCK_PAUSE_SECONDS, max(0, deadline - time.monotonic())))
            result = await run(_command(project))
    except subprocess.TimeoutExpired:
        return timed_out()
    except (OSError, ValueError) as exc:
        return f"❌ CodeGraph is not available: {exc}"
    output = sanitize_text_for_agent_from_ctx(_summary(result.stdout + "\n" + result.stderr), context)
    if result.returncode == _TIMED_OUT and _container_id(context):
        return timed_out()
    if result.returncode != 0 and "heap out of memory" in output.lower():
        return (
            "❌ CodeGraph is not available: indexing exhausted JavaScript heap memory. "
            "Exclude virtual environments and dependencies in codegraph.json, or index a smaller "
            "source directory. Continue with grep_tool/glob_tool/file_read."
        )
    if result.returncode != 0:
        return f"❌ CodeGraph is not available (exit {result.returncode}): {output}"
    try:
        status = await run(_status_command())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        status = None
    return sanitize_text_for_agent_from_ctx(_report(_index_counts(status), project, output), context)


def _index_counts(status: Optional[subprocess.CompletedProcess]) -> Optional[dict]:
    """Files, nodes and edges of the index from `codegraph status --json`; None when unreadable."""
    if status is None or status.returncode != 0:
        return None
    try:
        document = json.loads(status.stdout)
        return {key: int(document[name]) for key, name in _COUNTS.items()}
    except (ValueError, KeyError, TypeError):
        return None


def _report(counts: Optional[dict], project: Path, output: str) -> str:
    """What the agent is told: ready only when the index holds code, else what is true instead."""
    where = str(project)
    if counts is None:
        return (
            f"CodeGraph ran in {where}, but its index could not be read back, so it is unknown whether "
            f"the index holds any code. Check with codegraph_status before relying on CodeGraph.\n{output}"
        )
    if counts["files"] == 0:
        return (
            f"ℹ CodeGraph index of {where} is empty: 0 files indexed, so CodeGraph has nothing to answer with. "
            "The index covers source code only; it says nothing about the other files of the directory. "
            "Use grep_tool/glob_tool/file_read here, and call init_codegraph again once source files exist, "
            "or point CodeGraph tools at another indexed project with projectPath.\n" + output
        )
    return (
        f"✅ CodeGraph index of {where} is ready: {counts['files']} files, {counts['nodes']} nodes, "
        f"{counts['edges']} edges; use the codegraph tools for this project.\n{output}"
    )


def _codegraph_problem() -> Optional[str]:
    """Why codegraph cannot run: without a container it must be on this machine's PATH."""
    if shutil.which("codegraph") or shutil.which("docker"):
        return None
    return "codegraph is not on PATH, and docker for a run's container is missing"


TOOL_REQUIREMENTS = {
    "init_codegraph": Requires(
        check=_codegraph_problem,
        hint="npm i -g @colbymchenry/codegraph (it is in the grid-agent image)",
    ),
}

CODEGRAPH_TOOLS = {
    "init_codegraph": init_codegraph,
}

# Runs in the run's container when there is one (utils.tool_isolation).
TOOL_ISOLATION = {
    "init_codegraph": _CONTAINER,
}

# Writes its index, .codegraph/, into the workspace (utils.tool_effects).
TOOL_EFFECTS = {
    "init_codegraph": _effects.write(),
}
