"""
Skill Manager - Handles file-based skills with Database synchronization.

Storage (Anthropic standard):
    workspace/{user_id}/agents/{agent_id}/skills/{skill_name}/SKILL.md

Companion files (scripts, templates, references, etc.) are stored alongside
SKILL.md in the same skill directory, mirroring the Anthropic skills structure.

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

# Anthropic standard skill filename
SKILL_FILENAME = "SKILL.md"
# Legacy filename for backward compatibility
SKILL_FILENAME_LEGACY = "skill.md"


class SkillManager:
    """
    Manages skills storage and synchronization.

    Each skill follows the Anthropic standard structure:
        {skill_name}/
            SKILL.md          - Main instructions (required)
            scripts/          - Helper scripts (optional)
            templates/        - Template files (optional)
            reference/        - Reference documentation (optional)
            examples/         - Example files (optional)
            LICENSE.txt       - License information (optional)
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

    def _sanitize_path_component(self, value: str, fallback: str) -> str:
        """Sanitize a path component for safe filesystem storage."""
        safe_value = "".join(c for c in value if c.isalnum() or c in ('-', '_')).strip()
        return safe_value or fallback

    def _get_agent_skills_dir(self, user_id: str, agent_id: str) -> Path:
        """Get skills directory for a user+agent pair."""
        safe_user_id = self._sanitize_path_component(user_id, "default_user")
        safe_agent_id = self._sanitize_path_component(agent_id, "default_agent")
        return self.workspace_root / safe_user_id / "agents" / safe_agent_id / "skills"

    def _get_skill_dir(self, user_id: str, agent_id: str, skill_name: str) -> Path:
        """Get the skill's root directory."""
        safe_name = self._sanitize_path_component(skill_name, "skill")
        return self._get_agent_skills_dir(user_id, agent_id) / safe_name

    def get_skill_dir_path(self, user_id: str, agent_id: str, skill_name: str) -> Optional[Path]:
        """Return the absolute path to a skill's directory, or None if it doesn't exist."""
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        if self._find_skill_md(skill_dir):
            return skill_dir
        return None

    def _get_skill_path(self, user_id: str, agent_id: str, skill_name: str) -> Path:
        """Get path to the skill's SKILL.md file (Anthropic standard)."""
        return self._get_skill_dir(user_id, agent_id, skill_name) / SKILL_FILENAME

    def _find_skill_md(self, skill_dir: Path) -> Optional[Path]:
        """Find the skill's main file, checking SKILL.md then legacy skill.md."""
        standard = skill_dir / SKILL_FILENAME
        if standard.exists():
            return standard
        legacy = skill_dir / SKILL_FILENAME_LEGACY
        if legacy.exists():
            return legacy
        return None

    def create_skill(
        self,
        user_id: str,
        agent_id: str,
        skill_name: str,
        content: str,
        tags: str = "",
        companion_files_dir: Optional[Path] = None
    ) -> str:
        """
        Create a new skill following the Anthropic standard.

        Args:
            user_id: User ID
            agent_id: Agent ID
            skill_name: Name of the skill
            content: Markdown content for SKILL.md
            tags: Comma-separated tags
            companion_files_dir: Optional path to a directory whose contents
                (scripts, templates, references, etc.) are copied into the
                skill folder alongside SKILL.md.

        Returns:
            Skill name
        """
        skill_path = self._get_skill_path(user_id, agent_id, skill_name)

        if skill_path.exists():
            raise FileExistsError(
                f"Skill '{skill_name}' already exists for user {user_id} and agent {agent_id}"
            )

        # Create skill directory
        skill_path.parent.mkdir(parents=True, exist_ok=True)

        # Copy companion files first (SKILL.md from companion dir, if any, is overwritten below)
        if companion_files_dir:
            companion_path = Path(companion_files_dir)
            if companion_path.is_dir():
                shutil.copytree(str(companion_path), str(skill_path.parent), dirs_exist_ok=True)
                logger.info(f"📁 Copied companion files from '{companion_path}' for skill '{skill_name}'")
            else:
                logger.warning(f"⚠️ companion_files_dir '{companion_path}' is not a directory, skipping")

        # Write SKILL.md (authoritative content, overrides any companion version)
        with open(skill_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Index in DB
        self.memory_store.save(
            content=content,
            type="skill",
            user_id=user_id,
            agent_id=agent_id,
            tags=f"{tags},skill:{skill_name}".strip(","),
            importance=1.0
        )

        logger.info(f"✅ Created skill '{skill_name}' for user {user_id} and agent {agent_id}")
        return skill_name

    def update_skill(self, user_id: str, agent_id: str, skill_name: str, content: str) -> bool:
        """
        Update an existing skill's SKILL.md content.

        Args:
            user_id: User ID
            agent_id: Agent ID
            skill_name: Name of the skill
            content: New content

        Returns:
            True if updated
        """
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        skill_md = self._find_skill_md(skill_dir)

        if not skill_md:
            return False

        # Update file (always write to standard SKILL.md on update)
        skill_path = self._get_skill_path(user_id, agent_id, skill_name)
        with open(skill_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Remove legacy skill.md if it exists alongside new SKILL.md
        legacy_path = skill_dir / SKILL_FILENAME_LEGACY
        if legacy_path.exists() and skill_path != legacy_path:
            legacy_path.unlink()

        # Update DB
        entries = self.memory_store.search(
            query=f"skill:{skill_name}",
            type="skill",
            user_id=user_id,
            agent_id=agent_id,
            limit=1
        )

        target_entry = None
        for entry in entries:
            if f"skill:{skill_name}" in entry.tags:
                target_entry = entry
                break

        if target_entry:
            self.memory_store.update(target_entry.id, content=content)
        else:
            self.memory_store.save(
                content=content,
                type="skill",
                tags=f"skill:{skill_name}",
                user_id=user_id,
                agent_id=agent_id,
                importance=1.0
            )

        logger.info(f"✏️ Updated skill '{skill_name}' for user {user_id} and agent {agent_id}")
        return True

    def delete_skill(self, user_id: str, agent_id: str, skill_name: str) -> bool:
        """
        Delete a skill and all its companion files.

        Args:
            user_id: User ID
            agent_id: Agent ID
            skill_name: Name of the skill

        Returns:
            True if deleted
        """
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)

        if not self._find_skill_md(skill_dir):
            return False

        # Delete entire skill directory (includes companion files)
        try:
            shutil.rmtree(skill_dir)
        except OSError as e:
            logger.error(f"Failed to delete skill directory: {e}")
            return False

        # Delete from DB
        entries = self.memory_store.search(
            type="skill",
            user_id=user_id,
            agent_id=agent_id,
            limit=100
        )

        for entry in entries:
            if f"skill:{skill_name}" in entry.tags:
                self.memory_store.delete(entry.id, hard=True)

        logger.info(f"🗑️ Deleted skill '{skill_name}' for user {user_id} and agent {agent_id}")
        return True

    def get_skill(self, user_id: str, agent_id: str, skill_name: str) -> Optional[str]:
        """
        Get skill's SKILL.md content.

        Args:
            user_id: User ID
            agent_id: Agent ID
            skill_name: Name of the skill

        Returns:
            Content string or None
        """
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        skill_md = self._find_skill_md(skill_dir)

        if not skill_md:
            return None

        with open(skill_md, 'r', encoding='utf-8') as f:
            return f.read()

    def get_skill_files(self, user_id: str, agent_id: str, skill_name: str) -> List[str]:
        """
        List all companion files in a skill's directory (excludes SKILL.md).

        Args:
            user_id: User ID
            agent_id: Agent ID
            skill_name: Name of the skill

        Returns:
            List of relative file paths within the skill directory
        """
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        if not skill_dir.exists():
            return []

        files = []
        for f in skill_dir.rglob("*"):
            if f.is_file() and f.name not in (SKILL_FILENAME, SKILL_FILENAME_LEGACY):
                files.append(str(f.relative_to(skill_dir)).replace("\\", "/"))
        return sorted(files)

    def read_skill_file(self, user_id: str, agent_id: str, skill_name: str, relative_path: str) -> Optional[str]:
        """
        Read a specific companion file within a skill directory.

        Args:
            user_id: User ID
            agent_id: Agent ID
            skill_name: Name of the skill
            relative_path: Path relative to the skill directory

        Returns:
            File content or None if not found
        """
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        # Prevent path traversal
        target = (skill_dir / relative_path).resolve()
        if not str(target).startswith(str(skill_dir.resolve())):
            logger.warning(f"Path traversal attempt blocked: {relative_path}")
            return None

        if not target.exists() or not target.is_file():
            return None

        with open(target, 'r', encoding='utf-8') as f:
            return f.read()

    def list_skills(self, user_id: str, agent_id: str) -> List[str]:
        """
        List all skills for a user+agent pair.

        Args:
            user_id: User ID
            agent_id: Agent ID

        Returns:
            List of skill names
        """
        skills_dir = self._get_agent_skills_dir(user_id, agent_id)
        if not skills_dir.exists():
            return []

        skills = []
        for item in skills_dir.iterdir():
            if item.is_dir() and self._find_skill_md(item):
                skills.append(item.name)

        return sorted(skills)

    def search_skills(self, user_id: str, agent_id: str, query: str, limit: int = 10) -> List[Any]:
        """Search skills for a specific user+agent pair."""
        return self.memory_store.search(
            query=query,
            type="skill",
            user_id=user_id,
            agent_id=agent_id,
            limit=limit,
        )

    def sync_skills_to_db(self, user_id: str, agent_id: str):
        """
        Sync filesystem skills to database.
        Should be called on agent startup or periodically.
        """
        skills = self.list_skills(user_id, agent_id)

        # 1. Index/Update existing files
        for skill_name in skills:
            content = self.get_skill(user_id, agent_id, skill_name)
            if content:
                entries = self.memory_store.search(
                    type="skill",
                    user_id=user_id,
                    agent_id=agent_id,
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
                        agent_id=agent_id,
                        importance=1.0
                    )

        # 2. Clean up DB entries for deleted files
        db_skills = self.memory_store.search(
            type="skill",
            user_id=user_id,
            agent_id=agent_id,
            limit=1000
        )

        for entry in db_skills:
            tags = entry.tags.split(",")
            skill_tag = next((t for t in tags if t.startswith("skill:")), None)
            if skill_tag:
                name = skill_tag.split(":", 1)[1]
                if name not in skills:
                    self.memory_store.delete(entry.id, hard=True)
                    logger.info(f"Removed orphaned DB entry for skill '{name}'")

    def broadcast_skill(
        self,
        source_user_id: str,
        source_agent_id: str,
        skill_name: str,
        target_user_ids: List[str]
    ) -> Dict[str, bool]:
        """
        Copy a skill (including all companion files) to other users.

        Args:
            source_user_id: Source User ID
            source_agent_id: Source Agent ID
            skill_name: Name of skill to copy
            target_user_ids: List of target User IDs

        Returns:
            Dict {user_id: success}
        """
        source_dir = self._get_skill_dir(source_user_id, source_agent_id, skill_name)
        content = self.get_skill(source_user_id, source_agent_id, skill_name)

        if not content:
            return {uid: False for uid in target_user_ids}

        results = {}
        for target_uid in target_user_ids:
            try:
                target_dir = self._get_skill_dir(target_uid, source_agent_id, skill_name)
                target_skill_path = target_dir / SKILL_FILENAME

                if target_skill_path.exists():
                    # Update: overwrite SKILL.md, merge companion files
                    shutil.copytree(str(source_dir), str(target_dir), dirs_exist_ok=True)
                    self.update_skill(target_uid, source_agent_id, skill_name, content)
                else:
                    # Create: copy entire skill dir as companion files, then write SKILL.md
                    self.create_skill(
                        target_uid,
                        source_agent_id,
                        skill_name,
                        content,
                        companion_files_dir=source_dir
                    )
                results[target_uid] = True
            except Exception as e:
                logger.error(f"Failed to broadcast skill to {target_uid}: {e}")
                results[target_uid] = False

        return results
