"""Small STDIO MCP surface that lets Codex launch Grid worker agents."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from core.agent_catalog import AgentCatalog
from core.agent_factory import AgentFactory
from core.background_agents import BackgroundAgentSupervisor
from core.config import Config

LOGGER = logging.getLogger("grid.codex_mcp")

SERVER_INSTRUCTIONS = """
Grid is a worker-agent launcher for Codex. Starting a worker takes exactly two
tool actions: (1) call grid_get_system once to obtain and cache valid model keys,
tool names, and limits; (2) call grid_start_agent with a chosen model, task, and
tools. grid_start_agent returns immediately with an agent_id and deadline. Do not
poll it. Continue planning or other work, then call grid_wait_agents when ready to
wait for completion, a bounded timer, or interruption. Use grid_get_agents for a
non-blocking snapshot and grid_interrupt_agent to stop a worker.
""".strip()


def create_server(supervisor: BackgroundAgentSupervisor) -> FastMCP:
    """Create the MCP server around an already configured supervisor."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
        try:
            yield {"supervisor": supervisor}
        finally:
            await supervisor.shutdown()

    mcp = FastMCP(
        "grid-codex-workers",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Grid worker system",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    async def grid_get_system() -> Dict[str, Any]:
        """Return valid OpenCode models, worker tools, limits, and the two-step launch contract.

        Call this once before the first worker launch and cache its result. Model
        keys and tool names passed to grid_start_agent must come from this response.
        """
        return supervisor.get_system_info()

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Start Grid worker agent",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        structured_output=True,
    )
    async def grid_start_agent(
        model: str,
        task: str,
        tools: List[str],
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Start one worker in the background and return immediately.

        Args:
            model: Exact model key returned by grid_get_system.
            task: Self-contained execution task with expected output and checks.
            tools: Exact tool names returned by grid_get_system. An empty list is valid.
            timeout_seconds: Wall-clock deadline including queue time; omit for the default.
        """
        result = await supervisor.start_agent(
            model=model,
            task=task,
            tools=tools,
            timeout_seconds=timeout_seconds,
        )
        result["next_action"] = (
            "Continue other work. Later call grid_wait_agents with this agent_id; "
            "do not poll grid_get_agents."
        )
        return result

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Grid worker status",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    async def grid_get_agents(
        agent_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Return a non-blocking snapshot of selected workers, or all workers."""
        return await supervisor.get_agents(agent_ids)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Wait for Grid workers",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    async def grid_wait_agents(
        agent_ids: List[str],
        timeout_seconds: float = 180.0,
        return_when: Literal["first", "all"] = "first",
    ) -> Dict[str, Any]:
        """Wait until a worker event or a bounded timer fires.

        The wait never cancels workers. It returns on the first/all requested
        workers finishing (including timeout or interruption), or when this wait's
        own timeout expires.
        """
        return await supervisor.wait_agents(
            agent_ids,
            timeout_seconds=timeout_seconds,
            return_when=return_when,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Interrupt Grid worker",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    async def grid_interrupt_agent(
        agent_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Cancel one running or queued worker and return its terminal state."""
        return await supervisor.interrupt_agent(agent_id, reason=reason)

    return mcp


def build_supervisor(
    *,
    config_path: str = "config.yaml",
    workdir: Optional[str] = None,
    tools_directory: str = "examples/coder/tools",
    provider: str = "opencode",
    max_concurrency: int = 4,
    default_timeout_seconds: int = 300,
    max_timeout_seconds: int = 900,
) -> BackgroundAgentSupervisor:
    """Build the catalog and runtime used by the Codex-facing MCP server."""
    resolved_config = str(Path(config_path).resolve())
    resolved_workdir = str(Path(workdir).resolve()) if workdir else None
    config = Config(resolved_config, resolved_workdir)
    catalog = AgentCatalog(
        config,
        tools_directory=tools_directory,
        provider_key=provider,
    )
    factory = AgentFactory(
        config=config,
        working_directory=resolved_workdir,
        tracing_level=None,
    )
    return BackgroundAgentSupervisor(
        factory=factory,
        catalog=catalog,
        max_concurrency=max_concurrency,
        default_timeout_seconds=default_timeout_seconds,
        max_timeout_seconds=max_timeout_seconds,
    )


def _protect_stdio_transport() -> None:
    """Keep application logging away from MCP's stdout JSON-RPC stream."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if (
            isinstance(handler, logging.StreamHandler)
            and getattr(getattr(handler, "stream", None), "name", None) == "<stdout>"
        ):
            root.removeHandler(handler)
    root.setLevel(logging.WARNING)


def serve(**kwargs: Any) -> None:
    """Build and run the Grid MCP server over STDIO."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)
    # Some legacy Grid/tool initializers attach stdout loggers while importing.
    # Redirect the entire construction phase so no startup byte can corrupt the
    # STDIO JSON-RPC channel before FastMCP begins serving.
    with redirect_stdout(sys.stderr):
        supervisor = build_supervisor(**kwargs)
    _protect_stdio_transport()
    LOGGER.warning(
        "Grid Codex MCP ready: %d models, %d tools",
        len(supervisor.catalog.model_keys()),
        len(supervisor.catalog.tool_names()),
    )
    create_server(supervisor).run(transport="stdio")


__all__ = ["SERVER_INSTRUCTIONS", "build_supervisor", "create_server", "serve"]
