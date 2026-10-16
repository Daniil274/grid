"""The memory API: a user browses and manages the agent's memory (L0-L3).

    GET    /api/memory                 overview, config, prompt-section preview and records
    POST   /api/memory/records         {content, layer, scope, category, title, tags, source_ref, file_path, confidence}
    PUT    /api/memory/records/{id}    the same fields, each optional (PATCH is the same route)
    DELETE /api/memory/records/{id}
    POST   /api/memory/sync            distil the conversation into memory

``GET`` takes ``context_id`` (the chat whose system and workspace are
meant), ``level``, ``layer``, ``scope``, ``category``, ``query``, ``stale_only``,
``include_stale`` and ``limit``; the writes take ``context_id`` in the body.

A chat's memory has levels (core.factory.memory): its own, its project's and
its user's. Every record says its ``level``; ``GET`` lists them all or one
(``level``), a new record is stored at the ``level`` it names, and an update
naming another level moves the record there.

Every route acts in the asking user's space (web_chat.space) and, with a
``context_id``, in the workspace and system that chat runs on. The memory is
the one the agents of that system use - the same file and host identity - so
one user never reaches another's records or a record of another workspace.
A system whose config leaves ``memory.enabled`` off still gets a local
SQLite memory in the workspace's own storage path, so the panel works before
the agents are told to use it; ``enabled_in_config`` says which case it is.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.memory import (
    MemoryCategory,
    MemoryConfig,
    MemoryLayer,
    MemoryManager,
    MemoryRecord,
    MemoryScope,
)
from core.memory.distill import exchanges
from core.memory.models import estimated_tokens
from web_chat.space import UserSpace

Layer = Literal["L0", "L1", "L2", "L3"]
Level = Literal["chat", "project", "user"]
Scope = Literal["project", "user", "system"]
Category = Literal["decision", "constraint", "architecture", "bug", "preference", "workflow", "note"]

#: Most messages one sync reads, newest last.
SYNC_MESSAGES = 400
#: Exchanges the model reads on "Extract from chat": one model call each.
SYNC_EXCHANGES = 8


class MemoryCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    layer: Layer = "L1"
    scope: Scope = "project"
    category: Category = "note"
    title: str = Field(default="", max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source_ref: str | None = Field(default=None, max_length=1000)
    file_path: str | None = Field(default=None, max_length=1000)
    confidence: float = Field(default=1.0, ge=0, le=1)
    context_id: str | None = Field(default=None, max_length=200)
    level: Level | None = None


class MemoryUpdateRequest(BaseModel):
    """Each field is optional; one left out stays as it is, and an empty
    ``source_ref`` or ``file_path`` clears it."""

    content: str | None = Field(default=None, min_length=1, max_length=20000)
    layer: Layer | None = None
    scope: Scope | None = None
    category: Category | None = None
    title: str | None = Field(default=None, max_length=300)
    tags: list[str] | None = Field(default=None, max_length=50)
    source_ref: str | None = Field(default=None, max_length=1000)
    file_path: str | None = Field(default=None, max_length=1000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    context_id: str | None = Field(default=None, max_length=200)
    level: Level | None = None


class MemorySyncRequest(BaseModel):
    context_id: str = Field(min_length=1, max_length=200)


class _Target:
    """The memory one request works on, and what its config says about it."""

    def __init__(self, manager: MemoryManager, settings: Any, enabled: bool,
                 factory: Any = None, agent_key: str | None = None,
                 levels: list[tuple[str, MemoryManager]] | None = None,
                 titles: dict[str, str] | None = None) -> None:
        self.manager = manager
        self.settings = settings
        self.enabled = enabled
        self.factory = factory      # the chat's agent factory: its model writes memories
        self.agent_key = agent_key
        #: The chat's memory levels, nearest first; the user's (``manager``) last.
        self.levels = levels or [("user", manager)]
        self.titles = titles or {"user": "the user, in any project"}

    def level(self, name: str | None) -> MemoryManager:
        """The memory of level *name* (the user's when None); 400 for one the chat lacks."""
        managers = dict(self.levels)
        if name is None:
            return self.manager
        if name not in managers:
            raise HTTPException(status_code=400, detail=f"This chat has no {name} memory")
        return managers[name]

    def holder(self, record_id: str) -> tuple[str, MemoryManager, MemoryRecord]:
        """The level holding record *record_id*; 404 when none does."""
        for name, manager in self.levels:
            record = manager.get(record_id)
            if record is not None:
                return name, manager, record
        raise HTTPException(status_code=404, detail="Memory record not found")


_local: weakref.WeakKeyDictionary[Any, dict[tuple, MemoryManager]] = weakref.WeakKeyDictionary()
_local_lock = threading.Lock()


def _local_manager(space: Any, config: Any, settings: Any, tenant: str) -> MemoryManager:
    """A SQLite memory in the workspace for a system that left memory off.

    It sits where the system's own memory would (``memory.storage_path``) and
    under the identity the agents would use, so enabling memory later shows the
    same records. A storage path leaving the workspace falls back to the default.
    """
    root = Path(config.get_working_directory()).resolve(strict=True)
    path = (root / settings.storage_path).resolve()
    if not path.is_relative_to(root):
        path = root / ".grid" / "memory.sqlite"
    key = (str(root), str(path), tenant)
    with _local_lock:
        managers = _local.setdefault(space, {})
        manager = managers.get(key)
        if manager is None:
            manager = managers[key] = MemoryManager.from_config(
                MemoryConfig(workspace_root=root, backend="sqlite", db_path=path, tenant_id=tenant)
            )
        return manager


def _resolve_sync(space: UserSpace, registry: Any, system: str, context_id: str | None,
                  agent_key: str | None = None) -> _Target:
    config = registry.config(system)
    settings = config.get_memory_config()
    factory = registry.factory(system)
    of_levels = getattr(factory, "memory_levels", None)
    levels = of_levels(context_id, force=True) if of_levels is not None else []
    titles = factory.memory_level_titles(context_id) if levels else None
    if settings.enabled:
        manager = factory.get_memory_manager()
        if manager is not None:
            return _Target(manager, settings, True, factory, agent_key, levels=levels, titles=titles)
    # Disabled in the config: still the factory's own workspace store (the one its
    # memory tools write to), so panel, tools and turn hook share file and namespace.
    shared = getattr(factory, "get_workspace_memory_manager", None)
    if shared is not None:
        manager = shared()
        if manager is not None:
            return _Target(manager, settings, False, levels=levels, titles=titles)
    tenant = getattr(factory, "_memory_tenant", None) or settings.tenant_id
    return _Target(_local_manager(space, config, settings, tenant), settings, False)


async def _target(space: UserSpace, context_id: str | None) -> _Target:
    """The memory of the system and workspace chat *context_id* runs on; the
    space's own default system without one. An unknown chat is a 404."""
    metadata: dict[str, Any] = {}
    if context_id:
        manager = space.context_manager()
        if manager.conversation_view(context_id) is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        metadata = manager.get_context_metadata(context_id)
    # A chat with a space of its own remembers in its user's space: no runtime of its own is built for this.
    of_memory = getattr(space, "memory_registry", None)
    registry = await (of_memory(context_id) if of_memory is not None else space.registry_for(context_id))
    system = metadata.get("routed_system")
    if system not in set(registry.keys()):
        system = registry.default_key()
    try:
        agent_key = metadata.get("routed_agent") or metadata.get("agent_key")
        return await asyncio.to_thread(_resolve_sync, space, registry, system, context_id, agent_key)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - a config the memory cannot be built from
        raise HTTPException(status_code=503, detail=f"Memory is unavailable: {exc}") from None


def serialize_record(record: MemoryRecord, level: str | None = None) -> dict[str, Any]:
    return {
        "id": record.id,
        "level": level or "user",
        "layer": record.layer.value,
        "scope": record.scope.value,
        "category": record.category.value,
        "title": record.title,
        "key": record.key,
        "content": record.content,
        "tags": list(record.tags),
        "source_ref": record.source_ref,
        "file_path": record.file_path,
        "confidence": record.confidence,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "superseded_by": record.superseded_by,
        "stale_status": record.stale_status.value,
        "estimated_tokens": estimated_tokens(record.content),
        "metadata": dict(record.metadata),
    }


def _config_summary(target: _Target) -> dict[str, Any]:
    settings = target.settings
    return {
        "enabled_in_config": target.enabled,
        "configured_backend": settings.backend,
        "backend": target.manager.backend.name,
        "storage_path": settings.storage_path,
        "auto_inject": settings.auto_inject,
        "auto_inject_tokens": settings.auto_inject_tokens,
        "extract_on_compact": settings.extract_on_compact,
    }


def _conversation_messages(space: UserSpace, context_id: str) -> list[dict[str, Any]]:
    view = space.context_manager().conversation_view(context_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages: list[dict[str, Any]] = []
    for message in view["messages"][-SYNC_MESSAGES:]:
        role = getattr(message, "role", None)
        if role is None and isinstance(message, dict):
            role, content = message.get("role"), message.get("content", "")
        elif hasattr(message, "get_text_content"):
            content = message.get_text_content()
        else:
            content = getattr(message, "content", "")
        if role in ("user", "assistant") and isinstance(content, str) and content:
            messages.append({"role": role, "content": content})
    return messages


async def _sync_with_model(target: _Target, messages: list[dict[str, Any]], source_ref: str) -> list | None:
    """The chat's last exchanges, each read by the agents' model (as after a turn).

    None when the system has no model path for it: the caller then distils with
    the heuristic instead. Exchanges go one at a time, oldest first, so a later
    one sees - and may retire - what an earlier one stored.
    """
    extract = getattr(target.factory, "extract_turn_with_model", None)
    if not target.enabled or extract is None or not getattr(target.settings, "extract_with_model", True):
        return None
    from core.factory.memory import chat_episode, store_by_level, store_episode

    snapshot = bool(getattr(target.settings, "snapshot_turns", True))
    context_id = source_ref.removeprefix("chat:")
    stored: dict[str, Any] = {}
    for request, answer in exchanges(messages)[-SYNC_EXCHANGES:]:
        current = await asyncio.to_thread(chat_episode, target.levels, context_id)
        extracted = await extract(target.levels, request, answer, target.agent_key, target.settings,
                                  source_ref=source_ref, titles=target.titles,
                                  episode=current[1].content if current else None)
        if extracted is None:  # the model failed: what it did store, never heuristic fragments
            return list(stored.values())
        for record in await asyncio.to_thread(
            store_by_level, target.levels, extracted, source_ref=source_ref,
            exchange=(request, answer) if snapshot else None,
        ):
            stored[record.id] = record
        episode = await asyncio.to_thread(store_episode, target.levels, context_id, extracted.episode, current,
                                          source_ref=source_ref)
        if episode is not None:
            if current is not None:
                stored.pop(current[1].id, None)  # replaced within this sync
            stored[episode.id] = episode
    return list(stored.values())


def _refused(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc) or "Invalid memory record")


def register_memory_routes(router: APIRouter, current_space: Callable[..., Any]) -> None:
    @router.get("/api/memory")
    async def memory_overview(
        context_id: str | None = Query(default=None, max_length=200),
        level: Level | None = None,
        layer: Layer | None = None,
        scope: Scope | None = None,
        category: Category | None = None,
        query: str = Query(default="", max_length=1000),
        stale_only: bool = False,
        include_stale: bool = True,
        limit: int = Query(default=100, ge=1, le=1000),
        space: UserSpace = Depends(current_space),
    ) -> JSONResponse:
        target = await _target(space, context_id)
        manager, settings = target.manager, target.settings

        def read() -> dict[str, Any]:
            overview = manager.overview_summary(
                context_id, query,
                enabled=target.enabled, auto_inject=settings.auto_inject,
                auto_inject_tokens=settings.auto_inject_tokens,
            )
            records = []
            levels = []
            for name, holder in target.levels:
                found = holder.list_records(
                    layer=layer, scope=scope, category=category, stale_only=stale_only,
                    include_stale=include_stale, query=query, limit=limit,
                ) if level in (None, name) else []
                records.extend(serialize_record(r, name) for r in found)
                counts = holder.stats().get("counts") or {}
                levels.append({"level": name, "title": target.titles.get(name, name),
                               "count": sum(int(v) for k, v in counts.items() if k != "L0" and isinstance(v, int))})
            if not query:
                records.sort(key=lambda record: record["updated_at"] or "", reverse=True)
            return {"overview": overview, "records": records[:limit], "levels": levels}

        try:
            data = await asyncio.to_thread(read)
        except (ValueError, OSError) as exc:
            raise _refused(exc) from None
        data["config"] = _config_summary(target)
        data["count"] = len(data["records"])
        data["filters"] = {
            "level": level, "layer": layer, "scope": scope, "category": category, "query": query,
            "stale_only": stale_only, "include_stale": include_stale, "limit": limit,
        }
        return JSONResponse(data)

    @router.post("/api/memory/records")
    async def create_record(body: MemoryCreateRequest, space: UserSpace = Depends(current_space)) -> JSONResponse:
        target = await _target(space, body.context_id)
        names = [name for name, _ in target.levels]
        level = "user" if body.layer == "L3" else body.level or ("project" if "project" in names else "user")
        holder = target.level(level)

        def write() -> MemoryRecord:
            record = MemoryRecord(
                content=body.content, layer=MemoryLayer(body.layer), scope=MemoryScope(body.scope),
                category=MemoryCategory(body.category), title=body.title, tags=tuple(body.tags),
                source_ref=body.source_ref or None, file_path=body.file_path or None,
                confidence=body.confidence,
            )
            return holder.remember(record)

        try:
            record = await asyncio.to_thread(write)
        except (ValueError, TypeError, OSError) as exc:
            raise _refused(exc) from None
        return JSONResponse(serialize_record(record, level), status_code=status.HTTP_201_CREATED)

    @router.api_route("/api/memory/records/{record_id}", methods=["PUT", "PATCH"])
    async def update_record(
        record_id: str, body: MemoryUpdateRequest, space: UserSpace = Depends(current_space)
    ) -> JSONResponse:
        target = await _target(space, body.context_id)
        fields = body.model_dump(exclude={"context_id", "level"}, exclude_none=True)
        name, holder, _old = await asyncio.to_thread(target.holder, record_id)
        destination = target.level(body.level) if body.level and body.level != name else None

        def update() -> MemoryRecord | None:
            record = holder.update_record(record_id, **fields) if fields else holder.get(record_id)
            if record is None or destination is None:
                return record
            # Another level: the record moves there, whole, and leaves this one.
            moved = destination.remember(record)
            holder.forget(record_id)
            return moved

        try:
            record = await asyncio.to_thread(update)
        except (ValueError, TypeError, OSError) as exc:
            raise _refused(exc) from None
        if record is None:
            raise HTTPException(status_code=404, detail="Memory record not found")
        return JSONResponse(serialize_record(record, body.level if destination is not None else name))

    @router.delete("/api/memory/records/{record_id}")
    async def delete_record(
        record_id: str,
        context_id: str | None = Query(default=None, max_length=200),
        space: UserSpace = Depends(current_space),
    ) -> JSONResponse:
        target = await _target(space, context_id)
        _name, holder, _record = await asyncio.to_thread(target.holder, record_id)
        removed = await asyncio.to_thread(holder.forget, record_id)
        if not removed:
            raise HTTPException(status_code=404, detail="Memory record not found")
        return JSONResponse({"ok": True, "id": record_id})

    @router.post("/api/memory/sync")
    async def sync_from_context(body: MemorySyncRequest, space: UserSpace = Depends(current_space)) -> JSONResponse:
        """Distil the chat's messages into memory: natural RU/EN prose as well as
        marked statements (core.memory.distill, MemoryManager.extract_and_store_from_messages).
        Idempotent: a repeated sync stores nothing twice."""
        target = await _target(space, body.context_id)
        messages = _conversation_messages(space, body.context_id)
        try:
            stored = await _sync_with_model(target, messages, f"chat:{body.context_id}")
            if stored is None:
                stored = await asyncio.to_thread(
                    lambda: target.manager.extract_and_store_from_messages(
                        messages, source_ref=f"chat:{body.context_id}",
                        snapshot=bool(getattr(target.settings, "snapshot_turns", True)),
                    )
                )
        except (ValueError, TypeError, OSError) as exc:
            raise _refused(exc) from None
        return JSONResponse({
            "ok": True,
            "context_id": body.context_id,
            "scanned": len(messages),
            "stored": len(stored),
            "records": [serialize_record(r) for r in stored],
        })
