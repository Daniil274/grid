"""Cross-system tools bound to the caller's registry (core.system_access).

A key agent of a user's registered systems gets these tools to see the user's
other systems and delegate tasks to them. They are registered in the shared
tools package (AVAILABLE_TOOLS) so a config may declare them by name and
tool_check accepts them, yet at call time they find the calling factory's
broker and delegate to it: what is callable stays exactly what that user's
registry offers, never a system or path named in a tool argument.
"""
from __future__ import annotations

import json
from typing import Any

from agents import RunContextWrapper, function_tool

from core.system_access import (
    SYSTEM_DELEGATE_DESCRIPTION,
    SYSTEMS_LIST_DESCRIPTION,
    SystemAccessBroker,
)
from utils import tool_effects as effects
from utils.path_utils import get_current_factory
from utils.tool_isolation import WORKSPACE


def _binding(context: Any) -> tuple[Any, SystemAccessBroker, str] | None:
    """The caller's factory, broker and system key, or None without a registry."""
    raw = getattr(context, "context", None)
    factory = getattr(raw, "factory", None) or get_current_factory()
    broker = getattr(factory, "_system_access_broker", None)
    source = getattr(factory, "_system_access_key", None)
    if broker is None or source is None:
        return None
    return factory, broker, source


@function_tool(description_override=SYSTEMS_LIST_DESCRIPTION)
async def systems_list(context: RunContextWrapper) -> str:
    binding = _binding(context)
    if binding is None:
        return json.dumps({"status": "blocked", "reason": "No cross-system registry is bound to this caller"})
    factory, broker, source = binding
    return broker.list_systems(factory, source, context)


@function_tool(description_override=SYSTEM_DELEGATE_DESCRIPTION)
async def system_delegate(context: RunContextWrapper, system: str, task: str) -> str:
    binding = _binding(context)
    if binding is None:
        return json.dumps({"status": "blocked", "reason": "No cross-system registry is bound to this caller"})
    factory, broker, source = binding
    return await broker.delegate_from_tool(factory, source, context, system, task)


SYSTEM_ACCESS_TOOLS = {
    "systems_list": systems_list,
    "system_delegate": system_delegate,
}
TOOL_ISOLATION = {name: WORKSPACE for name in SYSTEM_ACCESS_TOOLS}
TOOL_EFFECTS = {
    "systems_list": effects.read(),
    "system_delegate": effects.external(),
}
