"""The MCP servers agents use, started once and shared.

McpServers starts each configured MCP server on first use and keeps it. A
server is keyed by its tool, and by the working directory and container it
was started for: one started for another workspace or container is never
handed out. With a container the server runs inside it (``docker exec``),
confined like the agent's own commands.

Each server lives in a task of its own, which connects it, keeps it open and
closes it. The MCP client's streams belong to the task that opened them
(anyio cancel scopes): a server connected in the task of a web request died
with that request, and the next turn got it from the cache closed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from core.managers.mcp_manager import ResilientMCPServerStdio

logger = logging.getLogger("grid.agent_factory")


class McpServers:
    """The MCP servers of one factory: its config, workspace and container."""

    def __init__(self, config: Any, context_manager: Any, container_id: Optional[str], container_workdir: str) -> None:
        self.config = config
        self.context_manager = context_manager
        self.container_id = container_id
        self._container_workdir = container_workdir
        self._servers: Dict[str, Any] = {}
        # The task that owns each server, and the event that tells it to close.
        self._owners: Dict[str, Tuple["asyncio.Task[None]", asyncio.Event]] = {}

    def __len__(self) -> int:
        return len(self._servers)

    async def for_tools(self, mcp_tool_names: List[str]) -> List[Any]:
        """Create and connect MCP servers using the Agents SDK."""
        logger.info(f"Creating MCP servers for tools: {mcp_tool_names}")
        servers: list[Any] = []
        unavailable: list[str] = []
        for name in mcp_tool_names:
            try:
                logger.debug(f"Attempting to create MCP server: {name}")
                server = await self.get(name)
                if server is not None:
                    servers.append(server)
                    logger.info(f"✅ MCP server created successfully: {name}")
                else:
                    unavailable.append(name)
                    logger.warning(f"❌ MCP server creation returned None: {name}")
            except Exception as e:
                unavailable.append(name)
                logger.error(
                    f"❌ MCP server creation failed: {name} - {e}", exc_info=True
                )

        if unavailable:
            logger.warning(f"Unavailable MCP servers: {unavailable}")
            try:
                self.context_manager.set_metadata("mcp_unavailable", unavailable)
            except Exception as exc:
                logger.warning(
                    "Failed to store MCP availability metadata: %s", exc, exc_info=exc
                )

        logger.info(
            f"Created {len(servers)} MCP servers out of {len(mcp_tool_names)} requested"
        )
        return servers

    async def get(self, tool_name: str) -> Optional[Any]:
        """Get or create an SDK-based MCP server (MCPServerStdio)."""
        tool_config = self.config.get_tool(tool_name)
        if tool_config.type != "mcp":
            logger.warning(
                f"Tool '{tool_name}' is not of type 'mcp' (type={tool_config.type})"
            )
            return None

        cwd = self.config.get_working_directory()

        # IMPORTANT:
        # MCP tools like terminal/filesystem are started with cwd and/or a cwd argument.
        # If we cache only by tool_name, then per-user TG runs can reuse a server created
        # for a different cwd, breaking the "cwd is always user workspace" invariant.
        # Also include container_id in cache key
        cache_key = (
            f"{tool_name}::{cwd}"
            if getattr(tool_config, "add_working_directory", False)
            else tool_name
        )
        if self.container_id:
            cache_key += f"::{self.container_id}"

        owner = self._owners.get(cache_key)
        if owner is not None and owner[0].done():
            logger.warning("MCP server %s has stopped; starting it again", cache_key)
            self._servers.pop(cache_key, None)
            self._owners.pop(cache_key, None)
        if cache_key in self._servers:
            logger.debug("Reusing cached MCP server: %s", cache_key)
            return self._servers[cache_key]

        server_command = tool_config.server_command or []
        if not server_command:
            logger.error(f"MCP tool '{tool_name}' has no server_command configured")
            return None

        command = server_command[0]
        args = list(server_command[1:])
        # Make npx non-interactive
        if command.lower() in ("npx", "npx.cmd") and "-y" not in args:
            args.insert(0, "-y")

        env = dict(tool_config.env_vars or {})

        # Add working directory to args if configured (CRITICAL FIX for filesystem MCP)
        if getattr(tool_config, "add_working_directory", False):
            # Agent sees root as "/". In container pass "/" as the allowed root so MCP
            # filesystem accepts any absolute agent path (e.g. /docs, /sub/file).
            # The docker exec still runs with -w /workspace so relative ops work correctly.
            target_cwd = "/" if self.container_id else cwd
            args.append(target_cwd)
            logger.debug(f"Added working directory to MCP server args: {target_cwd}")

            # CRITICAL SAFETY (TG invariant):
            # Prevent `git` from walking up from the per-user workspace into the main repo.
            # project root when executed from host workspace.
            target_ceiling = self._container_workdir if self.container_id else cwd
            env.setdefault("GIT_CEILING_DIRECTORIES", target_ceiling)
            env.setdefault("GIT_DISCOVERY_ACROSS_FILESYSTEM", "0")
            # For beads, ensure it doesn't try to use a host daemon
            env.setdefault("BEADS_DAEMON", "0")

        # Wrap command for Docker execution if container_id is provided
        if self.container_id:
            # Construct docker exec command
            # docker exec -i -w <container workdir> [ENV] <container_id> <command> <args>

            # Forward proxy env vars from host so npm/npx can download packages inside container
            for _proxy_var in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "all_proxy",
            ):
                _proxy_val = os.environ.get(_proxy_var)
                if _proxy_val:
                    env.setdefault(_proxy_var, _proxy_val)

            # Save original command/args for logging/debug
            orig_cmd = command
            orig_args = args

            # Build docker exec args
            args = ["exec", "-i", "-w", self._container_workdir]

            # Pass environment variables
            for k, v in env.items():
                args.extend(["-e", f"{k}={v}"])

            args.extend([self.container_id, orig_cmd])
            args.extend(orig_args)

            command = "docker"

            logger.info(
                f"Wrapping MCP server in container {self.container_id}",
                extra={
                    "tool_name": tool_name,
                    "mcp_command": command,
                    "mcp_args": args,
                },
            )

        logger.info(
            f"Creating MCP server: {tool_name} | command={command} | args={args} | cwd={cwd}"
        )

        max_output_tokens = getattr(
            self.config.config.settings, "max_tool_output_tokens", None
        )
        server = ResilientMCPServerStdio(
            params={
                "command": command,
                "args": args,
                "env": env,
                "cwd": cwd,
            },
            cache_tools_list=True,
            name=tool_name,
            client_session_timeout_seconds=300,  # 5 minutes timeout (increased from default 5s)
            max_output_tokens=max_output_tokens,
        )

        self._owners[cache_key] = await self._start(server)
        logger.info(f"MCP server connected successfully: {tool_name}")
        self._servers[cache_key] = server
        return server

    async def _start(self, server: Any) -> Tuple["asyncio.Task[None]", asyncio.Event]:
        """Connect *server* in a task of its own; it stays open until the
        returned event is set, and the same task then closes it."""
        connected: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def own() -> None:
            try:
                await server.connect()
            except asyncio.CancelledError:
                connected.cancel()
                raise
            except Exception as exc:
                connected.set_exception(exc)
                return
            connected.set_result(None)
            try:
                await stop.wait()
            finally:
                try:
                    await server.cleanup()
                except Exception as exc:
                    logger.warning(
                        "Failed to clean up MCP server %s: %s", getattr(server, "name", "unknown"), exc, exc_info=exc
                    )

        task = asyncio.create_task(own(), name=f"mcp-server:{getattr(server, 'name', 'unknown')}")
        try:
            await asyncio.shield(connected)
        except asyncio.CancelledError:
            # The caller gave up: the server is never handed out, so close it.
            stop.set()
            raise
        return task, stop

    async def close(self) -> None:
        """Disconnect every server; a failure is logged, the rest still close."""
        owners = self._owners
        self._owners = {}
        for _, stop in owners.values():
            stop.set()
        for key, server in self._servers.items():
            try:
                if key in owners:
                    await owners[key][0]  # its own task closes it
                else:
                    await server.cleanup()
            except asyncio.CancelledError:
                logger.debug("MCP cleanup cancelled", exc_info=True)
            except Exception as exc:
                logger.warning(
                    "Failed to clean up MCP server %s: %s", getattr(server, "name", "unknown"), exc, exc_info=exc
                )
        self._servers.clear()
