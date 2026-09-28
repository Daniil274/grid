"""Function tools written as plain functions run in a worker thread.

The Agents SDK calls a synchronous tool function on the event loop itself, so
a tool that waits - a shell command, a download, a walk over files - stops the
whole server for that long: every user's chat, socket and turn. ``install``
makes ``agents.function_tool`` wrap such a function in a coroutine that runs
it with ``asyncio.to_thread``; context variables go along (the run's factory,
the session log). Tools written as coroutines are left as they are.

A module binds ``function_tool`` when it imports it, so ``install`` runs
before any tool module is imported: ``tools/__init__`` calls it. Every tool
made through it carries :data:`OFF_LOOP`; the tests check that every shipped
tool does.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any, Callable, Optional

import agents
import agents.tool

#: Set on each FunctionTool made through the patched decorator.
OFF_LOOP = "_grid_off_loop"

_sdk_function_tool = agents.tool.function_tool


def _off_loop(func: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(func):
        return func

    # functools.wraps keeps what the SDK reads to build the schema: the
    # signature and type hints (through __wrapped__), the name and docstring.
    @functools.wraps(func)
    async def run_in_thread(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(func, *args, **kwargs)

    return run_in_thread


def function_tool(func: Optional[Callable[..., Any]] = None, **options: Any) -> Any:
    """``agents.function_tool``, with a plain function run in a worker thread."""

    def make(target: Callable[..., Any]) -> Any:
        tool = _sdk_function_tool(_off_loop(target), **options)
        setattr(tool, OFF_LOOP, True)
        return tool

    return make if func is None else make(func)


def install() -> None:
    """Make ``agents.function_tool`` the decorator above; safe to call again."""
    agents.function_tool = function_tool
    agents.tool.function_tool = function_tool
