"""Configuration subsystem: config, prompts, protocols."""

from .config import Config
from .prompt_sections import PromptSection, ModelContextAssembly
from .protocols import IConfig, IContextManager

__all__ = [
    "Config",
    "PromptSection",
    "ModelContextAssembly",
    "IConfig",
    "IContextManager",
]