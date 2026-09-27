"""The MCP servers an agent uses, started once and shared.

Each configured MCP server is started on first use and kept; with a
container the server runs inside it. Relies on ``self.config``,
``self.container_id``, ``self._container_workdir`` and ``self._mcp_servers``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from core.managers.mcp_manager import ResilientMCPServerStdio

logger = logging.getLogger("grid.agent_factory")


class McpSetup:
    """The MCP servers an agent uses, started once and shared.

    Each configured MCP server is started on first use and kept; with a
    container the server runs inside it. Relies on ``self.config``,
    ``self.container_id``, ``self._container_workdir`` and ``self._mcp_servers``.
    """

    async def _create_mcp_servers(self, mcp_tool_names: List[str]) -> List[Any]:
        """Create and connect MCP servers using the Agents SDK."""
        logger.info(f"Creating MCP servers for tools: {mcp_tool_names}")
        servers: list[Any] = []
        unavailable: list[str] = []
        for name in mcp_tool_names:
            try:
                logger.debug(f"Attempting to create MCP server: {name}")
                server = await self._get_mcp_server(name)
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

    async def _get_mcp_server(self, tool_name: str) -> Optional[Any]:
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

        if cache_key in self._mcp_servers:
            logger.debug("Reusing cached MCP server: %s", cache_key)
            return self._mcp_servers[cache_key]

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

        await server.connect()
        logger.info(f"MCP server connected successfully: {tool_name}")
        self._mcp_servers[cache_key] = server
        return server
