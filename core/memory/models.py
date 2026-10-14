"""Backend-neutral memory values and deterministic ranking helpers."""
from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable
from uuid import uuid4


class MemoryLayer(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class MemoryScope(StrEnum):
    PROJECT = "project"
    USER = "user"
    SYSTEM = "system"


class MemoryCategory(StrEnum):
    DECISION = "decision"
    CONSTRAINT = "constraint"
    ARCHITECTURE = "architecture"
    BUG = "bug"
    PREFERENCE = "preference"
    WORKFLOW = "workflow"
    NOTE = "note"


class StaleStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNVERIFIED = "unverified"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    content: str
    id: str = field(default_factory=lambda: uuid4().hex)
    layer: MemoryLayer = MemoryLayer.L1
    scope: MemoryScope = MemoryScope.PROJECT
    category: MemoryCategory = MemoryCategory.NOTE
    title: str = ""
    key: str = ""
    tags: tuple[str, ...] = ()
    source_ref: str | None = None
    file_path: str | None = None
    file_mtime_ns: int | None = None
    file_sha256: str | None = None
    confidence: float = 1.0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    superseded_by: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    stale_status: StaleStatus = StaleStatus.UNVERIFIED

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("Memory id must be a nonempty string")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("Memory metadata must be a mapping")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("Memory content must not be empty")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    text: str = ""
    layers: tuple[MemoryLayer, ...] = tuple(MemoryLayer)
    scopes: tuple[MemoryScope, ...] = tuple(MemoryScope)
    limit: int = 20
    include_stale: bool = False
    include_superseded: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")


@dataclass(frozen=True, slots=True)
class MemorySearchHit:
    record: MemoryRecord
    score: float
    bm25_rank: int | None = None
    lexical_rank: int | None = None
    rrf_score: float = 0.0


@dataclass(frozen=True, slots=True)
class MemoryContextPack:
    profile_l3: tuple[MemoryRecord, ...] = ()
    scenarios_l2: tuple[MemoryRecord, ...] = ()
    facts_l1: tuple[MemoryRecord, ...] = ()
    total_estimated_tokens: int = 0
    backend_name: str = ""


# UTF-8 bytes are a deliberately conservative tokenizer-free budget estimate.
def estimated_tokens(text: str) -> int:
    return len(text.encode("utf-8"))


def context_pack(hits: Sequence[MemorySearchHit], backend_name: str) -> MemoryContextPack:
    records = [hit.record for hit in hits if hit.record.layer != MemoryLayer.L0]
    return MemoryContextPack(
        profile_l3=tuple(r for r in records if r.layer == MemoryLayer.L3),
        scenarios_l2=tuple(r for r in records if r.layer == MemoryLayer.L2),
        facts_l1=tuple(r for r in records if r.layer == MemoryLayer.L1),
        total_estimated_tokens=sum(estimated_tokens(r.content) for r in records),
        backend_name=backend_name,
    )


T = TypeVar("T", bound=Hashable)


def reciprocal_rank_fusion(ranked_lists: Iterable[Iterable[T]], k: int = 60) -> dict[T, float]:
    """Return scores in descending order; repeated items in one list count once."""
    if k < 0:
        raise ValueError("k must be non-negative")
    scores: dict[T, float] = {}
    for ranking in ranked_lists:
        seen: set[T] = set()
        rank = 0
        for item in ranking:
            if item in seen:
                continue
            seen.add(item)
            rank += 1
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return dict(sorted(scores.items(), key=lambda pair: -pair[1]))


@runtime_checkable
class MemoryBackend(Protocol):
    name: str

    def remember(self, record: MemoryRecord, *, supersedes: str | None = None) -> MemoryRecord: ...
    def get(self, record_id: str) -> MemoryRecord | None: ...
    def forget(self, record_id: str) -> bool: ...
    def search(self, query: MemoryQuery) -> list[MemorySearchHit]: ...
    def build_context(self, query: MemoryQuery) -> MemoryContextPack: ...
    def healthcheck(self) -> bool: ...
    def close(self) -> None: ...


def build_context_pack(backend: MemoryBackend, query: MemoryQuery) -> MemoryContextPack:
    """Keep profile independent of topical relevance; exclude raw L0 transcripts."""
    hits: list[MemorySearchHit] = []
    for layer in (MemoryLayer.L3, MemoryLayer.L2, MemoryLayer.L1):
        if layer in query.layers:
            hits.extend(backend.search(replace(
                query, layers=(layer,), text="" if layer == MemoryLayer.L3 else query.text,
            )))
    return context_pack(hits, backend.name)


def list_records_via_search(
    backend: MemoryBackend,
    *,
    layers: Sequence[MemoryLayer] = tuple(MemoryLayer),
    scopes: Sequence[MemoryScope] = tuple(MemoryScope),
    category: MemoryCategory | None = None,
    stale_only: bool = False,
    include_stale: bool = True,
    include_superseded: bool = False,
    query: str = "",
    limit: int = 100,
) -> list[MemoryRecord]:
    """Browse records over any backend's ``search``; no search text is required.

    Without *query* the newest records come first (``updated_at`` descending);
    with one, the backend's hybrid relevance order is kept.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    hits = backend.search(MemoryQuery(
        text=query, layers=tuple(layers), scopes=tuple(scopes), limit=1000,
        include_stale=include_stale or stale_only, include_superseded=include_superseded,
    ))
    records = [hit.record for hit in hits]
    if category is not None:
        records = [r for r in records if r.category == category]
    if stale_only:
        records = [r for r in records if r.stale_status == StaleStatus.STALE]
    elif not include_stale:
        records = [r for r in records if r.stale_status != StaleStatus.STALE]
    if not query.strip():
        records.sort(key=lambda r: (r.updated_at, r.id), reverse=True)
    return records[:limit]


def stats_via_search(backend: MemoryBackend) -> dict[str, Any]:
    """Record counts per layer and how many are stale (capped at 1000 per layer)."""
    counts: dict[str, int] = {}
    stale = 0
    for layer in MemoryLayer:
        records = list_records_via_search(backend, layers=(layer,), limit=1000)
        counts[layer.value] = len(records)
        stale += sum(1 for r in records if r.stale_status == StaleStatus.STALE)
    return {"counts": counts, "stale_count": stale, "total": sum(counts.values())}


def record_to_dict(record: MemoryRecord) -> dict[str, Any]:
    return {f.name: getattr(record, f.name) for f in fields(record)}


def record_from_dict(data: Mapping[str, Any]) -> MemoryRecord:
    values = {f.name: data[f.name] for f in fields(MemoryRecord) if f.name in data}
    for name, enum in (("layer", MemoryLayer), ("scope", MemoryScope),
                       ("category", MemoryCategory), ("stale_status", StaleStatus)):
        if name in values:
            values[name] = enum(values[name])
    if "tags" in values:
        values["tags"] = tuple(values["tags"])
    return MemoryRecord(**values)
