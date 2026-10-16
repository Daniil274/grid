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

They do not share ``asyncio.to_thread``'s default executor: the server's own
work goes there too (checkpoints, spaces, flushes), and one user's long
commands would take every thread of it - the whole server waiting on them.
Tools run in an executor of their own, and each user - a run's container, or
its workspace without one - holds at most :data:`SLOTS_PER_USER` of its
threads at a time: other users' tools are not queued behind them.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import os
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

import agents
import agents.tool

#: Set on each FunctionTool made through the patched decorator.
OFF_LOOP = "_grid_off_loop"

_sdk_function_tool = agents.tool.function_tool


def _setting(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


#: Threads all tools of the server share (GRID_TOOL_THREADS).
THREADS = _setting("GRID_TOOL_THREADS", 32)
#: Tools of one user running at once (GRID_TOOL_SLOTS_PER_USER); the rest wait.
SLOTS_PER_USER = _setting("GRID_TOOL_SLOTS_PER_USER", 4)

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
# Per event loop: a semaphore belongs to the loop it was first awaited on.
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)


def _tool_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="grid-tool")
        return _executor


def _user() -> str:
    """Whose tool this is: the run's container, else its workspace."""
    from utils.path_utils import get_current_factory

    factory = get_current_factory()
    if factory is None:
        return ""
    container = getattr(factory, "container_id", None)
    if container:
        return f"container:{container}"
    try:
        return f"workspace:{factory.config.get_working_directory()}"
    except AttributeError:
        return ""


def _slot(loop: asyncio.AbstractEventLoop, user: str) -> asyncio.Semaphore:
    slots = _slots.setdefault(loop, {})
    slot = slots.get(user)
    if slot is None:
        slot = slots[user] = asyncio.Semaphore(SLOTS_PER_USER)
    return slot


async def run_in_tool_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """``func(*args, **kwargs)`` in a tool thread, once its user has a free slot.

    Context variables go along, as with ``asyncio.to_thread``.

    A worker thread cannot be stopped, so the user's slot is held until the
    worker itself finishes - not until whoever awaited it stops waiting. A
    cancelled caller must not hand out capacity that is still in use."""
    loop = asyncio.get_running_loop()
    slot = _slot(loop, _user())
    await slot.acquire()
    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)
    try:
        future = loop.run_in_executor(_tool_executor(), call)
    except BaseException:
        slot.release()
        raise
    # ``shield`` below keeps *future* alive when the await is cancelled, and a
    # done callback runs on the loop when the thread truly ends; the semaphore
    # is only ever touched from the loop's own thread.
    future.add_done_callback(lambda _done: slot.release())
    return await asyncio.shield(future)


def _off_loop(func: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(func):
        return func

    # functools.wraps keeps what the SDK reads to build the schema: the
    # signature and type hints (through __wrapped__), the name and docstring.
    @functools.wraps(func)
    async def run_in_thread(*args: Any, **kwargs: Any) -> Any:
        return await run_in_tool_thread(func, *args, **kwargs)

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
