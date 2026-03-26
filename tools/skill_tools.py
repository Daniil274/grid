"""
Skill Tools — SQL-only skill management (no filesystem).

Tools:
- skill_save:   Create or update a skill (upsert by name)
- skill_read:   Read skill content by name
- skill_search: Search skills by FTS query and/or tags
- skill_update: Update name / content / tags of an existing skill
- skill_delete: Delete a skill by name
"""

import json
import logging
from typing import Optional

from agents import function_tool, RunContextWrapper

logger = logging.getLogger(__name__)


# ── context helpers ──────────────────────────────────────────────────────────

def _get_skill_manager(context: RunContextWrapper):
    try:
        return context.context.factory.skill_manager
    except AttributeError:
        return None


def _get_user_agent(context: RunContextWrapper):
    raw = getattr(context, "context", None)
    user_id = getattr(raw, "user_id", None) or "default_user"
    agent_id = getattr(raw, "agent_id", None) or "default_agent"
    return user_id, agent_id


# ── tools ────────────────────────────────────────────────────────────────────

@function_tool
async def skill_save(
    context: RunContextWrapper,
    name: str,
    content: str,
    tags: str = "",
) -> str:
    """
    Save (create or update) a skill in the database.

    Args:
        name:    Unique skill name for this user + agent.
        content: Full skill text (markdown instructions).
        tags:    Comma-separated tags for search filtering (e.g. "python,git,code").

    Returns:
        JSON with skill id and name.
    """
    sm = _get_skill_manager(context)
    if not sm:
        return json.dumps({"error": "skill_manager not available"})
    user_id, agent_id = _get_user_agent(context)
    skill_id = sm.save(name, content, tags=tags, user_id=user_id, agent_id=agent_id)
    return json.dumps({"ok": True, "id": skill_id, "name": name}, ensure_ascii=False)


@function_tool
async def skill_read(
    context: RunContextWrapper,
    name: str,
) -> str:
    """
    Read the content of a skill by name.

    Args:
        name: Skill name.

    Returns:
        Skill content string, or error JSON if not found.
    """
    sm = _get_skill_manager(context)
    if not sm:
        return json.dumps({"error": "skill_manager not available"})
    user_id, agent_id = _get_user_agent(context)
    entry = sm.get(name, user_id, agent_id)
    if not entry:
        return json.dumps({"error": f"Skill '{name}' not found"})
    return entry.content


@function_tool
async def skill_search(
    context: RunContextWrapper,
    query: str = "",
    tags: str = "",
    limit: int = 10,
) -> str:
    """
    Search skills in the database.

    Searches the current user's skills across all their agents.

    Args:
        query: Full-text search terms (name, content, tags, summary).
               Leave empty to list recent skills.
        tags:  Comma-separated tag filter (e.g. "python,git").
               All specified tags must match.
        limit: Maximum number of results (default 10).

    Returns:
        JSON list of {id, name, summary, tags, agent_id, updated_at}.
    """
    sm = _get_skill_manager(context)
    if not sm:
        return json.dumps({"error": "skill_manager not available"})
    user_id, _ = _get_user_agent(context)
    entries = sm.search(query=query, tags=tags, user_id=user_id, limit=limit)
    return json.dumps(
        [
            {
                "id": e.id,
                "name": e.name,
                "summary": e.summary,
                "tags": e.tags,
                "agent_id": e.agent_id,
                "updated_at": e.updated_at,
            }
            for e in entries
        ],
        ensure_ascii=False,
        indent=2,
    )


@function_tool
async def skill_update(
    context: RunContextWrapper,
    name: str,
    new_name: Optional[str] = None,
    content: Optional[str] = None,
    tags: Optional[str] = None,
) -> str:
    """
    Update an existing skill's name, content, or tags.

    At least one of new_name / content / tags must be provided.

    Args:
        name:     Current skill name (used to find the skill).
        new_name: New name for the skill (optional).
        content:  New content (optional).
        tags:     New comma-separated tags (optional, replaces existing tags).

    Returns:
        JSON with ok flag and skill id.
    """
    sm = _get_skill_manager(context)
    if not sm:
        return json.dumps({"error": "skill_manager not available"})
    user_id, agent_id = _get_user_agent(context)
    entry = sm.get(name, user_id, agent_id)
    if not entry:
        return json.dumps({"error": f"Skill '{name}' not found"})
    ok = sm.update(entry.id, name=new_name, content=content, tags=tags)
    return json.dumps({"ok": ok, "id": entry.id}, ensure_ascii=False)


@function_tool
async def skill_delete(
    context: RunContextWrapper,
    name: str,
) -> str:
    """
    Delete a skill by name.

    Args:
        name: Skill name to delete.

    Returns:
        JSON with ok flag.
    """
    sm = _get_skill_manager(context)
    if not sm:
        return json.dumps({"error": "skill_manager not available"})
    user_id, agent_id = _get_user_agent(context)
    entry = sm.get(name, user_id, agent_id)
    if not entry:
        return json.dumps({"error": f"Skill '{name}' not found"})
    ok = sm.delete(entry.id)
    return json.dumps({"ok": ok, "name": name}, ensure_ascii=False)


# ── registry ─────────────────────────────────────────────────────────────────

SKILL_TOOLS = {
    "skill_save": skill_save,
    "skill_read": skill_read,
    "skill_search": skill_search,
    "skill_update": skill_update,
    "skill_delete": skill_delete,
}
