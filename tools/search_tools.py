"""
Discovery tools for coordinator and orchestrator agents.

- search_tools:        Find available function tools by name / description keyword.
                       Automatically filters out tools whose required modalities are
                       not supported by the calling agent's model.
- search_skills:       Search skills in the database (FTS + tag filter).
- semantic_search_skills: Hybrid vector + FTS search over skills (by meaning).
"""

from __future__ import annotations

import difflib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from agents import RunContextWrapper, function_tool

logger = logging.getLogger(__name__)


# ── modality requirements ─────────────────────────────────────────────────────
# Maps tool names to the model capabilities they require.
# Tools absent from this dict have no modality requirement (available to all).

TOOL_MODALITY_REQUIREMENTS: Dict[str, List[str]] = {
    "view_image":            ["vision"],
    "analyze_image":         ["vision"],
    "ocr_process_file":      ["vision"],
    "ocr_batch":             ["vision"],
    "ocr_process_directory": ["vision"],
    "screen_capture":        ["vision"],
    "screen_capture_region": ["vision"],
    "screen_diff":           ["vision"],
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_model_capabilities(context: RunContextWrapper) -> List[str]:
    """
    Return the capabilities list of the model used by the calling agent.

    Falls back to ["text"] when the information is unavailable.
    """
    try:
        factory = context.context.factory
        agent_id = context.context.agent_id
        agent_cfg = factory.config.get_agent(agent_id)
        model_key = agent_cfg.model if agent_cfg else None
        if model_key:
            model_cfg = factory.config.get_model(model_key)
            caps = getattr(model_cfg, "capabilities", None)
            if caps:
                return list(caps)
    except Exception:
        pass
    return ["text"]


def _get_skill_manager(context: RunContextWrapper):
    try:
        return context.context.factory.skill_manager
    except AttributeError:
        return None


def _get_user_id(context: RunContextWrapper) -> str:
    return getattr(getattr(context, "context", None), "user_id", None) or "default_user"


# ── tools ─────────────────────────────────────────────────────────────────────

@function_tool
async def search_tools(
    context: RunContextWrapper,
    query: str,
    category: Optional[str] = None,
    limit: int = 15,
    filter_by_modality: bool = True,
) -> str:
    """
    Find available function tools by name or description keyword.

    Automatically hides tools whose modality requirements exceed the calling
    model's capabilities (e.g. vision tools are hidden for text-only models).

    Args:
        query:              Search keywords (e.g. "file read", "git commit").
        category:           Optional name prefix filter (e.g. "file", "git",
                            "skill", "memory", "ocr").
        limit:              Maximum number of results (default 15).
        filter_by_modality: If True (default), exclude tools that require
                            capabilities the current model does not have.

    Returns:
        JSON array of {name, description} sorted by relevance.
    """
    from tools.function_tools import AVAILABLE_TOOLS  # late import to avoid cycles

    model_caps = _get_model_capabilities(context) if filter_by_modality else []
    q = query.lower()
    candidates = []

    for name, func in AVAILABLE_TOOLS.items():
        # Category prefix filter
        if category and not name.startswith(category):
            continue

        # Modality filter
        if filter_by_modality:
            required = TOOL_MODALITY_REQUIREMENTS.get(name, [])
            if required and not all(r in model_caps for r in required):
                continue

        doc = (getattr(func, "__doc__", "") or "").lower()
        # Score: name similarity weighted higher than doc keyword hits
        score = difflib.SequenceMatcher(None, q, name.lower()).ratio() * 2.0
        score += sum(0.5 for kw in q.split() if kw in doc or kw in name.lower())
        if score > 0.1 or any(kw in name.lower() for kw in q.split()):
            first_doc_line = (getattr(func, "__doc__", "") or "").strip().split("\n")[0].strip()
            candidates.append((score, name, first_doc_line))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return json.dumps(
        [{"name": n, "description": d} for _, n, d in candidates[:limit]],
        ensure_ascii=False,
        indent=2,
    )


@function_tool
async def search_skills(
    context: RunContextWrapper,
    query: str = "",
    tags: str = "",
    agent_id: Optional[str] = None,
    limit: int = 10,
) -> str:
    """
    Search skills in the database.

    By default searches across all agents belonging to the current user.
    Pass agent_id to restrict results to a specific agent.

    Args:
        query:    Full-text search terms (searches name, content, tags,
                  summary).  Leave empty to list recent skills.
        tags:     Comma-separated tag filter (e.g. "python,git").
                  All listed tags must appear in the skill's tags field.
        agent_id: Optional — restrict to a specific agent's skills.
        limit:    Maximum results (default 10).

    Returns:
        JSON array of {id, name, summary, tags, agent_id}.
    """
    sm = _get_skill_manager(context)
    if not sm:
        return json.dumps({"error": "skill_manager not available"})

    user_id = _get_user_id(context)
    entries = sm.search(query=query, tags=tags, user_id=user_id, agent_id=agent_id, limit=limit)

    return json.dumps(
        [
            {
                "id": e.id,
                "name": e.name,
                "summary": e.summary,
                "tags": e.tags,
                "agent_id": e.agent_id,
            }
            for e in entries
        ],
        ensure_ascii=False,
        indent=2,
    )


# ── semantic skill search ──────────────────────────────────────────────────────

_SKILL_BACKFILL_DONE = False
_SKILL_CHUNK_THRESHOLD = 800  # chars


def _get_memory_store(context: RunContextWrapper) -> Any:
    try:
        return context.context.factory.memory_store
    except AttributeError:
        return None


def _get_embeddings(context: RunContextWrapper) -> Any:
    store = _get_memory_store(context)
    return getattr(store, "_embeddings", None) if store else None


def _skill_backfill(store: Any, emb: Any, user_id: Optional[str]) -> None:
    """
    Lazy one-time backfill: index all existing skills into ChromaDB (source=skill).
    Long skills are chunked with _split_markdown; all chunks share the same skill_id.
    """
    global _SKILL_BACKFILL_DONE
    if _SKILL_BACKFILL_DONE:
        return
    _SKILL_BACKFILL_DONE = True  # set early — errors are non-fatal

    try:
        from core.memory.embeddings import EmbeddingsManager
        all_skills = store.search_skills(query="", tags="", user_id=user_id, agent_id=None, limit=5000)
    except Exception as exc:
        logger.warning("skill backfill: fetch failed: %s", exc)
        return

    if not all_skills:
        return

    logger.info("skill backfill: indexing %d skills …", len(all_skills))
    for skill in all_skills:
        try:
            base_meta = {
                "skill_id":  skill.id,
                "name":      skill.name     or "",
                "tags":      skill.tags     or "",
                "user_id":   skill.user_id  or "",
                "agent_id":  skill.agent_id or "",
                "source":    "skill",
            }
            content = skill.content or ""
            if len(content) > _SKILL_CHUNK_THRESHOLD:
                chunks = EmbeddingsManager._split_markdown(content, chunk_size=_SKILL_CHUNK_THRESHOLD, overlap=150)
                for k, chunk in enumerate(chunks):
                    emb.upsert_text(
                        text=chunk,
                        doc_id=f"skill_{skill.id}_c{k}",
                        metadata={**base_meta, "chunk_index": k, "total_chunks": len(chunks)},
                    )
            else:
                emb.upsert_text(
                    text=content,
                    doc_id=f"skill_{skill.id}",
                    metadata={**base_meta, "chunk_index": 0, "total_chunks": 1},
                )
        except Exception as exc:
            logger.debug("skill backfill: skip skill %s: %s", skill.id, exc)

    logger.info("skill backfill: done (%d skills)", len(all_skills))


def _rrf_merge_skills(
    vec_ids: List[int],
    fts_ids: List[int],
    k: int = 60,
) -> List[Tuple[int, float]]:
    scores: Dict[int, float] = {}
    for rank, sid in enumerate(vec_ids):
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank + 1)
    for rank, sid in enumerate(fts_ids):
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


@function_tool
async def semantic_search_skills(
    context: RunContextWrapper,
    query: str,
    tags: str = "",
    limit: int = 10,
) -> str:
    """
    Search skills by MEANING using hybrid vector + keyword search.

    Finds relevant skills even when exact words don't match — e.g.
    query "how to save a file" finds a skill about "write_file / file persistence".

    Falls back to FTS-only if embeddings are unavailable.

    Args:
        query: Natural language description of what you're looking for
        tags:  Optional comma-separated tag filter (e.g. "python,git")
        limit: Number of results (1-20, default 10)

    Returns:
        Ranked skill entries with similarity scores.
    """
    limit = max(1, min(20, limit))
    user_id = _get_user_id(context)
    sm = _get_skill_manager(context)
    store = _get_memory_store(context)
    emb = _get_embeddings(context)

    if sm is None:
        return "❌ skill_manager not available"

    # ── Hybrid path ──────────────────────────────────────────────
    if emb is not None and store is not None:
        _skill_backfill(store, emb, user_id)

        vec_results: List[Dict] = []
        vec_ids: List[int] = []
        try:
            where: Any = {"source": {"$eq": "skill"}}
            vec_results = emb.search(query, n_results=min(limit * 3, 50), where=where)
            # Normalize similarities
            if vec_results:
                dists = [r.get("distance", 0.0) for r in vec_results]
                min_d, max_d = min(dists), max(dists)
                spread = max_d - min_d or 1.0
                for r in vec_results:
                    r["similarity"] = 1.0 - (r.get("distance", 0.0) - min_d) / spread
            vec_ids = [
                int(r["metadata"]["skill_id"])
                for r in vec_results
                if r.get("metadata", {}).get("skill_id") is not None
            ]
        except Exception as exc:
            logger.warning("semantic_search_skills: ChromaDB failed: %s", exc)

        fts_skills: List[Any] = []
        try:
            fts_skills = sm.search(query=query, tags=tags, user_id=user_id, limit=limit * 3)
        except Exception as exc:
            logger.warning("semantic_search_skills: FTS failed: %s", exc)

        fts_ids = [s.id for s in fts_skills]

        if vec_ids or fts_ids:
            merged = _rrf_merge_skills(vec_ids, fts_ids)[:limit]
            fts_by_id = {s.id: s for s in fts_skills}
            vec_sim: Dict[int, float] = {}
            for r in vec_results:
                sid = r.get("metadata", {}).get("skill_id")
                if sid is not None:
                    sid = int(sid)
                    vec_sim[sid] = max(vec_sim.get(sid, 0.0), r.get("similarity", 0.0))

            final: List[Any] = []
            for sid, rrf_score in merged:
                skill = fts_by_id.get(sid)
                if skill is None:
                    skill = store.get_skill_by_id(sid) if hasattr(store, "get_skill_by_id") else None
                if skill:
                    skill._rrf_score = rrf_score
                    skill._vec_sim   = vec_sim.get(sid, 0.0)
                    skill._in_fts    = sid in fts_by_id
                    final.append(skill)

            mode = (
                f"hybrid (vec={len(vec_ids)} + fts={len(fts_ids)} → RRF → top {len(final)})"
                if vec_ids and fts_ids
                else ("vector-only" if vec_ids else "fts-only")
            )
            return _format_skills(final, query, mode)

    # ── FTS fallback ─────────────────────────────────────────────
    try:
        fts_skills = sm.search(query=query, tags=tags, user_id=user_id, limit=limit)
        for s in fts_skills:
            s._rrf_score = 0.0
            s._vec_sim   = 0.0
            s._in_fts    = True
        return _format_skills(fts_skills, query, "fts-only (no embeddings)")
    except Exception as exc:
        return f"❌ Search error: {exc}"


def _format_skills(skills: List[Any], query: str, mode: str) -> str:
    if not skills:
        return f"[semantic_search_skills] Nothing found for query: '{query}' (mode: {mode})"

    lines = [f"## Skill search: '{query}'", f"_Mode: {mode}_\n"]
    for i, skill in enumerate(skills, 1):
        vec_sim = getattr(skill, "_vec_sim", 0.0)
        in_fts  = getattr(skill, "_in_fts",  False)
        signals = []
        if vec_sim > 0:
            signals.append(f"vec={vec_sim:.2f}")
        if in_fts:
            signals.append("fts✓")
        score_str = f"  `[{', '.join(signals)}]`" if signals else ""

        lines.append(f"### {i}. {skill.name}{score_str}")
        lines.append(f"**tags:** {skill.tags or '—'}  |  **id:** {skill.id}  |  **agent:** {skill.agent_id or '—'}")
        preview = (skill.content or "")[:400]
        if len(skill.content or "") > 400:
            preview += "…"
        lines.append(f"\n{preview}\n")
        lines.append("---")
    return "\n".join(lines)


# ── registry ──────────────────────────────────────────────────────────────────

SEARCH_TOOLS = {
    "search_tools":          search_tools,
    "search_skills":         search_skills,
    "semantic_search_skills": semantic_search_skills,
}
