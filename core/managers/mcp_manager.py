"""
MCP Manager for Model Context Protocol servers.

Manages lifecycle of MCP servers including creation, caching,
connection, and cleanup.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Union
from agents.mcp import MCPServerStdio
try:
    from mcp.types import CallToolResult, TextContent
except ImportError:
    import mcp.types
    CallToolResult = mcp.types.CallToolResult
    TextContent = mcp.types.TextContent

from core.protocols import IConfig, IContextManager
from utils.exceptions import ConfigError

logger = logging.getLogger("grid.mcp_manager")


class DictObj(dict):
    """
    Helper class that behaves like a dictionary but also supports attribute access.
    Mocking Pydantic model behavior for Agents SDK compatibility.
    """
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            # Must raise AttributeError for missing attributes so that hasattr() works correctly
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
            
    def __setattr__(self, name, value):
        self[name] = value

    def model_dump(self, **kwargs):
        """Mock Pydantic v2 model_dump method."""
        return dict(self)
        
    def dict(self, **kwargs):
        """Mock Pydantic v1 dict method."""
        return dict(self)


class ProxyCallToolResult:
    """
    Proxy class to allow returning flexible content types (dicts) for Agents SDK.
    Helps to bypass strict type validation in Pydantic models when we need to 
    pass 'image_url' dicts compatible with OpenAI/Agents SDK.
    """
    def __init__(self, content: List[Any], isError: bool = False):
        self.content = content
        self.isError = isError


class ResilientMCPServerStdio(MCPServerStdio):
    """
    MCPServerStdio that catches exceptions during tool calls and returns them as error results.
    This prevents the agent execution from crashing due to tool timeouts or errors.
    Also handles multimodal content conversion.
    """
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] | None) -> Union[CallToolResult, ProxyCallToolResult]:
        try:
            result = await super().call_tool(tool_name, arguments)
            
            # Post-process result to convert MCP ImageContent to Agents SDK format
            # MCP returns: {"type": "image", "data": "base64...", "mimeType": "..."}
            # SDK needs: {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
            if hasattr(result, "content") and result.content:
                has_changes = False
                new_content = []
                
                for item in result.content:
                    # Check if item is MCP ImageContent (type="image")
                    # Can be object or dict
                    item_type = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
                    
                    if item_type == "image":
                        has_changes = True
                        # Extract data
                        if isinstance(item, dict):
                            data = item.get("data", "")
                            mime = item.get("mimeType", "image/png")
                        else:
                            data = getattr(item, "data", "")
                            mime = getattr(item, "mimeType", "image/png")
                        
                        # Create Data URI
                        data_uri = f"data:{mime};base64,{data}"
                        
                        # Convert to Agents SDK format (Chat Completions API style)
                        # {"type": "image_url", "image_url": {"url": "..."}}
                        # Use DictObj to support both dict access and attribute access (obj.type)
                        # And mock Pydantic methods
                        new_content.append(DictObj({
                            "type": "image_url", 
                            "image_url": {"url": data_uri}
                        }))
                    else:
                        new_content.append(item)
                
                if has_changes:
                    # Return proxy object to bypass strict validation if any
                    # This ensures the Runner receives the correct dict structure for images
                    logger.debug(f"Converted MCP images for tool {tool_name}")
                    return ProxyCallToolResult(content=new_content, isError=result.isError)
            
            return result
        except Exception as e:
            error_msg = f"Error invoking MCP tool {tool_name}: {e}"
            if "Timed out" in str(e):
                error_msg += "\n(The tool execution timed out. If the tool is waiting for input or is slow, try to run it again or check the system state.)"
            
            logger.error(error_msg, exc_info=True)
            
            return CallToolResult(
                content=[TextContent(type="text", text=error_msg)],
                isError=True
            )


class MCPManager:
    """
    Manages MCP (Model Context Protocol) servers.

    Handles the lifecycle of MCP servers including:
    - Server creation from configuration
    - Server connection
    - Server caching for reuse
    - Proper cleanup and disconnection
    """

    def __init__(
        self,
        config: IConfig,
        context_manager: Optional[IContextManager] = None,
    ) -> None:
        """
        Initialize MCPManager.

        Args:
            config: Configuration instance
            context_manager: Optional context manager for metadata tracking
        """
        self.config = config
        self.context_manager = context_manager

        # New MCP servers cache (SDK-based)
        self._mcp_servers: Dict[str, Any] = {}

    async def create_mcp_servers(self, mcp_tool_names: List[str]) -> List[Any]:
        """
        Create and connect MCP servers using the Agents SDK.

        Args:
            mcp_tool_names: List of MCP tool names from configuration

        Returns:
            List of connected MCP server instances

        Example:
            >>> manager = MCPManager(config)
            >>> servers = await manager.create_mcp_servers(["filesystem", "git"])
            >>> print(f"Created {len(servers)} servers")
        """
        servers: List[Any] = []
        unavailable: List[str] = []

        logger.info(
            "Creating MCP servers",
            extra={"tool_names": mcp_tool_names, "count": len(mcp_tool_names)},
        )

        for name in mcp_tool_names:
            try:
                server = await self.get_mcp_server(name)
                if server is not None:
                    servers.append(server)
                    logger.debug(
                        "MCP server created successfully",
                        extra={"tool_name": name},
                    )
                else:
                    unavailable.append(name)
                    logger.warning(
                        "MCP server not available",
                        extra={"tool_name": name},
                    )
            except Exception as e:
                unavailable.append(name)
                logger.error(
                    "Failed to create MCP server",
                    extra={"tool_name": name, "error": str(e)},
                    exc_info=e,
                )

        # Store unavailable servers in metadata if context manager is available
        if unavailable and self.context_manager is not None:
            try:
                self.context_manager.set_metadata("mcp_unavailable", unavailable)
            except Exception as exc:
                logger.warning(
                    "Failed to store MCP availability metadata",
                    extra={"error": str(exc)},
                    exc_info=exc,
                )

        logger.info(
            "MCP servers creation complete",
            extra={
                "available": len(servers),
                "unavailable": len(unavailable),
                "unavailable_names": unavailable,
            },
        )

        return servers

    async def get_mcp_server(self, tool_name: str) -> Optional[Any]:
        """
        Get or create an SDK-based MCP server (MCPServerStdio).

        This method implements caching - if a server for the given tool
        already exists, it returns the cached instance.

        Args:
            tool_name: MCP tool name from configuration

        Returns:
            MCP server instance or None if tool is not MCP type

        Raises:
            ConfigError: If tool configuration is invalid
            Exception: If server creation or connection fails

        Example:
            >>> manager = MCPManager(config)
            >>> server = await manager.get_mcp_server("filesystem")
            >>> # Server is cached
            >>> same_server = await manager.get_mcp_server("filesystem")
            >>> assert server is same_server
        """
        # Return cached server if available
        if tool_name in self._mcp_servers:
            logger.debug(
                "Returning cached MCP server",
                extra={"tool_name": tool_name},
            )
            return self._mcp_servers[tool_name]

        # Get tool configuration
        tool_config = self.config.get_tool(tool_name)
        if tool_config.type != "mcp":
            logger.debug(
                "Tool is not MCP type",
                extra={"tool_name": tool_name, "tool_type": tool_config.type},
            )
            return None

        # Validate server command
        server_command = tool_config.server_command or []
        if not server_command:
            logger.warning(
                "MCP tool has no server_command",
                extra={"tool_name": tool_name},
            )
            return None

        # Extract command and arguments
        command = server_command[0]
        args = list(server_command[1:])

        # Make npx non-interactive by adding -y flag
        if command.lower() in ("npx", "npx.cmd") and "-y" not in args:
            args.insert(0, "-y")
            logger.debug(
                "Added -y flag to npx command",
                extra={"tool_name": tool_name},
            )

        # Get environment variables and working directory
        env = tool_config.env_vars or {}
        cwd = self.config.get_working_directory()

        # Add working directory to args if configured
        if getattr(tool_config, 'add_working_directory', False):
             args.append(cwd)
             logger.debug(
                "Added working directory to command arguments",
                extra={"tool_name": tool_name, "cwd": cwd},
            )

        logger.info(
            "Creating MCP server",
            extra={
                "tool_name": tool_name,
                "command": command,
                "args": args,
                "cwd": cwd,
                "env_vars": list(env.keys()),
            },
        )

        # Create server instance
        server = ResilientMCPServerStdio(
            params={
                "command": command,
                "args": args,
                "env": env,
                "cwd": cwd,
            },
            cache_tools_list=True,
            name=tool_name,
            client_session_timeout_seconds=60,  # 1 minute timeout
        )

        # Connect to server
        try:
            await server.connect()
            logger.info(
                "MCP server connected successfully",
                extra={"tool_name": tool_name},
            )
        except Exception as e:
            logger.error(
                "Failed to connect to MCP server",
                extra={"tool_name": tool_name, "error": str(e)},
                exc_info=e,
            )
            raise

        # Cache the server
        self._mcp_servers[tool_name] = server

        return server

    async def cleanup_servers(self) -> None:
        """
        Cleanup all MCP servers and release resources.

        This method properly disconnects from all MCP servers
        and should be called during application shutdown.

        Note:
            After cleanup, all servers are removed from the cache.
        """
        logger.info(
            "Cleaning up MCP servers",
            extra={"server_count": len(self._mcp_servers)},
        )

        for tool_name, mcp_server in list(self._mcp_servers.items()):
            try:
                # SDK MCP servers expose cleanup()
                cleanup_method = getattr(mcp_server, "cleanup", None)
                if cleanup_method is not None:
                    await cleanup_method()
                else:
                    # Back-compat for any legacy clients
                    await mcp_server.disconnect()

                logger.debug(
                    "MCP server cleaned up",
                    extra={"tool_name": tool_name},
                )

            except asyncio.CancelledError:
                logger.debug(
                    "MCP cleanup cancelled",
                    extra={"tool_name": tool_name},
                    exc_info=True,
                )
            except Exception as e:
                logger.warning(
                    "Failed to cleanup MCP server",
                    extra={
                        "tool_name": tool_name,
                        "server_name": getattr(mcp_server, "name", "unknown"),
                        "error": str(e),
                    },
                    exc_info=e,
                )

        self._mcp_servers.clear()
        logger.info("All MCP servers cleaned up")

    def clear_servers(self) -> None:
        """
        Clear all MCP servers from cache without cleanup.

        Warning:
            This does not disconnect from servers or release resources.
            Use this only when cleanup_servers() cannot be called.
            Prefer cleanup_servers() for proper resource management.
        """
        server_count = len(self._mcp_servers)
        self._mcp_servers.clear()
        logger.debug(
            "MCP servers cleared from cache",
            extra={"server_count": server_count},
        )

    def get_server_count(self) -> int:
        """
        Get the number of active MCP servers.

        Returns:
            Number of servers in cache
        """
        return len(self._mcp_servers)

    def has_server(self, tool_name: str) -> bool:
        """
        Check if a server exists for the given tool.

        Args:
            tool_name: MCP tool name

        Returns:
            True if server exists in cache
        """
        return tool_name in self._mcp_servers
