"""Lazy tools: see and manage the user's lazy tasks from a chat.

Lazy tasks are the work the host runs in the background on a small share of the
user's subscription, between their own turns (web_chat.lazy). These tools let an
agent set one up, read how it goes and change it, without the user opening the
page:

    lazy_catalog   what a task is made of here: agents, sizes, limits
    lazy_list      the tasks, compactly (no steps, no plan)
    lazy_get       one task: its steps, its history, where it lands
    lazy_preview   where a task like this would land - nothing is saved
    lazy_create    make it; ``request_id`` makes a retry return the same task
    lazy_update    change it, or its whole series with ``series=true``
    lazy_cancel    stop it, or its whole series with ``series=true``

Whose tasks these are is the host's decision: the client is the one the user's
own space put on the factory (``factory.lazy_agents``), so the model never names
a user, a path or a tenant. A factory without a user - grid_mcp, agent_chat,
evals - has none, and every tool answers ``unavailable`` rather than guess.

Nothing here changes the pool's allowances: a task's own settings never touch the
shares of its subscription. The planner and the runner are the service's; these
tools never compute a plan or run a step themselves. But saving is the whole
trigger: like the page's API, ``lazy_create``, ``lazy_update`` and ``lazy_cancel``
only save and wake the service's loop (web_chat.lazy.service.LazyAgents.wake),
and that loop may start the first due step at once - a real model call in the
background, not a preview. A new task is planned as early as the budget allows
(core.lazy.planner), so an active task created outside the user's hours with
room is normally picked up within one loop tick. To hold work, create it and
pause it (``lazy_create`` then ``lazy_update(status="paused")``), or create it
with a deadline in the future; ``lazy_preview`` is the only call that saves
nothing and starts nothing.

Work list-first: ``lazy_catalog``, ``lazy_list``, ``lazy_get``, then change.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from agents import RunContextWrapper, function_tool

from utils import tool_effects as effects
from utils.path_utils import get_current_factory
from utils.tool_isolation import WORKSPACE
from web_chat.lazy.chat import LazyChat, LazyUnavailable
from web_chat.lazy.service import LazyError
from web_chat.projects import ProjectError


logger = logging.getLogger("grid.tools.lazy_tools")


def _failure(code: str, message: str) -> str:
    return json.dumps({"error": message, "code": code, "isError": True}, ensure_ascii=False)


def _client(context: Any) -> LazyChat:
    """The lazy tasks of the user this turn acts for."""
    factory = getattr(getattr(context, "context", None), "factory", None) or get_current_factory()
    client = getattr(factory, "lazy_agents", None)
    if client is None:
        # A standalone factory (grid_mcp, agent_chat, evals) has no user context:
        # refuse, never guess whose tasks these are.
        raise LazyUnavailable("Lazy tasks are not available in this session: no user context")
    return client


def _run(context: Any, operation) -> str:
    try:
        return json.dumps(operation(_client(context)), ensure_ascii=False)
    except (ProjectError, KeyError) as exc:
        if isinstance(exc, ProjectError):
            return _failure(getattr(exc, "code", "invalid"), str(exc))
        return _failure("not_found", "No such lazy task")
    except LazyError as exc:
        return _failure(getattr(exc, "code", "invalid"), str(exc))
    except Exception as exc:  # noqa: BLE001 - never leak paths or tracebacks
        logger.error("Lazy tool failed: %s", type(exc).__name__)
        return _failure("unavailable", "Lazy operation unavailable")


@function_tool
async def lazy_catalog(context: RunContextWrapper) -> str:
    """What a lazy task is made of here: the agents and subscriptions, sizes, priorities and limits.

    Call this first: an agent that cannot spend a subscription cannot do lazy work.
    """
    return await asyncio.to_thread(_run, context, lambda client: client.catalog())


@function_tool
async def lazy_list(
    context: RunContextWrapper,
    include_finished: bool = False,
    project_id: str = "",
    limit: int = 20,
) -> str:
    """List the user's lazy tasks, newest first: title, state, steps done, when it lands.

    Only unfinished tasks unless ``include_finished``; only one project's when
    ``project_id`` is given. Read one with lazy_get before changing it.
    """
    return await asyncio.to_thread(
        _run,
        context,
        lambda client: client.list(include_finished=include_finished, project_id=project_id, limit=limit),
    )


@function_tool
async def lazy_get(context: RunContextWrapper, task_id: str) -> str:
    """One lazy task in full: its brief, its steps, the last events, where it lands.

    ``version`` in the answer is what to pass to lazy_update or lazy_cancel as
    ``expected_version``: a change is refused if the task moved since.
    """
    return await asyncio.to_thread(_run, context, lambda client: client.get(task_id))


@function_tool
async def lazy_preview(
    context: RunContextWrapper,
    title: str,
    brief: str,
    system: str,
    agent: str,
    size: str = "M",
    acceptance: str = "",
    optional: str = "",
    priority: str = "normal",
    deadline: str = "",
    repeat_hours: float = 0,
    cap: float = 0,
    project_id: str = "",
) -> str:
    """Where a task like this would land among the existing ones - nothing is saved.

    ``deadline`` is ISO-8601 in the user's zone (2026-11-10T18:00) or epoch
    seconds; ``repeat_hours`` 0 runs it once. Shows the steps it would be cut
    into and which other tasks it would push.
    """
    payload = _payload(
        title=title, brief=brief, system=system, agent=agent, size=size, acceptance=acceptance,
        optional=optional, priority=priority, deadline=deadline, repeat_hours=repeat_hours,
        cap=cap, project_id=project_id,
    )
    return await asyncio.to_thread(_run, context, lambda client: client.preview(payload))


@function_tool
async def lazy_create(
    context: RunContextWrapper,
    title: str,
    brief: str,
    system: str,
    agent: str,
    size: str = "M",
    acceptance: str = "",
    optional: str = "",
    priority: str = "normal",
    deadline: str = "",
    repeat_hours: float = 0,
    cap: float = 0,
    project_id: str = "",
    request_id: str = "",
) -> str:
    """Create a lazy task: the agent does the work in the background, between the user's turns.

    ``request_id`` is the caller's key for this one creation: repeating the call
    with it returns the task already made (created=false) instead of a second
    copy - use one per intended task. ``project_id`` must be one of the user's
    projects (project_list). A task spends the agent's subscription and never
    changes the user's shares.

    This call saves the task and does not wait for any of its steps, but it is
    not a preview: saving wakes the service's loop, which may start the first
    step within a tick if the plan places it now. Pause it right away
    (lazy_update with status="paused"), or preview it instead, when the user
    wants to look before work begins.
    """
    payload = _payload(
        title=title, brief=brief, system=system, agent=agent, size=size, acceptance=acceptance,
        optional=optional, priority=priority, deadline=deadline, repeat_hours=repeat_hours,
        cap=cap, project_id=project_id,
    )
    return await asyncio.to_thread(_run, context, lambda client: client.create(payload, request_id=request_id))


@function_tool
async def lazy_update(
    context: RunContextWrapper,
    task_id: str,
    expected_version: int = -1,
    series: bool = False,
    title: Optional[str] = None,
    brief: Optional[str] = None,
    acceptance: Optional[str] = None,
    priority: Optional[str] = None,
    size: Optional[str] = None,
    deadline: Optional[str] = None,
    repeat_hours: Optional[float] = None,
    cap: Optional[float] = None,
    run_now: Optional[bool] = None,
    status: Optional[str] = None,
    project_id: Optional[str] = None,
    drop_optional: Optional[bool] = None,
) -> str:
    """Change a lazy task; only the fields named are changed.

    ``series=true`` applies the change to the whole series of rounds (a task
    with ``repeat_hours``); ``task_id`` may then be the series id. ``status``
    "paused" holds it and "active" resumes it. Pass the ``version`` from
    lazy_get as ``expected_version`` so a change made meanwhile is not
    overwritten (the answer says ``stale``). Clear a deadline with
    ``deadline=""``, make it run once with ``repeat_hours=0``, detach a project
    with ``project_id=""``.

    A change is saved and wakes the service's loop: resuming a paused task,
    setting ``run_now=true`` or moving a deadline can make the next step start
    at once. Pausing does not run anything.
    """
    changes = _changes(
        title=title, brief=brief, acceptance=acceptance, priority=priority, size=size,
        deadline=deadline, repeat_hours=repeat_hours, cap=cap, run_now=run_now, status=status,
        project_id=project_id, drop_optional=drop_optional,
    )
    version = None if expected_version is None or int(expected_version) < 0 else int(expected_version)
    return await asyncio.to_thread(
        _run,
        context,
        lambda client: client.update(task_id, changes, expected_version=version, series=series),
    )


@function_tool
async def lazy_cancel(
    context: RunContextWrapper, task_id: str, series: bool = False, expected_version: int = -1
) -> str:
    """Cancel a lazy task for good; with ``series=true``, its whole series of rounds.

    A running step is asked to stop at a safe point. ``task_id`` may be the
    series id (``series_id`` in the answers) when ``series=true``.
    """
    version = None if expected_version is None or int(expected_version) < 0 else int(expected_version)
    return await asyncio.to_thread(
        _run, context, lambda client: client.cancel(task_id, expected_version=version, series=series)
    )


def _payload(**fields: Any) -> Dict[str, Any]:
    """Only the fields that were named: an empty one leaves the default."""
    return {key: value for key, value in fields.items() if value not in (None, "")}


def _changes(**fields: Any) -> Dict[str, Any]:
    """Only the fields the caller named (None means "leave it"), plus explicit clears."""
    return {key: value for key, value in fields.items() if value is not None}


LAZY_TOOLS = {
    "lazy_catalog": lazy_catalog,
    "lazy_list": lazy_list,
    "lazy_get": lazy_get,
    "lazy_preview": lazy_preview,
    "lazy_create": lazy_create,
    "lazy_update": lazy_update,
    "lazy_cancel": lazy_cancel,
}
TOOL_ISOLATION = {name: WORKSPACE for name in LAZY_TOOLS}
TOOL_EFFECTS = {
    "lazy_catalog": effects.read(),
    "lazy_list": effects.read(),
    "lazy_get": effects.read(untrusted=True),
    "lazy_preview": effects.read(),
    "lazy_create": effects.write(),
    "lazy_update": effects.write(),
    "lazy_cancel": effects.write(),
}
