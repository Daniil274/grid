"""
Core modules of the Grid agent system.
"""

from __future__ import annotations

import importlib
import sys

__version__ = "0.1.0"

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

_LEGACY_MODULE_ALIASES = {
    "core.condition_evaluator": "core.platform.conditions",
    "core.event_bus": "core.platform.events",
    "core.events": "core.tracing.events",
    "core.tracing_config": "core.tracing.config",
    "core.timeline_tracer": "core.tracing.tracer",
    "core.pipeline_registry": "core.tracing.pipeline_registry",
    "core.config_proposer": "core.improvement.proposer",
    "core.config_apply": "core.improvement.config_apply",
    "core.evaluator": "core.improvement.evaluator",
    "core.improvement_monitor": "core.improvement.monitor",
    "core.improvement_registry": "core.improvement.registry",
    "core.log_observer": "core.improvement.log_observer",
    "core.embeddings": "core.memory.embeddings",
    "core.memory_optimizer": "core.memory.optimizer",
    "core.memory_store": "core.memory.store",
    "core.unified_memory": "core.memory.unified",
    "core.prompt_sections": "core.config.prompt_sections",
    "core.protocols": "core.config.protocols",
    "core.system_builder": "core.platform.builder",
    "core.system_compiler": "core.platform.compiler",
    "core.system_governance": "core.platform.governance",
    "core.system_mutation": "core.platform.mutation",
    "core.system_registry": "core.platform.registry",
    "core.system_release_manager": "core.platform.release",
    "core.system_runtime": "core.platform.runtime",
    "core.system_workbench": "core.platform.workbench",
}

def _install_legacy_module_aliases() -> None:
    for alias_name, target_name in _LEGACY_MODULE_ALIASES.items():
        if alias_name not in sys.modules:
            sys.modules[alias_name] = importlib.import_module(target_name)


_install_legacy_module_aliases()


def __getattr__(name):
    if name == "AgentFactory":
        from .agent_factory import AgentFactory
        return AgentFactory
    if name == "Config":
        from .config.config import Config
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
