"""
System Introspection Tools - Meta-information and self-discovery for the Grid agent system.

This module provides tools that allow agents to discover and understand the system
they are running in, including:
- Available agents and their capabilities
- Available tools and their parameters
- System configuration and context
- Skills and knowledge bases

Architecture:
    SystemIntrospector (singleton) - Core logic for system introspection
    ├── get_agents() - List/filter agents
    ├── get_agent_details() - Full agent information
    ├── get_tools() - List/filter tools
    ├── get_tool_details() - Full tool information
    ├── get_skills() - Available skills
    └── get_context() - Execution context

    Tools (@function_tool) - SDK integration layer
    └── Thin wrappers that extract context and call SystemIntrospector

Design Principles:
    1. Separation of concerns: Logic in SystemIntrospector, SDK integration in tools
    2. Fail-safe: Never crash, always return structured error responses
    3. Lazy loading: No expensive operations until needed
    4. Type safety: Full typing with dataclasses for responses
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agents import function_tool, RunContextWrapper

if TYPE_CHECKING:
    from core.agent_factory import AgentFactory
    from core.config import Config

logger = logging.getLogger(__name__)


# ============================================================================
# DATA STRUCTURES
# ============================================================================

class ToolCategory(str, Enum):
    """Tool categories for organization."""
    FILE = "file"
    GIT = "git"
    MEMORY = "memory"
    SKILL = "skill"
    ORCHESTRATION = "orchestration"
    SYSTEM = "system"
    VISION = "vision"
    VOICE = "voice"
    INPUT = "input"
    SCREEN = "screen"
    DOCUMENT = "document"
    TASKS = "tasks"
    EMERGENCY = "emergency"
    OTHER = "other"


class ToolSource(str, Enum):
    """Source of a tool."""
    SYSTEM = "system"
    PROJECT = "project"
    MCP = "mcp"


@dataclass
class AgentSummary:
    """Summary information about an agent."""
    key: str
    name: str
    description: str
    model: str
    tools_count: int
    mcp_enabled: bool


@dataclass
class ModelInfo:
    """Model configuration information."""
    key: str
    name: str
    provider: str
    temperature: float
    max_tokens: int


@dataclass
class ToolInfo:
    """Tool information for agent details."""
    name: str
    description: str
    type: str


@dataclass
class AgentDetails:
    """Detailed information about an agent."""
    key: str
    name: str
    description: str
    model: ModelInfo
    tools: List[ToolInfo]
    mcp_enabled: bool
    base_prompt_template: str
    auto_run_tools: Optional[List[Dict[str, Any]]] = None
    system_prompt: Optional[str] = None


@dataclass
class ToolSummary:
    """Summary information about a tool."""
    name: str
    description: str
    source: str
    category: str


@dataclass
class ToolDetails:
    """Detailed information about a tool."""
    name: str
    description: str
    source: str
    category: str
    parameters: Dict[str, Dict[str, Any]]
    module: Optional[str] = None
    aliases: Optional[List[str]] = None


@dataclass
class SkillInfo:
    """Information about a skill."""
    name: str
    preview: Optional[str] = None
    size: Optional[str] = None


@dataclass
class ContextInfo:
    """Current execution context information."""
    context_id: Optional[str] = None
    user_id: Optional[str] = None
    pipeline_id: Optional[str] = None
    current_agent: Optional[str] = None
    working_directory: Optional[str] = None
    active_pipelines: int = 0


@dataclass
class IntrospectionResult:
    """Base result for introspection operations."""
    success: bool
    error: Optional[str] = None
    data: Any = None


# ============================================================================
# SYSTEM INTROSPECTOR
# ============================================================================

class SystemIntrospector:
    """
    Core logic for system introspection.

    This class provides methods to query system configuration and state.
    It is designed to be used by the tool functions, not directly by agents.

    Usage:
        introspector = SystemIntrospector(factory)
        agents = introspector.get_agents()
        details = introspector.get_agent_details("chat_agent")
    """

    def __init__(self, factory: Optional[AgentFactory] = None):
        """
        Initialize introspector with optional factory.

        Args:
            factory: AgentFactory instance for accessing configuration.
                    If None, methods will return appropriate errors.
        """
        self._factory = factory

    @property
    def config(self) -> Optional[Config]:
        """Get configuration from factory."""
        if self._factory is None:
            return None
        return getattr(self._factory, "config", None)

    def _categorize_tool(self, tool_name: str) -> ToolCategory:
        """Determine tool category from name."""
        category_prefixes = {
            "file_": ToolCategory.FILE,
            "git_": ToolCategory.GIT,
            "memory_": ToolCategory.MEMORY,
            "skill_": ToolCategory.SKILL,
            "orchestrate": ToolCategory.ORCHESTRATION,
            "system_": ToolCategory.SYSTEM,
            "view_image": ToolCategory.VISION,
            "send_voice": ToolCategory.VOICE,
            "keyboard_": ToolCategory.INPUT,
            "take_screenshot": ToolCategory.SCREEN,
            "crop_image": ToolCategory.SCREEN,
            "pdf": ToolCategory.DOCUMENT,
            "markdown": ToolCategory.DOCUMENT,
            "beads_": ToolCategory.TASKS,
            "emergency_": ToolCategory.EMERGENCY,
            "get_pipeline": ToolCategory.EMERGENCY,
        }

        for prefix, category in category_prefixes.items():
            if tool_name.startswith(prefix):
                return category

        return ToolCategory.OTHER

    def _extract_docstring_first_line(self, obj: Any) -> str:
        """Extract first line of docstring as description."""
        doc = getattr(obj, "__doc__", None)
        if not doc:
            return "No description available"

        # Get first non-empty line
        for line in doc.strip().split("\n"):
            line = line.strip()
            if line:
                return line[:200]  # Limit length

        return "No description available"

    def _extract_tool_parameters(self, tool_func: Any) -> Dict[str, Dict[str, Any]]:
        """Extract parameter information from a tool function."""
        parameters = {}

        try:
            # Get the actual function
            func = tool_func
            if hasattr(tool_func, "__wrapped__"):
                func = tool_func.__wrapped__
            elif hasattr(tool_func, "func"):
                func = tool_func.func

            sig = inspect.signature(func)
            type_hints = {}
            try:
                type_hints = inspect.get_annotations(func)
            except Exception:
                pass

            for param_name, param in sig.parameters.items():
                # Skip context parameter
                if param_name in ("context", "self", "cls"):
                    continue

                param_info: Dict[str, Any] = {}

                # Required or optional
                param_info["required"] = param.default is inspect.Parameter.empty

                # Type from annotations
                if param_name in type_hints:
                    hint = type_hints[param_name]
                    param_info["type"] = getattr(hint, "__name__", str(hint))

                # Default value
                if param.default is not inspect.Parameter.empty:
                    default = param.default
                    # Only include JSON-serializable defaults
                    if isinstance(default, (str, int, float, bool, type(None), list, dict)):
                        param_info["default"] = default

                parameters[param_name] = param_info

        except Exception as e:
            logger.debug(f"Could not extract parameters: {e}")

        return parameters

    # -------------------------------------------------------------------------
    # Agent Methods
    # -------------------------------------------------------------------------

    def get_agents(
        self,
        include_details: bool = False
    ) -> IntrospectionResult:
        """
        Get list of all configured agents.

        Args:
            include_details: Include model and tools count for each agent

        Returns:
            IntrospectionResult with list of AgentSummary
        """
        if self.config is None:
            return IntrospectionResult(
                success=False,
                error="Configuration not available"
            )

        try:
            agents_config = self.config.config.agents
            agents_list = []

            for key, agent_cfg in agents_config.items():
                summary = AgentSummary(
                    key=key,
                    name=agent_cfg.name,
                    description=agent_cfg.description or "No description",
                    model=agent_cfg.model if include_details else "",
                    tools_count=len(agent_cfg.tools) if include_details else 0,
                    mcp_enabled=agent_cfg.mcp_enabled if include_details else False,
                )
                agents_list.append(summary)

            return IntrospectionResult(
                success=True,
                data={
                    "agents": [asdict(a) for a in agents_list],
                    "total": len(agents_list),
                    "default_agent": self.config.config.settings.default_agent,
                }
            )

        except Exception as e:
            logger.error(f"Error listing agents: {e}", exc_info=True)
            return IntrospectionResult(success=False, error=str(e))

    def get_agent_details(
        self,
        agent_key: str,
        include_prompt: bool = False,
        include_tools_details: bool = True,
    ) -> IntrospectionResult:
        """
        Get detailed information about a specific agent.

        Args:
            agent_key: Agent identifier
            include_prompt: Include full system prompt
            include_tools_details: Include tool descriptions

        Returns:
            IntrospectionResult with AgentDetails
        """
        if self.config is None:
            return IntrospectionResult(
                success=False,
                error="Configuration not available"
            )

        try:
            agents_config = self.config.config.agents

            if agent_key not in agents_config:
                return IntrospectionResult(
                    success=False,
                    error=f"Agent '{agent_key}' not found",
                    data={"available_agents": list(agents_config.keys())}
                )

            agent_cfg = agents_config[agent_key]

            # Get model info
            model_info = ModelInfo(
                key=agent_cfg.model,
                name="Unknown",
                provider="Unknown",
                temperature=0.7,
                max_tokens=4000,
            )

            if agent_cfg.model in self.config.config.models:
                model_cfg = self.config.config.models[agent_cfg.model]
                model_info = ModelInfo(
                    key=agent_cfg.model,
                    name=model_cfg.name,
                    provider=model_cfg.provider,
                    temperature=model_cfg.temperature,
                    max_tokens=model_cfg.max_tokens,
                )

            # Get tools info
            tools_info = []
            for tool_name in agent_cfg.tools:
                tool_info = ToolInfo(
                    name=tool_name,
                    description="",
                    type="function",
                )

                if include_tools_details:
                    # Try config first
                    if tool_name in self.config.config.tools:
                        tool_cfg = self.config.config.tools[tool_name]
                        tool_info.description = tool_cfg.description
                        tool_info.type = tool_cfg.type.value
                    else:
                        # Try function registry
                        try:
                            from tools.function_tools import AVAILABLE_TOOLS
                            if tool_name in AVAILABLE_TOOLS:
                                func = AVAILABLE_TOOLS[tool_name]
                                tool_info.description = self._extract_docstring_first_line(func)
                        except Exception:
                            pass

                tools_info.append(tool_info)

            # Build details
            details = AgentDetails(
                key=agent_key,
                name=agent_cfg.name,
                description=agent_cfg.description or "",
                model=model_info,
                tools=[asdict(t) for t in tools_info],
                mcp_enabled=agent_cfg.mcp_enabled,
                base_prompt_template=agent_cfg.base_prompt,
                auto_run_tools=agent_cfg.auto_run_tools,
            )

            # Include prompt if requested
            if include_prompt:
                try:
                    details.system_prompt = self.config.build_agent_prompt(agent_key)
                except Exception as e:
                    details.system_prompt = f"[Error loading prompt: {e}]"

            return IntrospectionResult(
                success=True,
                data=asdict(details)
            )

        except Exception as e:
            logger.error(f"Error getting agent details: {e}", exc_info=True)
            return IntrospectionResult(success=False, error=str(e))

    # -------------------------------------------------------------------------
    # Tool Methods
    # -------------------------------------------------------------------------

    def get_tools(
        self,
        category: Optional[str] = None,
        include_project: bool = True,
    ) -> IntrospectionResult:
        """
        Get list of all available tools.

        Args:
            category: Filter by category (file, git, memory, etc.)
            include_project: Include project-specific tools

        Returns:
            IntrospectionResult with tools organized by category
        """
        try:
            from tools.function_tools import AVAILABLE_TOOLS, TOOL_ALIASES
            from core.managers.project_tools_loader import get_project_loader

            tools_by_category: Dict[str, List[Dict[str, Any]]] = {}

            # Process system tools
            for tool_name, tool_func in AVAILABLE_TOOLS.items():
                tool_category = self._categorize_tool(tool_name)

                # Apply filter
                if category and tool_category.value != category:
                    continue

                cat_key = tool_category.value
                if cat_key not in tools_by_category:
                    tools_by_category[cat_key] = []

                summary = ToolSummary(
                    name=tool_name,
                    description=self._extract_docstring_first_line(tool_func),
                    source=ToolSource.SYSTEM.value,
                    category=cat_key,
                )
                tools_by_category[cat_key].append(asdict(summary))

            # Process project tools
            if include_project:
                project_loader = get_project_loader()
                if project_loader:
                    project_tools = project_loader.get_all_tools()
                    for tool_name, tool_func in project_tools.items():
                        tool_category = self._categorize_tool(tool_name)

                        if category and tool_category.value != category:
                            continue

                        cat_key = tool_category.value
                        if cat_key not in tools_by_category:
                            tools_by_category[cat_key] = []

                        summary = ToolSummary(
                            name=tool_name,
                            description=self._extract_docstring_first_line(tool_func),
                            source=ToolSource.PROJECT.value,
                            category=cat_key,
                        )
                        tools_by_category[cat_key].append(asdict(summary))

            # Calculate statistics
            total = sum(len(tools) for tools in tools_by_category.values())
            by_source = {ToolSource.SYSTEM.value: 0, ToolSource.PROJECT.value: 0}

            for tools in tools_by_category.values():
                for tool in tools:
                    by_source[tool["source"]] = by_source.get(tool["source"], 0) + 1

            return IntrospectionResult(
                success=True,
                data={
                    "tools": tools_by_category,
                    "categories": sorted(tools_by_category.keys()),
                    "total": total,
                    "by_source": by_source,
                    "aliases_count": len(TOOL_ALIASES),
                }
            )

        except Exception as e:
            logger.error(f"Error listing tools: {e}", exc_info=True)
            return IntrospectionResult(success=False, error=str(e))

    def get_tool_details(
        self,
        tool_name: str,
        include_schema: bool = True,
    ) -> IntrospectionResult:
        """
        Get detailed information about a specific tool.

        Args:
            tool_name: Tool name or alias
            include_schema: Include parameter schema

        Returns:
            IntrospectionResult with ToolDetails
        """
        try:
            from tools.function_tools import AVAILABLE_TOOLS, TOOL_ALIASES
            from core.managers.project_tools_loader import get_project_loader

            # Resolve alias
            actual_name = TOOL_ALIASES.get(tool_name, tool_name)

            # Find tool
            tool_func = None
            source = ToolSource.SYSTEM

            # Check project tools first (priority)
            project_loader = get_project_loader()
            if project_loader and project_loader.has_tool(actual_name):
                tool_func = project_loader.get_tool(actual_name)
                source = ToolSource.PROJECT

            # Check system tools
            if tool_func is None and actual_name in AVAILABLE_TOOLS:
                tool_func = AVAILABLE_TOOLS[actual_name]
                source = ToolSource.SYSTEM

            if tool_func is None:
                # Suggest similar tools
                all_tools = list(AVAILABLE_TOOLS.keys())
                similar = [t for t in all_tools if tool_name.lower() in t.lower()][:5]
                return IntrospectionResult(
                    success=False,
                    error=f"Tool '{tool_name}' not found",
                    data={"similar_tools": similar}
                )

            # Get full docstring
            full_doc = getattr(tool_func, "__doc__", None) or "No description available"

            # Get aliases
            aliases = [alias for alias, target in TOOL_ALIASES.items() if target == actual_name]

            # Build details
            details = ToolDetails(
                name=actual_name,
                description=full_doc.strip(),
                source=source.value,
                category=self._categorize_tool(actual_name).value,
                parameters=self._extract_tool_parameters(tool_func) if include_schema else {},
                module=getattr(tool_func, "__module__", None),
                aliases=aliases if aliases else None,
            )

            result_data = asdict(details)

            # Add alias info if requested name was different
            if tool_name != actual_name:
                result_data["requested_alias"] = tool_name

            return IntrospectionResult(
                success=True,
                data=result_data
            )

        except Exception as e:
            logger.error(f"Error getting tool details: {e}", exc_info=True)
            return IntrospectionResult(success=False, error=str(e))

    # -------------------------------------------------------------------------
    # Skill Methods
    # -------------------------------------------------------------------------

    def get_skills(self, user_id: str = "default_user") -> IntrospectionResult:
        """
        Get list of available skills.

        Args:
            user_id: User ID for skill lookup

        Returns:
            IntrospectionResult with list of SkillInfo
        """
        if self._factory is None:
            return IntrospectionResult(
                success=False,
                error="Factory not available"
            )

        try:
            skill_manager = getattr(self._factory, "skill_manager", None)
            if skill_manager is None:
                return IntrospectionResult(
                    success=False,
                    error="Skill manager not configured",
                    data={"hint": "Skills may not be enabled for this system"}
                )

            skill_names = skill_manager.list_skills(user_id)
            skills_list = []

            for name in skill_names:
                skill_info = SkillInfo(name=name)

                try:
                    content = skill_manager.get_skill(user_id, name)
                    if content:
                        skill_info.preview = content[:100] + "..." if len(content) > 100 else content
                        skill_info.size = f"{len(content)} chars"
                except Exception:
                    pass

                skills_list.append(skill_info)

            return IntrospectionResult(
                success=True,
                data={
                    "skills": [asdict(s) for s in skills_list],
                    "total": len(skills_list),
                    "user_id": user_id,
                }
            )

        except Exception as e:
            logger.error(f"Error getting skills: {e}", exc_info=True)
            return IntrospectionResult(success=False, error=str(e))

    # -------------------------------------------------------------------------
    # Context Methods
    # -------------------------------------------------------------------------

    def get_context(
        self,
        raw_context: Any,
        include_config_summary: bool = False,
    ) -> IntrospectionResult:
        """
        Get current execution context information.

        Args:
            raw_context: Raw context from RunContextWrapper
            include_config_summary: Include system configuration summary

        Returns:
            IntrospectionResult with ContextInfo
        """
        info = ContextInfo()

        try:
            # Extract from raw context
            if raw_context:
                info.context_id = getattr(raw_context, "context_id", None)
                info.user_id = getattr(raw_context, "user_id", None)
                info.pipeline_id = getattr(raw_context, "pipeline_id", None)
                info.current_agent = getattr(raw_context, "current_agent", None)

            # Add factory-derived info
            if self._factory and self.config:
                info.working_directory = self.config.get_working_directory()

                # Check pipeline registry
                registry = getattr(self._factory, "_pipeline_registry", None)
                if registry:
                    try:
                        active = registry.get_active_pipelines()
                        info.active_pipelines = len(active) if active else 0
                    except Exception:
                        pass

            result_data = asdict(info)

            # Add config summary if requested
            if include_config_summary and self.config:
                result_data["config_summary"] = {
                    "agents_count": len(self.config.config.agents),
                    "tools_count": len(self.config.config.tools),
                    "models_count": len(self.config.config.models),
                    "default_agent": self.config.config.settings.default_agent,
                    "mcp_enabled": self.config.config.settings.mcp_enabled,
                    "max_turns": self.config.config.settings.max_turns,
                }

            return IntrospectionResult(
                success=True,
                data=result_data
            )

        except Exception as e:
            logger.error(f"Error getting context: {e}", exc_info=True)
            return IntrospectionResult(success=False, error=str(e))


# ============================================================================
# HELP DOCUMENTATION
# ============================================================================

HELP_TOPICS = {
    "overview": """
# Grid Agent System - Overview

Grid is a multi-agent orchestration system that enables:

## Core Capabilities
- **Multiple Agents**: Configure specialized agents for different tasks
- **Dynamic Orchestration**: Create agents on-the-fly for complex workflows
- **Tool Ecosystem**: 45+ tools for file ops, git, memory, vision, etc.
- **Memory System**: Persistent memory with SQLite backend
- **Skills**: Reusable knowledge bases for agents

## Architecture
```
User Request → Agent (with tools) → Response
                  ↓
            orchestrate() → Dynamic Agent → Sub-task result
```

## Quick Start
1. Use `system_list_agents()` to see available agents
2. Use `system_list_tools()` to see available tools
3. Use `orchestrate()` to create dynamic agents for complex tasks

## Related Topics
- `system_help(topic="agents")` - Agent configuration
- `system_help(topic="tools")` - Tool categories
- `system_help(topic="orchestration")` - Dynamic agents
""",

    "agents": """
# Agents in Grid

## What is an Agent?
An agent is an AI assistant configured with:
- A specific model (LLM)
- A set of tools it can use
- A system prompt defining its behavior
- Optional MCP (Model Context Protocol) integrations

## Agent Configuration (config.yaml)
```yaml
agents:
  code_agent:
    name: "Code Agent"
    model: "qwen-3.5"
    tools: ["file_read", "file_write", "git_status"]
    base_prompt: "code_assistant"
    description: "Specialist for programming tasks"
```

## Introspection Commands
- `system_list_agents()` - List all agents
- `system_get_agent_info(agent_key)` - Get agent details
- `system_get_agent_info(agent_key, include_prompt=True)` - Include system prompt

## Agent-to-Agent Communication
Agents can delegate tasks using:
- `orchestrate(task, ...)` - Create dynamic agent for sub-tasks
- Agent tools (type: "agent") - Call predefined agents directly
""",

    "tools": """
# Tools in Grid

## Tool Categories

| Category | Prefix | Examples |
|----------|--------|----------|
| File | `file_*` | file_read, file_write, file_list |
| Git | `git_*` | git_status, git_commit, git_push |
| Memory | `memory_*` | memory_save, memory_search |
| Skills | `skill_*` | skill_list, skill_read, skill_create |
| Orchestration | `orchestrate*` | orchestrate, orchestrate_emergent |
| System | `system_*` | system_list_agents, system_help |
| Vision | `view_*` | view_image |
| Voice | `send_voice*` | send_voice_reply |
| Emergency | `emergency_*` | emergency_shutdown |

## Introspection Commands
- `system_list_tools()` - All tools by category
- `system_list_tools(category="file")` - Filter by category
- `system_get_tool_info(tool_name)` - Tool details with parameters

## Tool Sources
- **System tools**: Built-in tools from the Grid framework
- **Project tools**: Custom tools from project's `tools/` directory
""",

    "memory": """
# Memory System

Grid has a multi-level memory system:

## Short-term Memory
- Conversation history within session
- Managed by ContextManager
- Configurable via `max_history` setting

## Long-term Memory (SQLite)
- Persistent across sessions
- Full-text search support

```python
# Save information
memory_save(content="Important fact", tags="reference,api")

# Search later
memory_search(query="important")

# Delete
memory_delete(id="...")
```

## Skills (Knowledge Bases)
- Markdown files with reusable knowledge
- Per-user storage

```python
# List skills
skill_list()

# Read skill
skill_read(name="api-guidelines")

# Create skill
skill_create(name="new-skill", file_path="/path/to/content.md")
```
""",

    "orchestration": """
# Dynamic Orchestration

## orchestrate() Tool
Creates a temporary agent for a specific task:

```python
orchestrate(
    task="Analyze this code for security issues",
    agent_system_prompt="You are a security expert...",
    model_key="claude-3-sonnet",
    executor_tools=["file_read", "file_search"]
)
```

## When to Use Orchestration
- Complex multi-step tasks
- Tasks requiring specialized instructions
- Parallel sub-task execution
- Review/validation workflows

## Pipeline Patterns

### Sequential Pipeline
```
Agent A → Agent B → Agent C → Final Result
```

### Review Loop
```
Executor → Reviewer → (Revise if needed) → Final
```

### Committee Voting
```
Agent 1 ─┐
Agent 2 ─┼→ Vote Aggregation → Decision
Agent 3 ─┘
```

## Emergency Control
- `emergency_shutdown(reason)` - Stop all running tasks
- `get_pipeline_status()` - Check pipeline state
""",

    "commands": """
# System Commands Reference

## Introspection
| Command | Description |
|---------|-------------|
| `system_list_agents()` | List all configured agents |
| `system_get_agent_info(key)` | Agent details (tools, model) |
| `system_list_tools()` | List all tools by category |
| `system_get_tool_info(name)` | Tool parameters and docs |
| `system_get_skills()` | Available skills |
| `system_help(topic)` | This documentation |
| `system_get_context()` | Execution context info |

## Memory
| Command | Description |
|---------|-------------|
| `memory_save(content, tags)` | Save to persistent memory |
| `memory_search(query)` | Search memories |
| `memory_delete(id)` | Remove memory entry |

## Skills
| Command | Description |
|---------|-------------|
| `skill_list()` | List available skills |
| `skill_read(name)` | Read skill content |
| `skill_create(name, file_path)` | Create new skill |

## Orchestration
| Command | Description |
|---------|-------------|
| `orchestrate(task, ...)` | Create dynamic agent |
| `emergency_shutdown(reason)` | Stop all pipelines |
| `get_pipeline_status()` | Pipeline information |
"""
}


# ============================================================================
# CONTEXT HELPERS
# ============================================================================

def _get_factory(context: RunContextWrapper) -> Optional[AgentFactory]:
    """Extract AgentFactory from RunContextWrapper."""
    try:
        raw = getattr(context, "context", None)
        if raw is None:
            return None
        return getattr(raw, "factory", None)
    except Exception as e:
        logger.debug(f"Could not get factory: {e}")
        return None


def _get_raw_context(context: RunContextWrapper) -> Any:
    """Extract raw context from RunContextWrapper."""
    return getattr(context, "context", None)


def _get_user_id(context: RunContextWrapper) -> str:
    """Extract user_id from context."""
    try:
        raw = getattr(context, "context", None)
        if raw:
            uid = getattr(raw, "user_id", None)
            if uid:
                return uid
    except Exception:
        pass
    return "default_user"


def _format_result(result: IntrospectionResult) -> str:
    """Format IntrospectionResult as JSON string."""
    if result.success:
        return json.dumps(result.data, ensure_ascii=False, indent=2)
    else:
        error_response = {"error": result.error}
        if result.data:
            error_response.update(result.data)
        return json.dumps(error_response, ensure_ascii=False, indent=2)


# ============================================================================
# TOOL FUNCTIONS
# ============================================================================

@function_tool
async def system_list_agents(
    context: RunContextWrapper,
    include_details: bool = False,
) -> str:
    """
    List all configured agents in the system.

    Returns a list of agents with their names and descriptions. This tool helps
    you discover what agents are available and their capabilities before
    delegating tasks or using orchestration.

    Args:
        include_details: If True, include model name and tools count for each agent.
                        Use this to understand agent capabilities at a glance.

    Returns:
        JSON object with:
        - agents: List of agent summaries (key, name, description, [model, tools_count])
        - total: Total number of agents
        - default_agent: Key of the default agent

    Example:
        ```python
        # Basic list
        result = system_list_agents()

        # With details to see capabilities
        result = system_list_agents(include_details=True)
        # Returns: {"agents": [{"key": "chat_agent", "model": "qwen-3.5", "tools_count": 5}], ...}
        ```
    """
    factory = _get_factory(context)
    introspector = SystemIntrospector(factory)
    result = introspector.get_agents(include_details=include_details)
    return _format_result(result)


@function_tool
async def system_get_agent_info(
    context: RunContextWrapper,
    agent_key: str,
    include_prompt: bool = False,
    include_tools_details: bool = True,
) -> str:
    """
    Get detailed information about a specific agent.

    Returns comprehensive information about an agent including its model configuration,
    available tools, and optionally the full system prompt. Use this to understand
    an agent's capabilities before delegation.

    Args:
        agent_key: The agent identifier (e.g., "chat_agent", "code_agent").
                  Use system_list_agents() to see available keys.
        include_prompt: If True, include the full system prompt. The prompt can be
                       lengthy, so only request it when you need to understand
                       the agent's instructions in detail.
        include_tools_details: If True (default), include description for each tool
                              the agent has access to.

    Returns:
        JSON object with:
        - key, name, description: Agent identification
        - model: Model configuration (key, name, provider, temperature, max_tokens)
        - tools: List of tools with descriptions
        - mcp_enabled: Whether MCP is enabled
        - auto_run_tools: Tools that run automatically on agent start
        - system_prompt: Full prompt (if include_prompt=True)

    Example:
        ```python
        # Get agent capabilities
        info = system_get_agent_info("code_agent")

        # Get full details including prompt
        info = system_get_agent_info("code_agent", include_prompt=True)
        ```
    """
    factory = _get_factory(context)
    introspector = SystemIntrospector(factory)
    result = introspector.get_agent_details(
        agent_key=agent_key,
        include_prompt=include_prompt,
        include_tools_details=include_tools_details,
    )
    return _format_result(result)


@function_tool
async def system_list_tools(
    context: RunContextWrapper,
    category: Optional[str] = None,
    include_project_tools: bool = True,
) -> str:
    """
    List all available tools in the system organized by category.

    Returns tools grouped by their category (file, git, memory, etc.). Use this
    to discover what operations are available and plan your approach to tasks.

    Args:
        category: Filter to specific category. Valid values:
                 file, git, memory, skill, orchestration, system, vision,
                 voice, input, screen, document, tasks, emergency, other.
                 If None, returns all tools grouped by category.
        include_project_tools: If True (default), include project-specific tools
                              in addition to system tools.

    Returns:
        JSON object with:
        - tools: Dict mapping category -> list of tool summaries
        - categories: List of available categories
        - total: Total tool count
        - by_source: Count by source (system, project)
        - aliases_count: Number of tool aliases

    Example:
        ```python
        # Get all tools
        tools = system_list_tools()

        # Get only file tools
        file_tools = system_list_tools(category="file")

        # Get only system tools (exclude project)
        system_tools = system_list_tools(include_project_tools=False)
        ```
    """
    factory = _get_factory(context)
    introspector = SystemIntrospector(factory)
    result = introspector.get_tools(
        category=category,
        include_project=include_project_tools,
    )
    return _format_result(result)


@function_tool
async def system_get_tool_info(
    context: RunContextWrapper,
    tool_name: str,
    include_schema: bool = True,
) -> str:
    """
    Get detailed information about a specific tool.

    Returns the tool's full documentation, parameter schema, and metadata.
    Use this when you need to understand how to use a specific tool correctly.

    Args:
        tool_name: Name of the tool (e.g., "file_read", "orchestrate").
                  Aliases are also accepted (e.g., "read_file" -> "file_read").
        include_schema: If True (default), include detailed parameter schema
                       with types, required flags, and default values.

    Returns:
        JSON object with:
        - name: Actual tool name (resolved from alias if needed)
        - description: Full docstring with usage examples
        - source: "system" or "project"
        - category: Tool category
        - parameters: Parameter schema (if include_schema=True)
        - aliases: List of aliases for this tool
        - module: Python module containing the tool

    Example:
        ```python
        # Get tool documentation
        info = system_get_tool_info("orchestrate")

        # Get info using alias
        info = system_get_tool_info("read_file")  # Returns info for "file_read"
        ```
    """
    factory = _get_factory(context)
    introspector = SystemIntrospector(factory)
    result = introspector.get_tool_details(
        tool_name=tool_name,
        include_schema=include_schema,
    )
    return _format_result(result)


@function_tool
async def system_get_skills(
    context: RunContextWrapper,
) -> str:
    """
    List available skills (knowledge bases) for the current user.

    Skills are reusable markdown documents containing domain knowledge,
    guidelines, or reference material that agents can access. Use this
    to discover what knowledge is available before starting a task.

    Returns:
        JSON object with:
        - skills: List of skill summaries (name, preview, size)
        - total: Number of available skills
        - user_id: Current user ID

    Example:
        ```python
        # List available skills
        skills = system_get_skills()
        # Returns: {"skills": [{"name": "python-guidelines", "preview": "...", "size": "2.3 KB"}], ...}

        # Then read a specific skill
        content = skill_read(name="python-guidelines")
        ```
    """
    factory = _get_factory(context)
    user_id = _get_user_id(context)
    introspector = SystemIntrospector(factory)
    result = introspector.get_skills(user_id=user_id)
    return _format_result(result)


@function_tool
async def system_help(
    context: RunContextWrapper,
    topic: str = "overview",
) -> str:
    """
    Get documentation and help about the Grid agent system.

    Provides formatted documentation on various system aspects.
    Use this to understand how the system works and what capabilities
    are available.

    Args:
        topic: Help topic. Available topics:
              - overview: General system architecture and capabilities
              - agents: How agents work, configuration, orchestration
              - tools: Tool categories and usage patterns
              - memory: Memory system (short-term, long-term, skills)
              - orchestration: Dynamic agent creation, pipelines
              - commands: Quick reference of all system commands

    Returns:
        Markdown-formatted documentation for the requested topic.

    Example:
        ```python
        # Get system overview
        help_text = system_help()

        # Learn about orchestration
        help_text = system_help(topic="orchestration")

        # Quick command reference
        help_text = system_help(topic="commands")
        ```
    """
    if topic not in HELP_TOPICS:
        available = sorted(HELP_TOPICS.keys())
        return f"Unknown topic: '{topic}'\n\nAvailable topics: {', '.join(available)}"

    return HELP_TOPICS[topic].strip()


@function_tool
async def system_get_context(
    context: RunContextWrapper,
    include_config_summary: bool = False,
) -> str:
    """
    Get information about the current execution context.

    Returns information about where and how this agent is running,
    including context IDs, working directory, and active pipelines.
    Useful for understanding the execution environment and debugging.

    Args:
        include_config_summary: If True, include a summary of the system
                               configuration (agent count, tool count, etc.).

    Returns:
        JSON object with:
        - context_id: Current context/session identifier
        - user_id: Current user identifier
        - pipeline_id: Active pipeline ID (if in orchestrated execution)
        - current_agent: Name of the currently executing agent
        - working_directory: File system working directory
        - active_pipelines: Number of running pipelines
        - config_summary: System config summary (if requested)

    Example:
        ```python
        # Get basic context
        ctx = system_get_context()

        # Get context with system overview
        ctx = system_get_context(include_config_summary=True)
        ```
    """
    factory = _get_factory(context)
    raw_context = _get_raw_context(context)
    introspector = SystemIntrospector(factory)
    result = introspector.get_context(
        raw_context=raw_context,
        include_config_summary=include_config_summary,
    )
    return _format_result(result)


# ============================================================================
# TOOL REGISTRY
# ============================================================================

SYSTEM_TOOLS = {
    "system_list_agents": system_list_agents,
    "system_get_agent_info": system_get_agent_info,
    "system_list_tools": system_list_tools,
    "system_get_tool_info": system_get_tool_info,
    "system_get_skills": system_get_skills,
    "system_help": system_help,
    "system_get_context": system_get_context,
}
