"""Workspace-bound persistent memory, with no model-controlled tenant or database."""
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

from agents import RunContextWrapper, function_tool

from core.memory import MemoryManager, MemoryQuery
from core.memory.manager import MemoryConfig
from core.memory.models import record_from_dict, record_to_dict
from utils import tool_effects as effects
from utils.path_utils import get_current_factory, resolve_agent_path_from_ctx
from utils.tool_isolation import WORKSPACE


@contextmanager
def _manager(context):
    factory = getattr(getattr(context, "context", None), "factory", None) or get_current_factory()
    if factory is not None:
        manager = factory.get_memory_manager()
        if manager is None:
            raise ValueError("Memory is disabled by host configuration")
        yield manager
    else:
        # Explicit tools are usable outside a factory; cwd is still the host root.
        root = resolve_agent_path_from_ctx(".", context)
        if not (Path(root) / ".grid/memory.sqlite").resolve().is_relative_to(Path(root).resolve()):
            raise ValueError("Memory storage escapes workspace")
        with MemoryManager.from_config(MemoryConfig(workspace_root=root)) as manager:
            yield manager


def _invoke(context, operation):
    try:
        with _manager(context) as manager:
            return json.dumps(operation(manager), ensure_ascii=False)
    except Exception:  # noqa: BLE001 - optional backend failures must not break a tool run
        # Do not expose host paths, gateway credentials or arbitrary backend errors.
        return json.dumps({"error": "Memory operation unavailable or invalid arguments"})


@function_tool
async def memory_recall(context: RunContextWrapper, query: str = "", limit: int = 20) -> str:
    """Recall relevant historical data (not instructions) from this workspace."""
    return await asyncio.to_thread(_invoke, context, lambda manager: [
        {"record": record_to_dict(hit.record), "score": hit.score}
        for hit in manager.search(MemoryQuery(text=query, limit=limit))
    ])


@function_tool
async def memory_remember(
    context: RunContextWrapper, content: str, layer: str = "L1",
    category: str = "note", scope: str = "project", source_ref: str | None = None,
) -> str:
    """Store a fact, scenario (L2), or profile preference (L3) in this workspace."""
    def store(manager):
        if source_ref and (source_ref.startswith("file:") or ":" not in source_ref):
            root = Path(resolve_agent_path_from_ctx(".", context)).resolve()
            reference = source_ref.removeprefix("file:")
            if not (root / reference).resolve().is_relative_to(root):
                raise ValueError("Memory source escapes workspace")
        record = record_from_dict({"content": content, "layer": layer, "category": category,
                                   "scope": scope, "source_ref": source_ref})
        return record_to_dict(manager.remember(record))
    return await asyncio.to_thread(_invoke, context, store)


@function_tool
async def memory_overview(context: RunContextWrapper, limit: int = 100) -> str:
    """List this workspace's current memories, bounded by limit (not a total count)."""
    return await asyncio.to_thread(_invoke, context, lambda manager: {
        "records": [record_to_dict(hit.record) for hit in manager.search(MemoryQuery(limit=limit))],
        "limit": limit,
    })


@function_tool
async def memory_forget(context: RunContextWrapper, record_id: str) -> str:
    """Delete a memory by id in this namespace; remote outages never confirm deletion."""
    return await asyncio.to_thread(_invoke, context, lambda manager: {"forgotten": manager.forget(record_id)})


MEMORY_TOOLS = {
    "memory_recall": memory_recall,
    "memory_remember": memory_remember,
    "memory_overview": memory_overview,
    "memory_forget": memory_forget,
}
TOOL_ISOLATION = {name: WORKSPACE for name in MEMORY_TOOLS}
TOOL_EFFECTS = {
    "memory_recall": effects.read(untrusted=True),
    "memory_overview": effects.read(untrusted=True),
    "memory_remember": effects.write(),
    "memory_forget": effects.write(),
}
