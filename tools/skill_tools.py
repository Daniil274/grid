"""
Skill Tools - Manage agent skills following the Anthropic standard.

Each skill is a folder with a SKILL.md file and optional companion files:
    {skill_name}/
        SKILL.md          - Main instructions (required)
        scripts/          - Helper scripts (optional)
        templates/        - Template files (optional)
        reference/        - Reference documentation (optional)
        examples/         - Example files (optional)

Tools:
- skill_list:   List all available skills
- skill_read:   Read skill's SKILL.md + directory path (for accessing companion files)
- skill_create: Register a new skill from a SKILL.md file + optional companion files dir
- skill_delete: Delete a skill and all its files
"""

import logging
from pathlib import Path
from typing import Any, Optional
from agents import function_tool, RunContextWrapper
from utils.path_utils import display_agent_path, resolve_agent_path

logger = logging.getLogger(__name__)


def _get_skill_manager(context: RunContextWrapper) -> Any:
    """Get SkillManager from context."""
    try:
        raw = getattr(context, "context", None)
        if raw is None:
            return None
        factory = getattr(raw, "factory", None)
        if factory is None:
            return None
        return getattr(factory, "skill_manager", None)
    except Exception as e:
        logger.error(f"❌ Failed to get skill_manager: {e}")
        return None


def _get_factory(context: RunContextWrapper) -> Any:
    """Get AgentFactory from context."""
    try:
        raw = getattr(context, "context", None)
        if raw is None:
            return None
        return getattr(raw, "factory", None)
    except Exception:
        return None


def _get_user_id(context: RunContextWrapper) -> str:
    """Get user_id from context."""
    try:
        raw = getattr(context, "context", None)
        if raw:
            uid = getattr(raw, "user_id", None)
            if uid:
                return uid
    except Exception:
        pass
    return "default_user"


def _get_agent_id(context: RunContextWrapper) -> str:
    """Get agent_id from context."""
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
    """
    List all available skill names for the current user.

    Returns only names. Use skill_read(name) to get full content and directory path.

    Returns:
        List of skill names, or a message if no skills exist.
    """
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        names = manager.list_skills(user_id, agent_id)
        if not names:
            return "ℹ️ No skills available."
        return "📚 Available skills:\n" + "\n".join(f"- {n}" for n in names)
    except Exception as e:
        return f"❌ Error listing skills: {e}"


@function_tool
async def skill_read(
    context: RunContextWrapper,
    name: str
) -> str:
    """
    Read a skill's SKILL.md content and get its directory path.

    The directory path allows the agent to access companion files (scripts,
    templates, references, etc.) directly using file tools.

    Args:
        name: Skill name

    Returns:
        Skill directory path, followed by SKILL.md content.
    """
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
            rel = display_agent_path(str(skill_dir), _get_factory(context))
            dir_line = f"📁 Skill directory: {rel}"

        parts = [f"📚 Skill: {name}"]
        if dir_line:
            parts.append(dir_line)
        parts.append("")
        parts.append(content)
        return "\n".join(parts)
    except Exception as e:
        return f"❌ Error reading skill: {e}"


@function_tool
async def skill_create(
    context: RunContextWrapper,
    name: str,
    file_path: str,
    companion_files: str = "",
    tags: str = ""
) -> str:
    """
    Register a new skill from a SKILL.md file, following the Anthropic standard.

    The agent should first create the SKILL.md file (using write_file), then call
    this tool to register it. The content is copied to standard skill storage.

    Companion files (scripts, templates, references, etc.) can be provided as a
    directory. Its contents are copied into the skill folder alongside SKILL.md:
        {name}/
            SKILL.md
            scripts/     <- from companion_files dir
            templates/   <- from companion_files dir

    Args:
        name: Skill name (e.g., "python-best-practices", "deployment-guide")
        file_path: Path to the SKILL.md file
        companion_files: Optional path to a directory of companion files to bundle
        tags: Comma-separated tags (e.g., "python,coding,guide")

    Returns:
        Confirmation message with the skill's directory path.
    """
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)
    factory = _get_factory(context)

    try:
        visible_path = display_agent_path(file_path, factory)
        resolved_path = resolve_agent_path(file_path, factory)
        try:
            with open(resolved_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return f"❌ Error reading skill file from '{visible_path}': {e}"

        companion_dir: Optional[Path] = None
        if companion_files and companion_files.strip():
            resolved_companion = resolve_agent_path(companion_files.strip(), factory)
            companion_dir = Path(resolved_companion)
            if not companion_dir.is_dir():
                return f"❌ companion_files '{display_agent_path(companion_files.strip(), factory)}' is not a directory"

        manager.create_skill(user_id, agent_id, name, content, tags, companion_files_dir=companion_dir)

        skill_dir = manager.get_skill_dir_path(user_id, agent_id, name)
        rel_dir = display_agent_path(str(skill_dir), factory) if skill_dir else name
        msg = f"✅ Skill '{name}' registered.\n📁 Skill directory: {rel_dir}"
        if companion_dir:
            msg += f"\n   Companion files bundled from: {display_agent_path(companion_files.strip(), factory)}"
        return msg

    except FileExistsError:
        return f"❌ Skill '{name}' already exists."
    except Exception as e:
        return f"❌ Error creating skill: {e}"


@function_tool
async def skill_delete(
    context: RunContextWrapper,
    name: str
) -> str:
    """
    Delete a skill and all its files permanently.

    Args:
        name: Skill name to delete

    Returns:
        Confirmation message or error.
    """
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
    except Exception as e:
        return f"❌ Error deleting skill: {e}"


# ============================================================================
# TOOL REGISTRY
# ============================================================================

SKILL_TOOLS = {
    "skill_list": skill_list,
    "skill_read": skill_read,
    "skill_create": skill_create,
    "skill_delete": skill_delete,
}
