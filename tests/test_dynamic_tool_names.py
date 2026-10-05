"""Tool names a model writes for a dynamic agent: config keys or MCP tool names."""

from types import SimpleNamespace

import pytest

from core.factory.tools import ToolAssembly
from utils.exceptions import ConfigError


class _Config:
    def __init__(self, tools):
        self.config = SimpleNamespace(tools=tools)

    def get_tool(self, key):
        if key not in self.config.tools:
            raise ConfigError(f"Tool '{key}' not found")
        return self.config.tools[key]


class _Assembly(ToolAssembly):
    def __init__(self, tools, resolvable):
        self.config = _Config(tools)
        self._resolvable = resolvable

    def _resolve_function_tools(self, keys):
        return [SimpleNamespace(name=key) for key in keys if key in self._resolvable]


@pytest.mark.asyncio
async def test_mcp_tool_names_attach_their_server_and_unknown_names_are_reported():
    assembly = _Assembly(
        {
            "file_read": SimpleNamespace(type="function"),
            "bash_tool": SimpleNamespace(type="function"),
            "codegraph": SimpleNamespace(type="mcp"),
        },
        resolvable={"file_read"},
    )

    tools, servers, missing = await assembly._resolve_tools_for_names(
        ["file_read", "codegraph_explore", "codegraph_search", "bash_tool", "grepp"]
    )

    assert [tool.name for tool in tools] == ["file_read"]
    assert servers == ["codegraph"]
    # bash_tool is configured but withheld here; grepp does not exist.
    assert missing == ["grepp", "bash_tool"]


def test_longest_server_key_wins():
    assembly = _Assembly(
        {
            "code": SimpleNamespace(type="mcp"),
            "codegraph": SimpleNamespace(type="mcp"),
        },
        resolvable=set(),
    )
    assert assembly._mcp_server_for_tool_name("codegraph_explore") == "codegraph"
    assert assembly._mcp_server_for_tool_name("code_search") == "code"
    assert assembly._mcp_server_for_tool_name("explore") is None
