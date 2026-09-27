"""The MCP server client Grid runs MCP tools through.

ResilientMCPServerStdio is the SDK's stdio MCP server with Grid's output
limit and error handling; core.factory.mcp starts and keeps the servers.
"""

import logging
from typing import Dict, List, Optional, Any, Union
from agents.mcp import MCPServerStdio
try:
    from mcp.types import CallToolResult, TextContent
except ImportError:
    import mcp.types
    CallToolResult = mcp.types.CallToolResult
    TextContent = mcp.types.TextContent


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

    def model_dump_json(self, **kwargs):
        """Mock Pydantic v2 model_dump_json method."""
        import json
        return json.dumps(dict(self))

    def dict(self, **kwargs):
        """Mock Pydantic v1 dict method."""
        return dict(self)

    def json(self, **kwargs):
        """Mock Pydantic v1 json method."""
        import json
        return json.dumps(dict(self))


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
    Also handles multimodal content conversion and output token limits.
    """

    def __init__(self, *args, max_output_tokens: Optional[int] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._max_output_tokens = max_output_tokens

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    def _check_output_size(self, tool_name: str, result: Any) -> Optional["CallToolResult"]:
        """Checks the output size of an MCP tool. Returns an error if the token limit is exceeded."""
        if self._max_output_tokens is None:
            return None
        if not hasattr(result, "content") or not result.content:
            return None

        total_text = ""
        for item in result.content:
            item_type = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
            if item_type == "text":
                text = getattr(item, "text", None) or (item.get("text") if isinstance(item, dict) else "")
                total_text += text or ""

        if not total_text:
            return None

        estimated = self._estimate_tokens(total_text)
        if estimated > self._max_output_tokens:
            logger.warning(
                "MCP tool output rejected (token limit): %s (~%d tokens > %d limit)",
                tool_name, estimated, self._max_output_tokens,
            )
            error_msg = (
                f"ERROR: Tool output is too large (~{estimated} tokens, limit {self._max_output_tokens} tokens). "
                f"The result of '{tool_name}' was not passed to avoid context overflow. "
                f"Use more specific parameters or split the request into smaller parts."
            )
            return CallToolResult(
                content=[TextContent(type="text", text=error_msg)],
                isError=True,
            )
        return None

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any] | None) -> Union[CallToolResult, ProxyCallToolResult]:
        try:
            result = await super().call_tool(tool_name, arguments)

            # Check output token limit before any further processing
            size_error = self._check_output_size(tool_name, result)
            if size_error is not None:
                return size_error

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
                        # {"type": "image_url", "image_url": {"url": "...", "detail": "high"}}
                        # detail="high" for better vision analysis (same as tools/vision_tools.py)
                        new_content.append(DictObj({
                            "type": "image_url",
                            "image_url": {"url": data_uri, "detail": "high"},
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
