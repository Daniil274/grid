"""
Skill Tools - Manage file-based agent skills in the workspace.

Primary compatibility tools:
- skill_list:   List all available skills for the current agent
- skill_read:   Read a skill and show its workspace directory
- skill_create: Register a skill from a file in the working directory
- skill_update: Update an existing registered skill
- skill_delete: Delete a skill and its files

Compatibility aliases:
- skill_save:   Upsert by raw content
- skill_search: Search indexed skills
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional

from agents import RunContextWrapper, function_tool

from utils.path_utils import display_agent_path, resolve_agent_path

logger = logging.getLogger(__name__)


def _get_skill_manager(context: RunContextWrapper) -> Any:
    try:
        raw = getattr(context, "context", None)
        if raw is None:
            return None
        factory = getattr(raw, "factory", None)
        if factory is None:
            return None
        return getattr(factory, "skill_manager", None)
    except Exception as exc:
        logger.error("Failed to get skill_manager: %s", exc)
        return None


def _get_factory(context: RunContextWrapper) -> Any:
    try:
        raw = getattr(context, "context", None)
        if raw is None:
            return None
        return getattr(raw, "factory", None)
    except Exception:
        return None


def _get_user_id(context: RunContextWrapper) -> str:
    try:
        raw = getattr(context, "context", None)
        if raw:
            user_id = getattr(raw, "user_id", None)
            if user_id:
                return user_id
    except Exception:
        pass
    return "default_user"


def _get_agent_id(context: RunContextWrapper) -> str:
    try:
        raw = getattr(context, "context", None)
        if raw:
            agent_id = getattr(raw, "agent_id", None)
            if agent_id:
                return agent_id
    except Exception:
        pass
    return "default_agent"


@function_tool
async def skill_list(context: RunContextWrapper) -> str:
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        names = manager.list_skills(user_id, agent_id)
        if not names:
            return "ℹ️ No skills available."
        return "📚 Available skills:\n" + "\n".join(f"- {name}" for name in names)
    except Exception as exc:
        return f"❌ Error listing skills: {exc}"


@function_tool
async def skill_read(context: RunContextWrapper, name: str) -> str:
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        content = manager.get_skill(user_id, agent_id, name)
        if not content:
            return f"❌ Skill '{name}' not found."

        skill_dir = manager.get_skill_dir_path(user_id, agent_id, name)
        dir_line = ""
        if skill_dir:
            dir_line = f"📁 Skill directory: {display_agent_path(str(skill_dir), _get_factory(context))}"

        parts = [f"📚 Skill: {name}"]
        if dir_line:
            parts.append(dir_line)
        parts.append("")
        parts.append(content)
        return "\n".join(parts)
    except Exception as exc:
        return f"❌ Error reading skill: {exc}"


@function_tool
async def skill_create(
    context: RunContextWrapper,
    name: str,
    file_path: str,
    companion_files: str = "",
    tags: str = "",
) -> str:
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)
    factory = _get_factory(context)

    try:
        resolved_path = resolve_agent_path(file_path, factory)
        visible_path = display_agent_path(file_path, factory)
        content = Path(resolved_path).read_text(encoding="utf-8")

        companion_dir: Optional[Path] = None
        if companion_files.strip():
            companion_resolved = resolve_agent_path(companion_files.strip(), factory)
            companion_dir = Path(companion_resolved)
            if not companion_dir.is_dir():
                visible_companion = display_agent_path(companion_files.strip(), factory)
                return f"❌ companion_files '{visible_companion}' is not a directory"

        manager.create_skill(
            user_id,
            agent_id,
            name,
            content,
            tags=tags,
            companion_files_dir=companion_dir,
        )

        skill_dir = manager.get_skill_dir_path(user_id, agent_id, name)
        rel_dir = display_agent_path(str(skill_dir), factory) if skill_dir else name
        message = f"✅ Skill '{name}' registered.\n📁 Skill directory: {rel_dir}"
        message += f"\n📄 Source file: {visible_path}"
        if companion_dir:
            message += f"\n📦 Companion files: {display_agent_path(companion_files.strip(), factory)}"
        return message
    except FileExistsError:
        return f"❌ Skill '{name}' already exists."
    except Exception as exc:
        return f"❌ Error creating skill: {exc}"


@function_tool
async def skill_search(
    context: RunContextWrapper,
    query: str = "",
    tags: str = "",
    limit: int = 10,
) -> str:
    manager = _get_skill_manager(context)
    if not manager:
        return json.dumps({"error": "skill_manager not available"})

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        entries = manager.search_skills(user_id, agent_id, query, limit=limit)
        if tags:
            required = [tag.strip() for tag in tags.split(",") if tag.strip()]
            entries = [
                entry for entry in entries
                if all(tag in (entry.tags or "") for tag in required)
            ]
        return json.dumps(
            [
                {
                    "id": entry.id,
                    "name": next(
                        (tag.split(":", 1)[1] for tag in (entry.tags or "").split(",") if tag.startswith("skill:")),
                        "",
                    ),
                    "summary": entry.summary,
                    "tags": entry.tags,
                    "agent_id": entry.agent_id,
                    "updated_at": entry.updated_at,
                }
                for entry in entries[:limit]
            ],
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


@function_tool
async def skill_save(
    context: RunContextWrapper,
    name: str,
    content: str,
    tags: str = "",
) -> str:
    manager = _get_skill_manager(context)
    if not manager:
        return json.dumps({"error": "skill_manager not available"})

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)
    skill_id = manager.save(name, content, tags=tags, user_id=user_id, agent_id=agent_id)
    return json.dumps({"ok": True, "id": skill_id, "name": name}, ensure_ascii=False)


@function_tool
async def skill_update(
    context: RunContextWrapper,
    name: str,
    new_name: Optional[str] = None,
    content: Optional[str] = None,
    tags: Optional[str] = None,
) -> str:
    manager = _get_skill_manager(context)
    if not manager:
        return json.dumps({"error": "skill_manager not available"})

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    existing = manager.get_skill(user_id, agent_id, name)
    if existing is None:
        return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)

    try:
        new_skill_name = new_name or name
        if new_name and new_name != name:
            old_dir = manager.get_skill_dir_path(user_id, agent_id, name)
            if not old_dir:
                return json.dumps({"error": f"Skill '{name}' not found"}, ensure_ascii=False)
            new_dir = old_dir.parent / new_name
            if new_dir.exists():
                return json.dumps({"error": f"Skill '{new_name}' already exists"}, ensure_ascii=False)
            old_dir.rename(new_dir)

        updated = manager.update_skill(
            user_id,
            agent_id,
            new_skill_name,
            content if content is not None else existing,
            tags=tags,
        )
        return json.dumps({"ok": updated, "name": new_skill_name}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


@function_tool
async def skill_delete(context: RunContextWrapper, name: str) -> str:
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        deleted = manager.delete_skill(user_id, agent_id, name)
        if deleted:
            return f"✅ Skill '{name}' deleted."
        return f"❌ Skill '{name}' not found."
    except Exception as exc:
        return f"❌ Error deleting skill: {exc}"


SKILL_TOOLS = {
    "skill_list": skill_list,
    "skill_read": skill_read,
    "skill_create": skill_create,
    "skill_save": skill_save,
    "skill_search": skill_search,
    "skill_update": skill_update,
    "skill_delete": skill_delete,
}
