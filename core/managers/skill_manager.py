"""
Skill Manager - Handles file-based skills with Database synchronization.

Storage:
    workspace/{user_id}/skills/{skill_name}/skill.md

Synchronization:
    - Filesystem is the source of truth for content.
    - Database is used for indexing and searching (FTS).
"""

import logging
import shutil
from pathlib import Path
from typing import List, Dict, Optional, Any
from core.memory_store import MemoryStore

logger = logging.getLogger(__name__)


class SkillManager:
    """
    Manages skills storage and synchronization.
    """

    def __init__(self, memory_store: MemoryStore, workspace_root: Path):
        """
        Initialize SkillManager.

        Args:
            memory_store: MemoryStore instance
            workspace_root: Root workspace directory
        """
        self.memory_store = memory_store
        self.workspace_root = Path(workspace_root)

    def _get_user_skills_dir(self, user_id: str) -> Path:
        """Get skills directory for a user."""
        return self.workspace_root / user_id / "skills"

    def _get_skill_path(self, user_id: str, skill_name: str) -> Path:
        """Get path to a specific skill file."""
        # Sanitize skill name to be safe for filesystem
        safe_name = "".join(c for c in skill_name if c.isalnum() or c in ('-', '_')).strip()
        return self._get_user_skills_dir(user_id) / safe_name / "skill.md"

    def create_skill(self, user_id: str, skill_name: str, content: str, tags: str = "") -> str:
        """
        Create a new skill.

        Args:
            user_id: User ID
            skill_name: Name of the skill
            content: Markdown content
            tags: Comma-separated tags

        Returns:
            Skill name
        """
        skill_path = self._get_skill_path(user_id, skill_name)
        
        if skill_path.exists():
            raise FileExistsError(f"Skill '{skill_name}' already exists for user {user_id}")

        # Create directory and file
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        with open(skill_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Index in DB
        self.memory_store.save(
            content=content,
            type="skill",
            user_id=user_id,
            # Store skill name in task_id or a separate metadata field? 
            # Using task_id to store skill_name for easy retrieval/linking is a hack but works for now.
            # Better: Put "Skill: {name}" in content or tags? 
            # Let's put "skill:{name}" in tags.
            tags=f"{tags},skill:{skill_name}".strip(","),
            importance=1.0 # Skills are high importance
        )
        
        logger.info(f"✅ Created skill '{skill_name}' for user {user_id}")
        return skill_name

    def update_skill(self, user_id: str, skill_name: str, content: str) -> bool:
        """
        Update an existing skill.

        Args:
            user_id: User ID
            skill_name: Name of the skill
            content: New content

        Returns:
            True if updated
        """
        skill_path = self._get_skill_path(user_id, skill_name)
        
        if not skill_path.exists():
            return False

        # Update file
        with open(skill_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Update DB
        # Find entry by user_id and skill tag
        entries = self.memory_store.search(
            query=f"skill:{skill_name}", # This might match content too if not careful
            type="skill",
            user_id=user_id,
            limit=1
        )
        
        # Filter strictly by tag if search is fuzzy
        target_entry = None
        for entry in entries:
            if f"skill:{skill_name}" in entry.tags:
                target_entry = entry
                break
        
        if target_entry:
            self.memory_store.update(target_entry.id, content=content)
        else:
            # Re-index if missing
            self.memory_store.save(
                content=content,
                type="skill",
                tags=f"skill:{skill_name}",
                user_id=user_id,
                importance=1.0
            )

        logger.info(f"✏️ Updated skill '{skill_name}' for user {user_id}")
        return True

    def delete_skill(self, user_id: str, skill_name: str) -> bool:
        """
        Delete a skill.

        Args:
            user_id: User ID
            skill_name: Name of the skill

        Returns:
            True if deleted
        """
        skill_path = self._get_skill_path(user_id, skill_name)
        
        if not skill_path.exists():
            return False

        # Delete file and parent dir
        try:
            shutil.rmtree(skill_path.parent)
        except OSError as e:
            logger.error(f"Failed to delete skill directory: {e}")
            return False

        # Delete from DB
        entries = self.memory_store.search(
            type="skill",
            user_id=user_id,
            limit=100 # Fetch potential matches
        )
        
        for entry in entries:
            if f"skill:{skill_name}" in entry.tags:
                self.memory_store.delete(entry.id, hard=True)

        logger.info(f"🗑️ Deleted skill '{skill_name}' for user {user_id}")
        return True

    def get_skill(self, user_id: str, skill_name: str) -> Optional[str]:
        """
        Get skill content.

        Args:
            user_id: User ID
            skill_name: Name of the skill

        Returns:
            Content string or None
        """
        skill_path = self._get_skill_path(user_id, skill_name)
        
        if not skill_path.exists():
            return None

        with open(skill_path, 'r', encoding='utf-8') as f:
            return f.read()

    def list_skills(self, user_id: str) -> List[str]:
        """
        List all skills for a user.

        Args:
            user_id: User ID

        Returns:
            List of skill names
        """
        skills_dir = self._get_user_skills_dir(user_id)
        if not skills_dir.exists():
            return []

        skills = []
        for item in skills_dir.iterdir():
            if item.is_dir() and (item / "skill.md").exists():
                skills.append(item.name)
        
        return sorted(skills)

    def sync_skills_to_db(self, user_id: str):
        """
        Sync filesystem skills to database.
        Should be called on agent startup or periodically.
        """
        skills = self.list_skills(user_id)
        
        # 1. Index/Update existing files
        for skill_name in skills:
            content = self.get_skill(user_id, skill_name)
            if content:
                # Check DB
                entries = self.memory_store.search(
                    type="skill",
                    user_id=user_id,
                    limit=100
                )
                
                found = False
                for entry in entries:
                    if f"skill:{skill_name}" in entry.tags:
                        found = True
                        if entry.content != content:
                            self.memory_store.update(entry.id, content=content)
                        break
                
                if not found:
                    self.memory_store.save(
                        content=content,
                        type="skill",
                        tags=f"skill:{skill_name}",
                        user_id=user_id,
                        importance=1.0
                    )

        # 2. Clean up DB entries for deleted files
        # This is expensive if user has many skills, optimization needed for production
        db_skills = self.memory_store.search(
            type="skill",
            user_id=user_id,
            limit=1000
        )
        
        for entry in db_skills:
            # Extract skill name from tags
            tags = entry.tags.split(",")
            skill_tag = next((t for t in tags if t.startswith("skill:")), None)
            if skill_tag:
                name = skill_tag.split(":", 1)[1]
                if name not in skills:
                    self.memory_store.delete(entry.id, hard=True)
                    logger.info(f"Removed orphaned DB entry for skill '{name}'")

    def broadcast_skill(self, source_user_id: str, skill_name: str, target_user_ids: List[str]) -> Dict[str, bool]:
        """
        Copy a skill to other users.

        Args:
            source_user_id: Source User ID
            skill_name: Name of skill to copy
            target_user_ids: List of target User IDs

        Returns:
            Dict {user_id: success}
        """
        content = self.get_skill(source_user_id, skill_name)
        if not content:
            return {uid: False for uid in target_user_ids}

        results = {}
        for target_uid in target_user_ids:
            try:
                # Check if exists
                if (self._get_skill_path(target_uid, skill_name)).exists():
                    self.update_skill(target_uid, skill_name, content)
                else:
                    self.create_skill(target_uid, skill_name, content)
                results[target_uid] = True
            except Exception as e:
                logger.error(f"Failed to broadcast skill to {target_uid}: {e}")
                results[target_uid] = False
        
        return results
