"""
Skill Manager — thin wrapper around MemoryStore.skills_* methods.

Skills are stored entirely in SQLite (no filesystem).
Each skill has: id, name, content, tags, user_id, agent_id, summary,
created_at, updated_at.
"""

import logging
from typing import List, Optional, Any

from core.memory_store import MemoryStore, SkillEntry

logger = logging.getLogger(__name__)


class SkillManager:
    """
    SQL-only skill storage.  All reads and writes go through MemoryStore.
    No filesystem operations are performed.
    """

    def __init__(self, memory_store: MemoryStore, workspace_root: Any = None):
        """
        Args:
            memory_store: MemoryStore instance (required).
            workspace_root: Ignored — kept for backward-compat with old callers.
        """
        self.memory_store = memory_store

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def save(
        self,
        name: str,
        content: str,
        tags: str = "",
        user_id: str = "default_user",
        agent_id: str = "default_agent",
    ) -> int:
        """Upsert a skill.  Returns the skill id."""
        skill_id = self.memory_store.save_skill(name, content, tags, user_id, agent_id)
        logger.info(f"💾 Skill saved: '{name}' (id={skill_id}, user={user_id}, agent={agent_id})")
        return skill_id

    def get(
        self,
        name: str,
        user_id: str = "default_user",
        agent_id: str = "default_agent",
    ) -> Optional[SkillEntry]:
        """Return a SkillEntry by exact name, or None."""
        return self.memory_store.get_skill(name, user_id, agent_id)

    def search(
        self,
        query: str = "",
        tags: str = "",
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[SkillEntry]:
        """
        Search skills via FTS5 and/or tag filter.

        Omit agent_id to search across all agents for the given user.
        """
        return self.memory_store.search_skills(query, tags, user_id, agent_id, limit)

    def update(
        self,
        skill_id: int,
        name: Optional[str] = None,
        content: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> bool:
        """Update skill fields by id.  Returns True if updated."""
        ok = self.memory_store.update_skill(skill_id, name=name, content=content, tags=tags)
        if ok:
            logger.info(f"✏️ Skill id={skill_id} updated")
        return ok

    def delete(self, skill_id: int) -> bool:
        """Delete skill by id.  Returns True if deleted."""
        ok = self.memory_store.delete_skill(skill_id)
        if ok:
            logger.info(f"🗑️ Skill id={skill_id} deleted")
        return ok

    # ── Convenience helpers (used by skill_tools) ─────────────────────────────

    def get_id_by_name(
        self,
        name: str,
        user_id: str = "default_user",
        agent_id: str = "default_agent",
    ) -> Optional[int]:
        """Return skill id by name, or None."""
        entry = self.get(name, user_id, agent_id)
        return entry.id if entry else None
