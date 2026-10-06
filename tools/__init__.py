"""
Tools and utilities for Grid agents.

Public API: get_tools_by_names, get_all_tools, AVAILABLE_TOOLS.
Individual tool functions are loaded lazily — only when requested by name.
"""

# Before any tool module binds function_tool: plain-function tools then run in
# a worker thread, not on the server's event loop (core.tool_threads).
from core import tool_threads

tool_threads.install()

from .function_tools import (  # noqa: E402 - after install()
    AVAILABLE_TOOLS,
    get_all_tools,
    get_tools_by_names,
    resolve_tool,
    tool_effect,
    tool_isolation,
)

__all__ = [
    "get_tools_by_names",
    "resolve_tool",
    "tool_effect",
    "tool_isolation",
    "get_all_tools",
    "AVAILABLE_TOOLS",
]
