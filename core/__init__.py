"""
Core modules of the Grid agent system.
"""

__all__ = [
    "AgentFactory",
    "Config",
    "ContextManager",
    "SystemRegistry",
    "SystemRuntime",
    "SystemCompiler",
    "SystemMutator",
    "SystemWorkbench",
    "TaskOntology",
    "CapabilityRegistry",
    "PatternRegistry",
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
    if name == "SystemRegistry":
        from .platform import SystemRegistry
        return SystemRegistry
    if name == "SystemRuntime":
        from .platform import SystemRuntime
        return SystemRuntime
    if name == "SystemCompiler":
        from .platform import SystemCompiler
        return SystemCompiler
    if name == "SystemMutator":
        from .platform import SystemMutator
        return SystemMutator
    if name == "SystemWorkbench":
        from .platform import SystemWorkbench
        return SystemWorkbench
    if name == "TaskOntology":
        from .cognition import TaskOntology
        return TaskOntology
    if name == "CapabilityRegistry":
        from .cognition import CapabilityRegistry
        return CapabilityRegistry
    if name == "PatternRegistry":
        from .cognition import PatternRegistry
        return PatternRegistry
    raise AttributeError(name)
