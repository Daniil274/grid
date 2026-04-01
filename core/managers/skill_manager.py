"""
Skill Manager - Handles file-based skills with database synchronization.

Storage layout:
    {working_directory}/{user_id}/agents/{agent_id}/skills/{skill_name}/SKILL.md

Filesystem is the source of truth. Database entries are maintained only for
search/indexing compatibility.
"""

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.memory_store import MemoryStore

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
SKILL_FILENAME_LEGACY = "skill.md"


class SkillManager:
    """Manage skill files per user/agent inside the configured workspace."""

    def __init__(self, memory_store: MemoryStore, workspace_root: Any = None):
        self.memory_store = memory_store
        self.workspace_root = Path(workspace_root or ".")

    def _sanitize_path_component(self, value: str, fallback: str) -> str:
        safe_value = "".join(c for c in value if c.isalnum() or c in ("-", "_")).strip()
        return safe_value or fallback

    def _extract_summary(self, content: str) -> str:
        for line in content.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:200]
        return ""

    def _get_agent_skills_dir(self, user_id: str, agent_id: str) -> Path:
        safe_user_id = self._sanitize_path_component(user_id, "default_user")
        safe_agent_id = self._sanitize_path_component(agent_id, "default_agent")
        return self.workspace_root / safe_user_id / "agents" / safe_agent_id / "skills"

    def _get_skill_dir(self, user_id: str, agent_id: str, skill_name: str) -> Path:
        safe_name = self._sanitize_path_component(skill_name, "skill")
        return self._get_agent_skills_dir(user_id, agent_id) / safe_name

    def _get_skill_path(self, user_id: str, agent_id: str, skill_name: str) -> Path:
        return self._get_skill_dir(user_id, agent_id, skill_name) / SKILL_FILENAME

    def _find_skill_md(self, skill_dir: Path) -> Optional[Path]:
        standard = skill_dir / SKILL_FILENAME
        if standard.exists():
            return standard
        legacy = skill_dir / SKILL_FILENAME_LEGACY
        if legacy.exists():
            return legacy
        return None

    def _db_entries_for_skill(self, user_id: str, agent_id: str, skill_name: str) -> List[Any]:
        entries = self.memory_store.search(
            type="skill",
            user_id=user_id,
            agent_id=agent_id,
            limit=1000,
        )
        tag = f"skill:{skill_name}"
        return [entry for entry in entries if tag in (entry.tags or "").split(",")]

    def _sync_skill_to_db(self, user_id: str, agent_id: str, skill_name: str, content: str, tags: str = "") -> None:
        summary = self._extract_summary(content)
        db_tags = ",".join(part for part in [tags.strip(","), f"skill:{skill_name}"] if part).strip(",")
        existing = self._db_entries_for_skill(user_id, agent_id, skill_name)
        if existing:
            self.memory_store.update(existing[0].id, content=content, summary=summary)
            return
        self.memory_store.save(
            content=content,
            type="skill",
            tags=db_tags,
            summary=summary,
            importance=1.0,
            user_id=user_id,
            agent_id=agent_id,
        )

    def get_skill_dir_path(self, user_id: str, agent_id: str, skill_name: str) -> Optional[Path]:
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        if self._find_skill_md(skill_dir):
            return skill_dir
        return None

    def create_skill(
        self,
        user_id: str,
        agent_id: str,
        skill_name: str,
        content: str,
        tags: str = "",
        companion_files_dir: Optional[Path] = None,
    ) -> str:
        skill_path = self._get_skill_path(user_id, agent_id, skill_name)
        if self._find_skill_md(skill_path.parent):
            raise FileExistsError(
                f"Skill '{skill_name}' already exists for user {user_id} and agent {agent_id}"
            )

        skill_path.parent.mkdir(parents=True, exist_ok=True)

        if companion_files_dir:
            companion_path = Path(companion_files_dir)
            if companion_path.is_dir():
                shutil.copytree(str(companion_path), str(skill_path.parent), dirs_exist_ok=True)
            else:
                logger.warning("companion_files_dir '%s' is not a directory, skipping", companion_path)

        skill_path.write_text(content, encoding="utf-8")
        self._sync_skill_to_db(user_id, agent_id, skill_name, content, tags)
        logger.info("✅ Created skill '%s' for user %s and agent %s", skill_name, user_id, agent_id)
        return skill_name

    def update_skill(
        self,
        user_id: str,
        agent_id: str,
        skill_name: str,
        content: str,
        tags: Optional[str] = None,
    ) -> bool:
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        skill_md = self._find_skill_md(skill_dir)
        if not skill_md:
            return False

        skill_path = self._get_skill_path(user_id, agent_id, skill_name)
        skill_path.write_text(content, encoding="utf-8")

        legacy_path = skill_dir / SKILL_FILENAME_LEGACY
        if legacy_path.exists() and legacy_path != skill_path:
            legacy_path.unlink()

        existing_tags = ""
        existing = self._db_entries_for_skill(user_id, agent_id, skill_name)
        if existing:
            existing_tags = ",".join(
                part for part in (existing[0].tags or "").split(",") if part and part != f"skill:{skill_name}"
            )

        self._sync_skill_to_db(user_id, agent_id, skill_name, content, tags if tags is not None else existing_tags)
        logger.info("✏️ Updated skill '%s' for user %s and agent %s", skill_name, user_id, agent_id)
        return True

    def delete_skill(self, user_id: str, agent_id: str, skill_name: str) -> bool:
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        if not self._find_skill_md(skill_dir):
            return False

        try:
            shutil.rmtree(skill_dir)
        except OSError as exc:
            logger.error("Failed to delete skill directory '%s': %s", skill_dir, exc)
            return False

        for entry in self._db_entries_for_skill(user_id, agent_id, skill_name):
            self.memory_store.delete(entry.id, hard=True)

        logger.info("🗑️ Deleted skill '%s' for user %s and agent %s", skill_name, user_id, agent_id)
        return True

    def get_skill(self, user_id: str, agent_id: str, skill_name: str) -> Optional[str]:
        skill_md = self._find_skill_md(self._get_skill_dir(user_id, agent_id, skill_name))
        if not skill_md:
            return None
        return skill_md.read_text(encoding="utf-8")

    def get_skill_files(self, user_id: str, agent_id: str, skill_name: str) -> List[str]:
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        if not skill_dir.exists():
            return []

        files = []
        for file_path in skill_dir.rglob("*"):
            if file_path.is_file() and file_path.name not in (SKILL_FILENAME, SKILL_FILENAME_LEGACY):
                files.append(str(file_path.relative_to(skill_dir)).replace("\\", "/"))
        return sorted(files)

    def read_skill_file(self, user_id: str, agent_id: str, skill_name: str, relative_path: str) -> Optional[str]:
        skill_dir = self._get_skill_dir(user_id, agent_id, skill_name)
        target = (skill_dir / relative_path).resolve()
        if not str(target).startswith(str(skill_dir.resolve())):
            logger.warning("Path traversal attempt blocked: %s", relative_path)
            return None
        if not target.exists() or not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    def list_skills(self, user_id: str, agent_id: str) -> List[str]:
        skills_dir = self._get_agent_skills_dir(user_id, agent_id)
        if not skills_dir.exists():
            return []

        skills = []
        for item in skills_dir.iterdir():
            if item.is_dir() and self._find_skill_md(item):
                skills.append(item.name)
        return sorted(skills)

    def search_skills(self, user_id: str, agent_id: str, query: str, limit: int = 10) -> List[Any]:
        return self.memory_store.search(
            query=query,
            type="skill",
            user_id=user_id,
            agent_id=agent_id,
            limit=limit,
        )

    def sync_skills_to_db(self, user_id: str, agent_id: str) -> None:
        skills = self.list_skills(user_id, agent_id)
        for skill_name in skills:
            content = self.get_skill(user_id, agent_id, skill_name)
            if content is not None:
                self._sync_skill_to_db(user_id, agent_id, skill_name, content)

        for entry in self.memory_store.search(type="skill", user_id=user_id, agent_id=agent_id, limit=1000):
            tags = (entry.tags or "").split(",")
            skill_tag = next((tag for tag in tags if tag.startswith("skill:")), None)
            if skill_tag:
                name = skill_tag.split(":", 1)[1]
                if name not in skills:
                    self.memory_store.delete(entry.id, hard=True)
                    logger.info("Removed orphaned DB entry for skill '%s'", name)

    def broadcast_skill(
        self,
        source_user_id: str,
        source_agent_id: str,
        skill_name: str,
        target_user_ids: List[str],
    ) -> Dict[str, bool]:
        source_dir = self._get_skill_dir(source_user_id, source_agent_id, skill_name)
        content = self.get_skill(source_user_id, source_agent_id, skill_name)
        if content is None:
            return {user_id: False for user_id in target_user_ids}

        results: Dict[str, bool] = {}
        for target_user_id in target_user_ids:
            try:
                target_skill_path = self._get_skill_path(target_user_id, source_agent_id, skill_name)
                if target_skill_path.exists():
                    shutil.copytree(str(source_dir), str(target_skill_path.parent), dirs_exist_ok=True)
                    self.update_skill(target_user_id, source_agent_id, skill_name, content)
                else:
                    self.create_skill(
                        target_user_id,
                        source_agent_id,
                        skill_name,
                        content,
                        companion_files_dir=source_dir,
                    )
                results[target_user_id] = True
            except Exception as exc:
                logger.error("Failed to broadcast skill '%s' to %s: %s", skill_name, target_user_id, exc)
                results[target_user_id] = False
        return results

    # Compatibility wrappers for the newer SQL-only API names.
    def save(
        self,
        name: str,
        content: str,
        tags: str = "",
        user_id: str = "default_user",
        agent_id: str = "default_agent",
    ) -> int:
        if self.get_skill(user_id, agent_id, name) is None:
            self.create_skill(user_id, agent_id, name, content, tags=tags)
        else:
            self.update_skill(user_id, agent_id, name, content, tags=tags)
        entries = self._db_entries_for_skill(user_id, agent_id, name)
        return entries[0].id if entries else 0

    def get(
        self,
        name: str,
        user_id: str = "default_user",
        agent_id: str = "default_agent",
    ) -> Optional[Any]:
        content = self.get_skill(user_id, agent_id, name)
        if content is None:
            return None
        entries = self._db_entries_for_skill(user_id, agent_id, name)
        return entries[0] if entries else None

    def search(
        self,
        query: str = "",
        tags: str = "",
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Any]:
        if user_id is None:
            return []
        return self.memory_store.search_skills(
            query=query,
            tags=tags,
            user_id=user_id,
            agent_id=agent_id,
            limit=limit,
        )

    def update(
        self,
        skill_id: int,
        name: Optional[str] = None,
        content: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> bool:
        with self.memory_store._get_connection() as conn:
            row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if not row:
            return False

        current_name = row["name"]
        user_id = row["user_id"]
        agent_id = row["agent_id"]
        old_dir = self._get_skill_dir(user_id, agent_id, current_name)
        new_name = name or current_name

        if name and name != current_name:
            new_dir = self._get_skill_dir(user_id, agent_id, new_name)
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_dir), str(new_dir))
            for entry in self._db_entries_for_skill(user_id, agent_id, current_name):
                new_tags = ",".join(
                    new_name if part == current_name else part
                    for part in (entry.tags or "").split(",")
                ).replace(f"skill:{current_name}", f"skill:{new_name}")
                self.memory_store.delete(entry.id, hard=True)
                self.memory_store.save(
                    content=content if content is not None else row["content"],
                    type="skill",
                    tags=new_tags,
                    summary=self._extract_summary(content if content is not None else row["content"]),
                    importance=1.0,
                    user_id=user_id,
                    agent_id=agent_id,
                )

        if content is not None:
            return self.update_skill(user_id, agent_id, new_name, content, tags=tags)

        if tags is not None:
            skill_content = self.get_skill(user_id, agent_id, new_name)
            if skill_content is not None:
                self._sync_skill_to_db(user_id, agent_id, new_name, skill_content, tags)
                return True
        return True

    def delete(self, skill_id: int) -> bool:
        with self.memory_store._get_connection() as conn:
            row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if not row:
            return False
        return self.delete_skill(row["user_id"], row["agent_id"], row["name"])
