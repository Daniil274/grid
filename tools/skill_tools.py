"""
Skill Tools - Manage agent skills (knowledge/capabilities).

Tools:
- skill_create: Create a new skill
- skill_update: Update an existing skill
- skill_delete: Delete a skill
- skill_read: Read skill content
- skill_search: Search for skills
- skill_broadcast: Share skill with other users
"""

import logging
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
            return "default_user"
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
            return "default_agent"
    except Exception:
        pass
    return "default_agent"


@function_tool
async def skill_create(
    context: RunContextWrapper,
    name: str,
    file_path: str,
    tags: str = ""
) -> str:
    """
    Register a new skill from a file.

    The agent should first create the skill file (using write_file) in a temporary or skills directory,
    and then call this tool to register/index it as a formal skill.
    The content will be copied to the standard skill storage.

    Args:
        name: Skill name (e.g., "python-best-practices", "deployment-guide")
        file_path: Path to the markdown file containing the skill content
        tags: Comma-separated tags (e.g., "python,coding,guide")

    Returns:
        Confirmation message
    """
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        visible_path = display_agent_path(file_path, _get_factory(context))
        resolved_path = resolve_agent_path(file_path, _get_factory(context))
        try:
            with open(resolved_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return f"❌ Error reading skill file from '{visible_path}': {e}"

        manager.create_skill(user_id, agent_id, name, content, tags)
        return f"✅ Skill '{name}' created/registered successfully from '{visible_path}'."
    except FileExistsError:
        return f"❌ Skill '{name}' already exists. Use skill_update to modify it."
    except Exception as e:
        return f"❌ Error creating skill: {e}"


@function_tool
async def skill_update(
    context: RunContextWrapper,
    name: str,
    content: str
) -> str:
    """
    Update an existing skill.

    Args:
        name: Skill name
        content: New markdown content

    Returns:
        Confirmation message
    """
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        success = manager.update_skill(user_id, agent_id, name, content)
        if success:
            return f"✅ Skill '{name}' updated successfully."
        else:
            return f"❌ Skill '{name}' not found."
    except Exception as e:
        return f"❌ Error updating skill: {e}"


@function_tool
async def skill_delete(
    context: RunContextWrapper,
    name: str
) -> str:
    """
    Delete a skill.

    Args:
        name: Skill name

    Returns:
        Confirmation message
    """
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        success = manager.delete_skill(user_id, agent_id, name)
        if success:
            return f"✅ Skill '{name}' deleted successfully."
        else:
            return f"❌ Skill '{name}' not found."
    except Exception as e:
        return f"❌ Error deleting skill: {e}"


@function_tool
async def skill_read(
    context: RunContextWrapper,
    name: str
) -> str:
    """
    Read the content of a skill.

    Args:
        name: Skill name

    Returns:
        Skill content or error message
    """
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        content = manager.get_skill(user_id, agent_id, name)
        if content:
            return f"📚 Skill: {name}\n\n{content}"
        else:
            return f"❌ Skill '{name}' not found."
    except Exception as e:
        return f"❌ Error reading skill: {e}"


@function_tool
async def skill_search(
    context: RunContextWrapper,
    query: str
) -> str:
    """
    Search for skills by keywords.

    Args:
        query: Search keywords

    Returns:
        List of matching skills
    """
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)

    try:
        results = manager.search_skills(user_id, agent_id, query, limit=10)

        if not results:
            return f"ℹ️ No skills found for '{query}'"

        lines = [f"🔍 Found {len(results)} skills for '{query}':", ""]
        for entry in results:
            tags = entry.tags.split(",")
            name = "unknown"
            for t in tags:
                if t.startswith("skill:"):
                    name = t.split(":", 1)[1]
                    break

            lines.append(f"- {name} (ID: {entry.id})")
            snippet = entry.content[:100].replace("\n", " ") + "..."
            lines.append(f"  {snippet}")

        return "\n".join(lines)

    except Exception as e:
        return f"❌ Error searching skills: {e}"


@function_tool
async def skill_list(context: RunContextWrapper) -> str:
    """
    List all available skill names for the current user.

    Returns only names, not content. Use skill_read(name) to get the full content of a skill.

    Returns:
        Newline-separated list of skill names, or a message if no skills exist.
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
async def skill_broadcast(
    context: RunContextWrapper,
    name: str,
    target_users: str
) -> str:
    """
    Share/copy a skill to other users.

    Args:
        name: Skill name
        target_users: Comma-separated list of user IDs

    Returns:
        Result summary
    """
    manager = _get_skill_manager(context)
    if not manager:
        return "❌ Skill manager not available"

    user_id = _get_user_id(context)
    agent_id = _get_agent_id(context)
    targets = [u.strip() for u in target_users.split(",") if u.strip()]

    if not targets:
        return "❌ No target users specified"

    try:
        results = manager.broadcast_skill(user_id, agent_id, name, targets)

        success_count = sum(1 for v in results.values() if v)
        fail_count = len(results) - success_count

        msg = f"📢 Broadcast '{name}': {success_count} success, {fail_count} failed."
        if fail_count > 0:
            failed = [u for u, v in results.items() if not v]
            msg += f"\nFailed for: {', '.join(failed)}"

        return msg
    except Exception as e:
        return f"❌ Error broadcasting skill: {e}"


# ============================================================================
# TOOL REGISTRY
# ============================================================================

SKILL_TOOLS = {
    "skill_list": skill_list,
    "skill_create": skill_create,
    "skill_update": skill_update,
    "skill_delete": skill_delete,
    "skill_read": skill_read,
    "skill_search": skill_search,
    "skill_broadcast": skill_broadcast,
}
