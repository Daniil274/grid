"""
Model Context Protocol (MCP) client for Grid system.
"""

import asyncio
import json
import os
from typing import List, Dict, Any, Optional

from utils.logger import Logger
from utils.exceptions import MCPError

logger = Logger(__name__)


class MCPClient:
    """
    MCP client for connecting to external MCP servers.
    
    Provides integration with Model Context Protocol servers for extended
    capabilities like filesystem access, git operations, web search, etc.
    """
    
    def __init__(self, name: str, server_command: List[str], env_vars: Optional[Dict[str, str]] = None):
        """
        Initialize MCP client.
        
        Args:
            name: Client name for identification
            server_command: Command to start MCP server
            env_vars: Environment variables for server
        """
        self.name = name
        self.server_command = server_command
        self.env_vars = env_vars or {}
        
        self._process: Optional[asyncio.subprocess.Process] = None
        self._connected = False
        self._tools: List[Any] = []
        
        logger.debug(f"MCP client '{name}' initialized", command=server_command)
    
    async def connect(self) -> None:
        """
        Connect to MCP server.
        
        Raises:
            MCPError: If connection fails
        """
        try:
            if self._connected:
                logger.warning(f"MCP client '{self.name}' already connected")
                return
            
            logger.info(f"Connecting to MCP server '{self.name}'")
            
            # Prepare environment
            env = dict(os.environ)
            env.update(self.env_vars)
            
            # Start server process
            self._process = await asyncio.create_subprocess_exec(
                *self.server_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            
            # Initialize MCP protocol
            await self._initialize_mcp()
            
            # List available tools
            await self._list_tools()
            
            self._connected = True
            logger.info(f"MCP server '{self.name}' connected successfully", tools_count=len(self._tools))
            
        except Exception as e:
            logger.error(f"Failed to connect to MCP server '{self.name}'", error=str(e))
            raise MCPError(f"MCP connection failed: {e}") from e
    
    async def disconnect(self) -> None:
        """Disconnect from MCP server."""
        try:
            if not self._connected:
                return
            
            logger.info(f"Disconnecting MCP server '{self.name}'")
            
            if self._process:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"MCP server '{self.name}' did not terminate gracefully, killing")
                    self._process.kill()
                    await self._process.wait()
            
            self._connected = False
            self._tools.clear()
            
            logger.info(f"MCP server '{self.name}' disconnected")
            
        except Exception as e:
            logger.error(f"Error disconnecting MCP server '{self.name}'", error=str(e))
    
    async def get_tools(self) -> List[Any]:
        """
        Get available tools from MCP server.
        
        Returns:
            List of tool objects
        """
        if not self._connected:
            raise MCPError(f"MCP client '{self.name}' not connected")
        
        return self._tools.copy()
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """
        Call MCP tool.
        
        Args:
            tool_name: Name of tool to call
            arguments: Tool arguments
            
        Returns:
            Tool result
        """
        if not self._connected:
            raise MCPError(f"MCP client '{self.name}' not connected")
        
        try:
            logger.debug(f"Calling MCP tool '{tool_name}'", arguments=arguments)
            
            # Send tool call request
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments
                }
            }
            
            await self._send_request(request)
            response = await self._receive_response()
            
            if "error" in response:
                raise MCPError(f"MCP tool call failed: {response['error']}")
            
            result = response.get("result", {})
            logger.debug(f"MCP tool '{tool_name}' completed", result_type=type(result).__name__)
            
            return result
            
        except Exception as e:
            logger.error(f"MCP tool call failed", tool_name=tool_name, error=str(e))
            raise MCPError(f"MCP tool call failed: {e}") from e
    
    async def _initialize_mcp(self) -> None:
        """Initialize MCP protocol handshake."""
        # Send initialize request
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "clientInfo": {
                    "name": "grid-mcp-client",
                    "version": "0.1.0"
                }
            }
        }
        
        await self._send_request(init_request)
        response = await self._receive_response()
        
        if "error" in response:
            raise MCPError(f"MCP initialization failed: {response['error']}")
        
        # Send initialized notification
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }
        
        await self._send_request(initialized_notification)
    
    async def _list_tools(self) -> None:
        """List available tools from MCP server."""
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list"
        }
        
        await self._send_request(request)
        response = await self._receive_response()
        
        if "error" in response:
            raise MCPError(f"Failed to list MCP tools: {response['error']}")
        
        tools_data = response.get("result", {}).get("tools", [])
        
        # Create tool wrappers
        self._tools = []
        for tool_data in tools_data:
            tool_wrapper = self._create_tool_wrapper(tool_data)
            self._tools.append(tool_wrapper)
        
        logger.debug(f"Listed {len(self._tools)} tools from MCP server '{self.name}'")
    
    def _create_tool_wrapper(self, tool_data: Dict[str, Any]) -> Any:
        """
        Create tool wrapper for MCP tool.
        
        This is a simplified implementation - in practice would need to
        create proper tool objects compatible with the agents framework.
        """
        tool_name = tool_data.get("name", "unknown")
        tool_description = tool_data.get("description", "")
        
        async def tool_function(**kwargs):
            return await self.call_tool(tool_name, kwargs)
        
        # Add metadata to function
        tool_function.__name__ = tool_name
        tool_function.__doc__ = tool_description
        
        return tool_function
    
    async def _send_request(self, request: Dict[str, Any]) -> None:
        """Send JSON-RPC request to MCP server."""
        if not self._process or not self._process.stdin:
            raise MCPError("MCP server process not available")
        
        request_json = json.dumps(request) + "\\n"
        self._process.stdin.write(request_json.encode())
        await self._process.stdin.drain()
    
    async def _receive_response(self) -> Dict[str, Any]:
        """Receive JSON-RPC response from MCP server."""
        if not self._process or not self._process.stdout:
            raise MCPError("MCP server process not available")
        
        line = await self._process.stdout.readline()
        if not line:
            raise MCPError("MCP server closed connection")
        
        try:
            response = json.loads(line.decode().strip())
            return response
        except json.JSONDecodeError as e:
            raise MCPError(f"Invalid JSON response from MCP server: {e}")
    
    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected
    
    def __repr__(self) -> str:
        """String representation of MCP client."""
        status = "connected" if self._connected else "disconnected"
        return f"MCPClient(name='{self.name}', status='{status}', tools={len(self._tools)})"