"""
Tool Manager for handling agent tools.
"""

import logging
from typing import List, Any, TYPE_CHECKING
from core.config.protocols import IToolManager

if TYPE_CHECKING:
    from core.config.config import Config
    from core.agent_factory import AgentFactory

logger = logging.getLogger("grid.managers.tool")


class ToolManager:
    """
    Manager for agent tools.
    Implements IToolManager protocol.
    """

    def __init__(self, config: 'Config', factory: 'AgentFactory'):
        """
        Initialize ToolManager.

        Args:
            config: Configuration instance
            factory: AgentFactory instance (for creating agent-tools)
        """
        self.config = config
        self.factory = factory
        self._tool_cache = {}

    async def get_agent_tools(self, agent_config: Any) -> List[Any]:
        """
        Get all tools for agent with caching.

        Args:
            agent_config: Agent configuration

        Returns:
            List of tool instances
        """
        # TODO: Implement full logic or migrate from AgentFactory
        return []

    async def create_agent_tools(self, agent_keys: List[str]) -> List[Any]:
        """
        Create agent tools with proper logging and context sharing.

        Args:
            agent_keys: List of agent keys to create tools for

        Returns:
            List of agent tool instances
        """
        # TODO: Implement full logic or migrate from AgentFactory
        return []




