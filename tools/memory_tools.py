"""Workspace-bound persistent memory, with no model-controlled tenant or database.

A chat's memory has levels (core.factory.memory): its own, its project's and the
user's. Recall and overview read them all and say whose each record is;
remember takes the level to store at; forget finds the record in whichever
level holds it. The levels a chat has come from the host, never from the model.
"""
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

from agents import RunContextWrapper, function_tool

from core.memory import MemoryLayer, MemoryManager, MemoryQuery
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
            # Memory is off in the config, yet the system hands the agent memory
            # tools: use the workspace's own store, the one the web panel reads.
            fallback = getattr(factory, "get_workspace_memory_manager", None)
            manager = fallback() if fallback is not None else None
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


def _factory(context):
    return getattr(getattr(context, "context", None), "factory", None) or get_current_factory()


def _levels(context) -> list:
    """(level, manager) of the run's chat, nearest first; [] outside a factory with levels."""
    factory = _factory(context)
    of_levels = getattr(factory, "memory_levels", None)
    if of_levels is None:
        return []
    return of_levels(getattr(getattr(context, "context", None), "context_id", None), force=True)


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
    factory = _factory(context)
    levels = _levels(context)
    if len(levels) > 1 and query.strip():
        # Every level of the chat, ranked together; each hit says whose it is.
        try:
            hits = await factory.memory_search_levels(
                levels, query, layers=(MemoryLayer.L1, MemoryLayer.L2, MemoryLayer.L3),
                limit=max(1, min(limit, 50)), min_similarity=-1.0)
            return json.dumps([{"record": record_to_dict(record), "level": level, "score": score}
                               for level, record, score in hits], ensure_ascii=False)
        except Exception:  # noqa: BLE001 - same contract as _invoke
            return json.dumps({"error": "Memory operation unavailable or invalid arguments"})
    search = getattr(factory, "memory_search", None)
    if search is not None and query.strip():
        # By meaning (embeddings), every layer but raw transcripts, no similarity floor:
        # the agent asked, so it gets the closest records ranked, with their scores.
        try:
            with _manager(context) as manager:
                hits = await search(manager, query, layers=(MemoryLayer.L1, MemoryLayer.L2, MemoryLayer.L3),
                                    limit=max(1, min(limit, 50)), min_similarity=-1.0)
                return json.dumps([{"record": record_to_dict(record), "score": score} for record, score in hits],
                                  ensure_ascii=False)
        except Exception:  # noqa: BLE001 - same contract as _invoke
            return json.dumps({"error": "Memory operation unavailable or invalid arguments"})
    return await asyncio.to_thread(_invoke, context, lambda manager: [
        {"record": record_to_dict(hit.record), "score": hit.score}
        for hit in manager.search(MemoryQuery(text=query, limit=limit))
    ])


@function_tool
async def memory_remember(
    context: RunContextWrapper, content: str, layer: str = "L1",
    category: str = "note", scope: str = "project", source_ref: str | None = None,
    level: str = "",
) -> str:
    """Store a fact, scenario (L2), or profile preference (L3) in this workspace.

    level: whose memory it is - "chat" (this chat only), "project" (every chat of
    its project) or "user" (the user, in any project). Empty: the project's when
    the chat has one, else the user's; a profile preference (L3) is always the user's.
    """
    levels = dict(_levels(context))
    chosen = (level or "").strip().lower()
    if layer.upper() == "L3":
        chosen = "user"
    elif not chosen:
        chosen = "project" if "project" in levels else "user"
    if levels and chosen not in levels:
        return json.dumps({"error": f"This chat has no {chosen} memory; use one of: {', '.join(levels)}"})

    def store(manager):
        manager = levels.get(chosen, manager)
        if source_ref and (source_ref.startswith("file:") or ":" not in source_ref):
            root = Path(resolve_agent_path_from_ctx(".", context)).resolve()
            reference = source_ref.removeprefix("file:")
            if not (root / reference).resolve().is_relative_to(root):
                raise ValueError("Memory source escapes workspace")
        record = record_from_dict({"content": content, "layer": layer, "category": category,
                                   "scope": scope, "source_ref": source_ref})
        return {**record_to_dict(manager.remember(record)), "level": chosen if levels else "user"}
    return await asyncio.to_thread(_invoke, context, store)


@function_tool
async def memory_overview(context: RunContextWrapper, limit: int = 100) -> str:
    """List this workspace's current memories, bounded by limit (not a total count)."""
    levels = _levels(context)
    if len(levels) > 1:
        def overview(_manager):
            records = [
                {**record_to_dict(hit.record), "level": level}
                for level, manager in levels for hit in manager.search(MemoryQuery(limit=limit))
            ]
            return {"records": records[:limit], "limit": limit}
        return await asyncio.to_thread(_invoke, context, overview)
    return await asyncio.to_thread(_invoke, context, lambda manager: {
        "records": [record_to_dict(hit.record) for hit in manager.search(MemoryQuery(limit=limit))],
        "limit": limit,
    })


@function_tool
async def memory_forget(context: RunContextWrapper, record_id: str) -> str:
    """Delete a memory by id in this namespace; remote outages never confirm deletion."""
    levels = _levels(context)

    def forget(manager):
        for _level, holder in levels or [("user", manager)]:
            if holder.get(record_id) is not None:
                return {"forgotten": holder.forget(record_id)}
        return {"forgotten": False}
    return await asyncio.to_thread(_invoke, context, forget)


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
