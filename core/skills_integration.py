"""
Skills Integration - converts nanobot skills into grid tools.

Strategy:
1. Load skills via SkillsLoader (nanobot)
2. Skills with always=true → add to agent instructions
3. Remaining skills → register as "knowledge tools" (function_tool)
4. Validate requirements (bins, env vars)
"""

from pathlib import Path
from typing import Dict, Set, List, Optional
from loguru import logger

# Imports from nanobot
try:
    from nanobot.agent.skills import SkillsLoader
    NANOBOT_AVAILABLE = True
except ImportError:
    logger.warning("Nanobot not found, skills will be unavailable")
    NANOBOT_AVAILABLE = False
    SkillsLoader = None


class SkillsIntegration:
    """
    Converts nanobot skills into grid tools.

    Usage:
        skills_loader = SkillsLoader(
            workspace=Path("./workspace"),
            builtin_skills_dir=Path("./nanobot/nanobot/skills")
        )

        skills_integration = SkillsIntegration(
            skills_loader=skills_loader
        )

        # Register all skills
        skills_integration.register_all_skills()

        # Get always-loaded skills for instructions
        always_content = skills_integration.get_always_loaded_skills_content()

        # Get list of tool names
        tool_names = skills_integration.get_registered_tool_names()
    """

    def __init__(self, skills_loader: Optional[SkillsLoader] = None):
        """
        Args:
            skills_loader: SkillsLoader instance from nanobot
        """
        if not NANOBOT_AVAILABLE or skills_loader is None:
            logger.warning("SkillsLoader unavailable, skills integration disabled")
            self.loader = None
            self._registered_skills: Set[str] = set()
            self._skill_tools: Dict[str, any] = {}
            return

        self.loader = skills_loader
        self._registered_skills: Set[str] = set()
        self._skill_tools: Dict[str, any] = {}  # skill_name → tool function

        logger.info("✅ SkillsIntegration initialized")

    def register_all_skills(self) -> Dict[str, int]:
        """
        Registers all available skills as tools.

        Returns:
            Dict with success_count and failure_count
        """
        if not self.loader:
            logger.warning("SkillsLoader not available, skipping registration")
            return {"success": 0, "failure": 0}

        skills = self.loader.list_skills(filter_unavailable=True)
        success_count = 0
        failure_count = 0

        for skill_info in skills:
            try:
                self._register_skill(skill_info)
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to register skill {skill_info['name']}: {e}")
                failure_count += 1
                # Continue with other skills (resilience)

        logger.info(
            f"✅ Skills registration: {success_count} succeeded, "
            f"❌ {failure_count} failed"
        )

        return {"success": success_count, "failure": failure_count}

    def _register_skill(self, skill_info: Dict[str, str]):
        """
        Converts one skill into a tool function.

        Args:
            skill_info: Dict with 'name', 'path', 'source'
        """
        skill_name = skill_info['name']

        if skill_name in self._registered_skills:
            return

        # Load skill content
        skill_content = self.loader.load_skill(skill_name)
        if not skill_content:
            logger.warning(f"Skill {skill_name} has no content")
            return

        # Extract metadata
        metadata = self.loader.get_skill_metadata(skill_name) or {}

        # Create tool function
        tool_fn = self._create_skill_tool(skill_name, skill_content, metadata)

        # Store tool
        self._skill_tools[skill_name] = tool_fn
        self._registered_skills.add(skill_name)

        logger.debug(f"✅ Registered skill: {skill_name}")

    def _create_skill_tool(
        self,
        skill_name: str,
        skill_content: str,
        metadata: Dict
    ):
        """
        Creates a function_tool from a skill.

        Skill becomes a "knowledge tool" - when the agent calls the tool,
        it receives the skill content for use.

        Args:
            skill_name: Name of the skill
            skill_content: Contents of SKILL.md
            metadata: Metadata from frontmatter

        Returns:
            Tool function (or stub if OpenAI Agents SDK is unavailable)
        """
        # Strip frontmatter from content
        content = self._strip_frontmatter(skill_content)

        # Create tool description
        description = metadata.get('description', f'Access knowledge from skill: {skill_name}')

        # Define tool function
        # Note: Actual registration via @function_tool will happen
        # when the agent factory creates the agent
        def skill_tool(query: Optional[str] = None) -> str:
            """
            Access skill knowledge.

            Args:
                query: Optional query to filter skill content

            Returns:
                Skill content
            """
            if query:
                # Simple keyword filtering
                if query.lower() in content.lower():
                    return f"📚 Skill: {skill_name}\n\n{content}"
                else:
                    return f"📚 Skill: {skill_name}\n\nQuery '{query}' not found in skill. Showing full content:\n\n{content}"
            else:
                return f"📚 Skill: {skill_name}\n\n{content}"

        # Set metadata
        skill_tool.__name__ = f"skill_{skill_name}"
        skill_tool.__doc__ = description

        return skill_tool

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from content"""
        if content.startswith("---"):
            # Find end of frontmatter
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()
        return content

    def get_always_loaded_skills_content(self) -> str:
        """
        Get skills content with always=true for adding to instructions.

        Returns:
            Formatted skills content
        """
        if not self.loader:
            return ""

        always_skills = self.loader.get_always_skills()
        if not always_skills:
            return ""

        return self.loader.load_skills_for_context(always_skills)

    def get_registered_tool_names(self) -> List[str]:
        """
        Get list of registered skill tool names.

        Returns:
            List of tool names (skill_{name})
        """
        return [f"skill_{name}" for name in self._registered_skills]

    def get_skill_tool(self, skill_name: str):
        """
        Get tool function for a specific skill.

        Args:
            skill_name: Skill name (without skill_ prefix)

        Returns:
            Tool function or None
        """
        return self._skill_tools.get(skill_name)

    def get_all_skill_tools(self) -> Dict[str, any]:
        """
        Get all skill tools.

        Returns:
            Dict: skill_name → tool_function
        """
        return self._skill_tools.copy()

    def list_available_skills(self) -> List[Dict[str, str]]:
        """
        List of all available skills (regardless of registration).

        Returns:
            List of skill info dicts
        """
        if not self.loader:
            return []

        return self.loader.list_skills(filter_unavailable=True)

    def get_skills_summary(self) -> str:
        """
        Get a brief summary of all available skills.

        Returns:
            Formatted summary string
        """
        if not self.loader:
            return "Skills unavailable (SkillsLoader not initialized)"

        skills = self.list_available_skills()
        if not skills:
            return "No skills available"

        lines = ["📚 Available skills:"]
        for skill in skills:
            name = skill['name']
            source = skill['source']
            meta = self.loader.get_skill_metadata(name) or {}
            desc = meta.get('description', 'N/A')

            status = "✅" if name in self._registered_skills else "⚪"
            lines.append(f"  {status} {name} ({source}): {desc}")

        return "\n".join(lines)

    def get_stats(self) -> Dict:
        """
        Get skills statistics.

        Returns:
            Dict with statistics
        """
        if not self.loader:
            return {
                "available": False,
                "registered_count": 0,
                "total_count": 0,
                "always_loaded_count": 0,
            }

        all_skills = self.loader.list_skills(filter_unavailable=False)
        available_skills = self.loader.list_skills(filter_unavailable=True)
        always_skills = self.loader.get_always_skills()

        return {
            "available": True,
            "registered_count": len(self._registered_skills),
            "total_count": len(all_skills),
            "available_count": len(available_skills),
            "unavailable_count": len(all_skills) - len(available_skills),
            "always_loaded_count": len(always_skills),
            "always_loaded": always_skills,
            "registered": list(self._registered_skills),
        }
