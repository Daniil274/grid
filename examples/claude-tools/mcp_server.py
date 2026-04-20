#!/usr/bin/env python3
"""
MCP server exposing claude-tools FunctionTools via stdio.

Wraps all @function_tool objects from the tools/ package and serves them
as MCP tools, maintaining full compatibility with the AgentsSDK FunctionTool API.
"""

import asyncio
import importlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

# ── ensure claude-tools and grid root are importable ────────────────────────
_HERE = Path(__file__).parent          # .../examples/claude-tools
_GRID_ROOT = _HERE.parent.parent       # .../grid

for _p in [str(_HERE), str(_GRID_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import Usage
from agents.tool import ToolContext
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)
logger = logging.getLogger("claude-tools-mcp")

mcp = FastMCP("claude-tools")

# ── tool module registry ─────────────────────────────────────────────────────
# Loaded by file path to avoid shadowing conflicts with the grid `tools` package.
_TOOL_MODULE_FILES = [
    _HERE / "tools" / "bash_tool.py",
    _HERE / "tools" / "file_tools.py",
    _HERE / "tools" / "search_tools.py",
    _HERE / "tools" / "web_tools.py",
    _HERE / "tools" / "notebook_tool.py",
    _HERE / "tools" / "todo_tool.py",
]

# Set to None to expose all tools, or a frozenset of tool names to whitelist.
_ENABLED_TOOLS: frozenset[str] | None = frozenset({
    "file_read",
    "file_write",
    "file_edit",
    "grep_tool",
})


def _is_function_tool(obj: Any) -> bool:
    """Return True if obj is a @function_tool FunctionTool instance."""
    return (
        hasattr(obj, "name")
        and hasattr(obj, "params_json_schema")
        and hasattr(obj, "on_invoke_tool")
        and callable(obj.on_invoke_tool)
    )


def _make_wrapper(ft: Any):
    """
    Build an async wrapper function that the MCP server can call.
    Accepts **kwargs (the tool arguments) and forwards them to FunctionTool.on_invoke_tool.
    """
    ft_ref = ft  # capture in closure

    async def wrapper(**kwargs: Any) -> str:
        ctx = ToolContext(
            context=None,
            usage=Usage(),
            tool_name=ft_ref.name,
            tool_call_id="mcp-call",
            tool_arguments=json.dumps(kwargs),
        )
        result = await ft_ref.on_invoke_tool(ctx, json.dumps(kwargs))
        return str(result) if result is not None else ""

    # Give the wrapper the tool's name so FastMCP picks it up correctly
    wrapper.__name__ = ft.name
    wrapper.__qualname__ = ft.name
    # FastMCP reads __doc__ for the description
    wrapper.__doc__ = ft.description or ft.name
    return wrapper


def _build_typed_wrapper(ft: Any):
    """
    Dynamically create a wrapper with an explicit signature matching the tool's
    JSON schema so FastMCP can generate accurate MCP tool definitions.
    """
    schema = ft.params_json_schema or {}
    properties: dict = schema.get("properties", {})
    required: list = schema.get("required", [])

    # Build function source with typed parameters
    params_parts = []
    annotations: dict = {"return": str}

    for param_name, param_schema in properties.items():
        param_type = param_schema.get("type", "string")
        has_default = param_name not in required

        # Map JSON schema types to Python type annotations
        py_type: type
        if param_type == "integer":
            py_type = int
        elif param_type == "boolean":
            py_type = bool
        elif param_type == "number":
            py_type = float
        elif param_type == "array":
            py_type = list
        elif param_type == "object":
            py_type = dict
        else:
            py_type = str

        annotations[param_name] = py_type

        default = param_schema.get("default")
        if has_default:
            if isinstance(default, str):
                params_parts.append(f'{param_name}: _ann["{param_name}"] = {default!r}')
            else:
                params_parts.append(f'{param_name}: _ann["{param_name}"] = {default!r}')
        else:
            params_parts.append(f'{param_name}: _ann["{param_name}"]')

    params_str = ", ".join(params_parts)

    # Build docstring with param descriptions
    doc_lines = [ft.description or ft.name, ""]
    for param_name, param_schema in properties.items():
        desc = param_schema.get("description", "")
        if desc:
            doc_lines.append(f"    {param_name}: {desc}")
    docstring = "\n".join(doc_lines)

    fn_src = (
        f'async def {ft.name}({params_str}) -> str:\n'
        f'    """{docstring}"""\n'
        f'    kwargs = {{{", ".join(f"{repr(p)}: {p}" for p in properties)}}}\n'
        f'    return await _invoke(ft_ref, kwargs)\n'
    )

    ft_ref = ft

    async def _invoke(ft_inner: Any, kwargs: dict) -> str:
        ctx = ToolContext(
            context=None,
            usage=Usage(),
            tool_name=ft_inner.name,
            tool_call_id="mcp-call",
            tool_arguments=json.dumps(kwargs),
        )
        result = await ft_inner.on_invoke_tool(ctx, json.dumps(kwargs))
        return str(result) if result is not None else ""

    globs = {"_ann": annotations, "ft_ref": ft_ref, "_invoke": _invoke}
    exec(fn_src, globs)  # noqa: S102
    fn = globs[ft.name]
    fn.__annotations__ = {k: v for k, v in annotations.items()}
    return fn


def register_tools() -> int:
    """Load all tool modules and register each FunctionTool with the MCP server."""
    import importlib.util

    count = 0
    for module_file in _TOOL_MODULE_FILES:
        mod_name = f"claude_tools.{module_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, module_file)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        except Exception as exc:
            logger.warning("Skipping module %s: %s", module_file.name, exc)
            continue

        seen: set = set()
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name, None)
            if not _is_function_tool(obj):
                continue
            if obj.name in seen:
                continue
            seen.add(obj.name)
            if _ENABLED_TOOLS is not None and obj.name not in _ENABLED_TOOLS:
                continue
            try:
                wrapper = _build_typed_wrapper(obj)
                mcp.add_tool(wrapper, name=obj.name, description=obj.description or obj.name, structured_output=False)
                count += 1
                logger.debug("Registered tool: %s", obj.name)
            except Exception as exc:
                logger.warning("Failed to register %s: %s", obj.name, exc)

    return count


def _fix_logging() -> None:
    """
    Tool modules (via utils.logger.Logger) reset root logger to INFO and add a stdout
    StreamHandler. Before starting the MCP stdio transport we must:
      1. Remove every stdout handler from root (it would corrupt the JSON-RPC stream).
      2. Raise root logger level back to WARNING so INFO noise is suppressed.
    """
    root = logging.getLogger()
    to_remove = [
        h for h in list(root.handlers)
        if isinstance(h, logging.StreamHandler)
        and getattr(getattr(h, "stream", None), "name", None) == "<stdout>"
    ]
    for h in to_remove:
        root.removeHandler(h)
    # Re-apply WARNING threshold (tool modules may have lowered it to INFO).
    root.setLevel(logging.WARNING)


if __name__ == "__main__":
    n = register_tools()
    _fix_logging()
    logger.warning("claude-tools MCP server: %d tools registered", n)
    mcp.run(transport="stdio")
