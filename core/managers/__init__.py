"""
Managers package for agent system components.

Collaborators of AgentFactory (core.agent_factory) through AgentRuntimeSupport:
sessions, models, instructions; plus containers and project tools.
"""

from .session_manager import SessionManager
from .instructions_builder import InstructionsBuilder
from .model_manager import ModelManager

__all__ = [
    "SessionManager",
    "InstructionsBuilder",
    "ModelManager",
]
