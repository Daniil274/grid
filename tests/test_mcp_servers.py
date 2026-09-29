"""MCP servers outlive the task that asked for them (core.factory.mcp)."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from core.factory.mcp import McpServers

ECHO_SERVER = """
from mcp.server.fastmcp import FastMCP

server = FastMCP("echo")


@server.tool()
def echo(text: str) -> str:
    return text


server.run()
"""


def _servers(tmp_path):
    script = tmp_path / "echo_server.py"
    script.write_text(ECHO_SERVER, encoding="utf-8")
    tool = SimpleNamespace(
        type="mcp", server_command=[sys.executable, str(script)], env_vars={}, add_working_directory=False
    )
    config = SimpleNamespace(
        get_tool=lambda name: tool,
        get_working_directory=lambda: str(tmp_path),
        config=SimpleNamespace(settings=SimpleNamespace(max_tool_output_tokens=None)),
    )
    return McpServers(config, context_manager=None, container_id=None, container_workdir="/workspace")


@pytest.mark.asyncio
async def test_server_started_in_a_finished_task_still_answers(tmp_path):
    # A web request prepares the agent and ends; the turn runs in another task.
    servers = _servers(tmp_path)
    server = await asyncio.create_task(servers.get("echo"))
    try:
        tools = await asyncio.create_task(server.list_tools())
        assert [tool.name for tool in tools] == ["echo"]
        assert await servers.get("echo") is server
    finally:
        await servers.close()
    assert len(servers) == 0


@pytest.mark.asyncio
async def test_stopped_server_is_started_again(tmp_path):
    servers = _servers(tmp_path)
    first = await servers.get("echo")
    task, stop = servers._owners["echo"]
    stop.set()
    await task
    try:
        second = await servers.get("echo")
        assert second is not first
        assert [tool.name for tool in await second.list_tools()] == ["echo"]
    finally:
        await servers.close()
