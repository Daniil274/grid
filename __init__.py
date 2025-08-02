"""
Grid - Enterprise Agent Factory System.

Модульная система агентов на базе OpenAI Agents SDK с возможностями:
- Конфигурируемые агенты из YAML
- Агенты как инструменты
- MCP (Model Context Protocol) интеграция  
- Управление контекстом и памятью
- Enterprise-grade логирование и трассировка
"""

__version__ = "0.1.0"
__author__ = "Grid Development Team"

from .core.agent_factory import AgentFactory
from .core.config import Config
from .core.context import ContextManager

__all__ = [
    "AgentFactory",
    "Config", 
    "ContextManager",
]