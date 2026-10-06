"""
CodeGraph tools - keep the workspace's code graph index ready.

The codegraph MCP server answers only for a project with a .codegraph/ index
and builds none itself. init_codegraph creates the index, or brings an
existing one up to date, so an agent's auto_run_tools can make CodeGraph
usable before the model is first asked. The server finds an index created
after it started.
"""

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Optional

from agents import RunContextWrapper, function_tool

from utils import tool_effects as _effects
from utils.path_utils import container_path, resolve_agent_path_from_ctx, sanitize_text_for_agent_from_ctx
from utils.tool_isolation import CONTAINER as _CONTAINER
from utils.tool_requirements import Requires

#: A first index of a large repository takes minutes; a sync takes seconds.
CODEGRAPH_TIMEOUT_SECONDS = 600

#: No usage statistics and no colour codes in what the agent reads.
_ENV = {"CODEGRAPH_TELEMETRY": "0", "NO_COLOR": "1"}

#: Exit code of `timeout` when it ended the command.
_TIMED_OUT = 124

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


def _run(command: List[str], project: Path, container_id: Optional[str], context: Any) -> subprocess.CompletedProcess:
    """Run *command* in *project*: in the run's container when there is one."""
    if container_id:
        env = [arg for name, value in _ENV.items() for arg in ("-e", f"{name}={value}")]
        command = [
            "docker", "exec", "-w", container_path(str(project), getattr(context.context, "factory", None)),
            *env, container_id,
            # `timeout` ends codegraph inside the container too.
            "timeout", "-k", "5", str(CODEGRAPH_TIMEOUT_SECONDS), *command,
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
        timeout=CODEGRAPH_TIMEOUT_SECONDS + 15,
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
    try:
        result = await asyncio.to_thread(_run, _command(project), project, _container_id(context), context)
    except subprocess.TimeoutExpired:
        return f"❌ CodeGraph indexing did not finish within {CODEGRAPH_TIMEOUT_SECONDS} s; CodeGraph is not available."
    except (OSError, ValueError) as exc:
        return f"❌ CodeGraph is not available: {exc}"
    output = sanitize_text_for_agent_from_ctx(_summary(result.stdout + "\n" + result.stderr), context)
    if result.returncode == _TIMED_OUT and _container_id(context):
        return f"❌ CodeGraph indexing did not finish within {CODEGRAPH_TIMEOUT_SECONDS} s; CodeGraph is not available."
    if result.returncode != 0:
        return f"❌ CodeGraph is not available (exit {result.returncode}): {output}"
    return f"✅ CodeGraph index is ready; use the codegraph tools for this project.\n{output}"


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
