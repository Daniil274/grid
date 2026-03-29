"""
Core modules of the Grid agent system.
"""

__all__ = [
    "AgentFactory",
    "Config",
    "ContextManager"
]


def __getattr__(name):
    if name == "AgentFactory":
        from .agent_factory import AgentFactory
        return AgentFactory
    if name == "Config":
        from .config import Config
        return Config
    if name == "ContextManager":
        from .context import ContextManager
        return ContextManager
    raise AttributeError(name)
