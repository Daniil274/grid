"""Project tools: list, read and idempotently create the web chat user's projects.

The store is the one the user's space hands the factory (``factory.project_store``).
The model never names a user, a space or a path: whose projects these are is the
host's decision. Creation is ``project_ensure`` - a retry or a race returns the
existing project - and notes are never replaced silently. Nothing here deletes a
project, starts a chat, a task or any background work.

Work list-first: ``project_list``, then ``project_ensure`` only for what is missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from agents import RunContextWrapper, function_tool

from utils import tool_effects as effects
from utils.path_utils import get_current_factory
from utils.tool_isolation import WORKSPACE
from web_chat.projects import ProjectError, ProjectNotFound, ProjectUnavailable


logger = logging.getLogger("grid.tools.project_tools")


def _failure(code: str, message: str) -> str:
    return json.dumps({"error": message, "code": code, "isError": True}, ensure_ascii=False)


def _store(context: Any):
    factory = getattr(getattr(context, "context", None), "factory", None) or get_current_factory()
    store = getattr(factory, "project_store", None)
    if store is None:
        # A standalone factory (grid_mcp, agent_chat, evals) has no user context:
        # refuse, never guess whose projects these are.
        raise ProjectUnavailable("Projects are not available in this session: no user project store")
    return store


def _brief(project: Any) -> Dict[str, Any]:
    return {
        "id": project.id,
        "title": project.title,
        "parent_id": project.parent_id,
        "description": project.description,
        "archived": project.archived,
        "has_notes": bool(project.notes.strip()),
    }


def _run(context: Any, operation) -> str:
    try:
        return json.dumps(operation(_store(context)), ensure_ascii=False)
    except ProjectError as exc:
        return _failure(getattr(exc, "code", "invalid"), str(exc))
    except Exception as exc:  # noqa: BLE001 - never leak paths or tracebacks
        logger.error("Project tool failed: %s", type(exc).__name__)
        return _failure("unavailable", "Project operation unavailable")


@function_tool
async def project_list(context: RunContextWrapper) -> str:
    """List the user's projects (id, title, parent_id, description). Call this before creating any."""
    return await asyncio.to_thread(_run, context, lambda s: {"projects": [_brief(p) for p in s.list()]})


@function_tool
async def project_get(context: RunContextWrapper, project_id: str) -> str:
    """Read one project including its shared notes."""

    def read(store):
        project = store.get(project_id)
        if project is None:
            raise ProjectNotFound("Project not found")
        return {"project": {**_brief(project), "notes": project.notes}}

    return await asyncio.to_thread(_run, context, read)


@function_tool
async def project_ensure(
    context: RunContextWrapper, title: str, parent_id: str = "", description: str = ""
) -> str:
    """Create a project only if none with that title exists under the same parent.

    Idempotent: a repeat returns the existing project (created=false) and never
    overwrites its description. Check project_list first and create only what is needed.
    """

    def ensure(store):
        project, created = store.ensure(title, parent_id=parent_id or None, description=description)
        if created:
            status = "created"
        elif project.archived:
            status = "archived"
        else:
            status = "exists"
        out = {"project": _brief(project), "created": created, "status": status}
        if status == "archived":
            out["hint"] = (
                "A project with this title exists but is archived; nothing was created or changed. "
                "Ask the user whether to restore it in the UI, or use a different title."
            )
        return out

    return await asyncio.to_thread(_run, context, ensure)


@function_tool
async def project_add_notes(
    context: RunContextWrapper, project_id: str, text: str, mode: str = "if_empty"
) -> str:
    """Add shared context (notes) to a project, never replacing existing notes silently.

    mode "if_empty": write only if the notes are empty (identical text is a no-op;
    different notes give a conflict error). mode "append": add after existing notes
    unless a whole block equal to the text is already there.
    """

    def add(store):
        project, changed = store.add_notes(project_id, text, mode)
        return {"project": _brief(project), "changed": changed}

    return await asyncio.to_thread(_run, context, add)


PROJECT_TOOLS = {
    "project_list": project_list,
    "project_get": project_get,
    "project_ensure": project_ensure,
    "project_add_notes": project_add_notes,
}
TOOL_ISOLATION = {name: WORKSPACE for name in PROJECT_TOOLS}
TOOL_EFFECTS = {
    "project_list": effects.read(),
    "project_get": effects.read(untrusted=True),
    "project_ensure": effects.write(),
    "project_add_notes": effects.write(),
}
