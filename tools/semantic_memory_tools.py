"""
Semantic Memory Search — hybrid vector + keyword search over memory_store.

Unlike memory_search (FTS5 keyword-only), this tool searches by MEANING.
It combines:
  1. ChromaDB vector similarity  (primary — finds by concept, no keyword required)
  2. FTS5 keyword match          (secondary — fast exact/partial matches)
  3. RRF (Reciprocal Rank Fusion) merge of both ranked lists

Degrades gracefully:
  - No embeddings → pure FTS5 with similarity scoring
  - ChromaDB empty but entries exist → lazy backfill on first call
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from agents import RunContextWrapper, function_tool

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Context helpers (same pattern as memory_tools_v2.py)
# ──────────────────────────────────────────────────────────────

def _get_store(context: RunContextWrapper) -> Any:
    try:
        raw = getattr(context, "context", None)
        if raw is None:
            return None
        factory = getattr(raw, "factory", None)
        if factory is None:
            return None
        return getattr(factory, "memory_store", None)
    except Exception as exc:
        logger.error("semantic_memory: failed to get memory_store: %s", exc)
        return None


def _get_ids(context: RunContextWrapper) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    session_id = user_id = agent_id = None
    try:
        raw = getattr(context, "context", None)
        if raw:
            session_id = getattr(raw, "session_id", None)
            user_id    = getattr(raw, "user_id",    None)
            agent_id   = getattr(raw, "agent_id",   None)
            if hasattr(raw, "context_manager"):
                cm = raw.context_manager
                if hasattr(cm, "get_metadata"):
                    user_id  = user_id  or cm.get_metadata("user_id")
                    agent_id = agent_id or cm.get_metadata("agent_id")
    except Exception:
        pass
    return session_id, user_id, agent_id


# ──────────────────────────────────────────────────────────────
# Lazy ChromaDB backfill
# ──────────────────────────────────────────────────────────────

_BACKFILL_DONE = False
_MEM_CHUNK_THRESHOLD = 800  # chars; entries longer than this are split before indexing


def _index_entry(emb: Any, entry: Any) -> None:
    """
    Index a single memory entry into ChromaDB.
    Long entries are split with _split_markdown so each chunk gets its own vector.
    All chunks share the same entry_id in metadata, enabling dedup at retrieval time.
    """
    from core.embeddings import EmbeddingsManager

    base_meta = {
        "entry_id":   entry.id,
        "type":       entry.type      or "",
        "tags":       entry.tags      or "",
        "user_id":    entry.user_id   or "",
        "agent_id":   entry.agent_id  or "",
        "importance": entry.importance,
        "source":     "memory",
    }
    content = entry.content

    if len(content) > _MEM_CHUNK_THRESHOLD:
        chunks = EmbeddingsManager._split_markdown(content, chunk_size=_MEM_CHUNK_THRESHOLD, overlap=150)
        for k, chunk in enumerate(chunks):
            emb.upsert_text(
                text=chunk,
                doc_id=f"mem_{entry.id}_c{k}",
                metadata={**base_meta, "chunk_index": k, "total_chunks": len(chunks)},
            )
    else:
        emb.upsert_text(
            text=content,
            doc_id=f"mem_{entry.id}",
            metadata={**base_meta, "chunk_index": 0, "total_chunks": 1},
        )


def _maybe_backfill(store: Any) -> None:
    """
    If ChromaDB collection is empty but SQLite has entries, index them all.
    Long entries are chunked via _split_markdown.
    Called once per process. Errors are non-fatal.
    """
    global _BACKFILL_DONE
    if _BACKFILL_DONE:
        return

    emb = getattr(store, "_embeddings", None)
    if emb is None:
        _BACKFILL_DONE = True
        return

    try:
        chroma_count = emb.get_collection_count()
    except Exception:
        _BACKFILL_DONE = True
        return

    if chroma_count > 0:
        _BACKFILL_DONE = True
        return

    try:
        all_entries = store.search(query="", limit=5000)
    except Exception as exc:
        logger.warning("semantic_memory backfill: search failed: %s", exc)
        _BACKFILL_DONE = True
        return

    if not all_entries:
        _BACKFILL_DONE = True
        return

    logger.info("semantic_memory: backfilling %d entries into ChromaDB …", len(all_entries))
    for entry in all_entries:
        try:
            _index_entry(emb, entry)
        except Exception as exc:
            logger.debug("backfill: skip entry %s: %s", entry.id, exc)

    logger.info("semantic_memory: backfill complete (%d entries indexed)", len(all_entries))
    _BACKFILL_DONE = True


# ──────────────────────────────────────────────────────────────
# RRF merge
# ──────────────────────────────────────────────────────────────

def _rrf_merge(
    vec_ids: List[int],    # ordered by vector similarity (best first)
    fts_ids: List[int],    # ordered by FTS rank
    k: int = 60,
) -> List[Tuple[int, float]]:
    """
    Reciprocal Rank Fusion.
    Returns [(entry_id, rrf_score)] sorted descending.
    """
    scores: Dict[int, float] = {}
    for rank, eid in enumerate(vec_ids):
        scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank + 1)
    for rank, eid in enumerate(fts_ids):
        scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ──────────────────────────────────────────────────────────────
# Core search logic
# ──────────────────────────────────────────────────────────────

def _semantic_search(
    store: Any,
    query: str,
    memory_type: Optional[str],
    user_id: Optional[str],
    limit: int,
) -> Tuple[List[Any], str]:
    """
    Returns (entries, mode_description).
    mode_description explains what search path was used.
    """
    emb = getattr(store, "_embeddings", None)

    # ── Path A: Hybrid (vector + FTS) ─────────────────────────
    if emb is not None:
        _maybe_backfill(store)

        vec_results: List[Dict] = []
        vec_ids: List[int] = []
        try:
            # Build ChromaDB filter
            where_filter: Optional[Dict] = None
            if user_id:
                where_filter = {"user_id": {"$eq": user_id}}
            if memory_type:
                type_filter = {"type": {"$eq": memory_type}}
                if where_filter:
                    where_filter = {"$and": [where_filter, type_filter]}
                else:
                    where_filter = type_filter

            vec_results = emb.search(query, n_results=min(limit * 3, 50), where=where_filter)

            # Normalize similarity relative to result set (handles L2/cosine/any metric)
            if vec_results:
                dists = [r.get("distance", 0.0) for r in vec_results]
                min_d, max_d = min(dists), max(dists)
                spread = max_d - min_d or 1.0
                for r in vec_results:
                    r["similarity"] = 1.0 - (r.get("distance", 0.0) - min_d) / spread

            vec_ids = [
                int(r["metadata"]["entry_id"])
                for r in vec_results
                if r.get("metadata", {}).get("entry_id") is not None
            ]
        except Exception as exc:
            logger.warning("semantic_memory: ChromaDB search failed: %s", exc)

        # FTS5 pass
        fts_entries = []
        try:
            fts_entries = store.search(
                query=query,
                type=memory_type or None,
                user_id=user_id,
                limit=limit * 3,
            )
        except Exception as exc:
            logger.warning("semantic_memory: FTS search failed: %s", exc)

        fts_ids = [e.id for e in fts_entries]

        if vec_ids or fts_ids:
            merged = _rrf_merge(vec_ids, fts_ids)[:limit]

            # Load full entries (prefer already-fetched FTS entries to avoid extra DB calls)
            fts_by_id = {e.id: e for e in fts_entries}
            # An entry may have multiple chunks; keep the highest similarity score.
            vec_sim: Dict[int, float] = {}
            for r in vec_results:
                eid = r.get("metadata", {}).get("entry_id")
                if eid is not None:
                    eid = int(eid)
                    vec_sim[eid] = max(vec_sim.get(eid, 0.0), r.get("similarity", 0.0))

            final_entries = []
            for eid, rrf_score in merged:
                entry = fts_by_id.get(eid)
                if entry is None:
                    entry = store.get_by_id(eid)
                if entry:
                    # Attach scores as temporary attributes for formatting
                    entry._rrf_score = rrf_score
                    entry._vec_sim   = vec_sim.get(eid, 0.0)
                    entry._in_fts    = eid in fts_by_id
                    final_entries.append(entry)

            mode = (
                f"hybrid (vector={len(vec_ids)} + fts={len(fts_ids)} → RRF → top {len(final_entries)})"
                if vec_ids and fts_ids
                else ("vector-only" if vec_ids else "fts-only (vector empty)")
            )
            return final_entries, mode

    # ── Path B: FTS5 fallback ──────────────────────────────────
    try:
        fts_entries = store.search(
            query=query,
            type=memory_type or None,
            user_id=user_id,
            limit=limit,
        )
        for e in fts_entries:
            e._rrf_score = 0.0
            e._vec_sim   = 0.0
            e._in_fts    = True
        return fts_entries, "fts5-only (no embeddings)"
    except Exception as exc:
        logger.error("semantic_memory: FTS fallback failed: %s", exc)
        return [], "error"


# ──────────────────────────────────────────────────────────────
# Agent tool
# ──────────────────────────────────────────────────────────────

@function_tool
async def semantic_memory_search(
    context: RunContextWrapper,
    query: str,
    type: str = "",
    limit: int = 10,
) -> str:
    """
    Search memory by MEANING, not just keywords.

    Uses hybrid vector + keyword search (ChromaDB + FTS5 with RRF merge).
    Finds relevant memories even when the exact words don't match —
    e.g., query "как решить проблему со звуком" finds memories about
    "ALSA", "TLV320AIC3100", "audio DSP" by semantic similarity.

    Prefer this over memory_search when:
    - You're looking for a concept, not a specific keyword
    - The query is in one language but memories might use another
    - You want the most RELEVANT result, not just matching words

    Args:
        query: Natural language description of what you're looking for
        type: Optional filter — long_term | short_term | task | task_plan | skill | insight
        limit: Number of results (1-30, default 10)

    Returns:
        Ranked memory entries with similarity scores and source info.
    """
    limit = max(1, min(30, limit))
    memory_type = type.strip() or None

    store = _get_store(context)
    if store is None:
        return "❌ Memory store not available"

    _, user_id, _ = _get_ids(context)

    entries, mode = _semantic_search(store, query, memory_type, user_id, limit)

    if not entries:
        return f"[semantic_memory_search] Ничего не найдено по запросу: '{query}' (mode: {mode})"

    lines = [f"## Семантический поиск по памяти: '{query}'"]
    lines.append(f"_Режим: {mode}_\n")

    for i, entry in enumerate(entries, 1):
        vec_sim = getattr(entry, "_vec_sim", 0.0)
        rrf    = getattr(entry, "_rrf_score", 0.0)
        in_fts = getattr(entry, "_in_fts", False)

        signals = []
        if vec_sim > 0:
            signals.append(f"vec={vec_sim:.2f}")
        if in_fts:
            signals.append("fts✓")
        score_str = f"  `[{', '.join(signals)}]`" if signals else ""

        lines.append(f"### {i}. [{entry.type}] {entry.tags or '(нет тегов)'}{score_str}")
        lines.append(f"**importance:** {entry.importance:.1f}  |  **id:** {entry.id}")
        lines.append(f"**сохранено:** {entry.created_at[:10] if entry.created_at else '?'}")
        lines.append("")
        lines.append(entry.content[:600] + ("…" if len(entry.content) > 600 else ""))
        lines.append("")
        lines.append("---")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────

SEMANTIC_MEMORY_TOOLS = {
    "semantic_memory_search": semantic_memory_search,
}
