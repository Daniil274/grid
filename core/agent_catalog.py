"""Strict model and tool catalog for externally launched Grid agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List

from core.config.config import Config
from core.managers.project_tools_loader import initialize_project_tools
from schemas import ToolConfig, ToolType


def _is_function_tool(value: Any) -> bool:
    """Return whether *value* is an OpenAI Agents SDK FunctionTool."""
    return (
        hasattr(value, "name")
        and hasattr(value, "description")
        and hasattr(value, "params_json_schema")
        and hasattr(value, "on_invoke_tool")
        and callable(value.on_invoke_tool)
    )


class AgentCatalog:
    """Expose the exact OpenCode models and claude-tools available to Codex."""

    def __init__(
        self,
        config: Config,
        *,
        tools_directory: str = "examples/claude-tools/tools",
        provider_key: str = "opencode",
    ) -> None:
        self.config = config
        self.provider_key = provider_key
        self.tools_directory = tools_directory
        self._tools: Dict[str, Any] = {}
        self._load_tools()

    def _load_tools(self) -> None:
        config_dir = Path(self.config.config_path).resolve().parent
        loader = initialize_project_tools(str(config_dir), self.tools_directory)
        loaded = loader.get_all_tools()
        self._tools = {
            tool.name: tool for tool in loaded.values() if _is_function_tool(tool)
        }

        # AgentFactory resolves only tools declared in config. Register the strict
        # FunctionTool catalog dynamically so the worker config stays lightweight.
        for name, tool in self._tools.items():
            if name not in self.config.config.tools:
                self.config.config.tools[name] = ToolConfig(
                    type=ToolType.FUNCTION,
                    name=name,
                    description=str(getattr(tool, "description", "") or name),
                )

    def model_keys(self) -> List[str]:
        allowed = set(self.config.config.settings.allowed_models or [])
        return sorted(
            key
            for key, model in self.config.config.models.items()
            if model.provider == self.provider_key and (not allowed or key in allowed)
        )

    def tool_names(self) -> List[str]:
        return sorted(self._tools)

    def require_model(self, model_key: str) -> None:
        if model_key not in set(self.model_keys()):
            available = ", ".join(self.model_keys()) or "none"
            raise ValueError(
                f"Model '{model_key}' is not an enabled {self.provider_key} model. "
                f"Available models: {available}."
            )

    def require_tools(self, tool_names: Iterable[str]) -> List[str]:
        requested = list(
            dict.fromkeys(str(name).strip() for name in tool_names if str(name).strip())
        )
        missing = [name for name in requested if name not in self._tools]
        if missing:
            raise ValueError(
                "Unknown or unavailable tools: "
                + ", ".join(missing)
                + ". Call grid_get_system and use exact tool names."
            )
        return requested

    def system_info(self, *, constraints: Dict[str, Any]) -> Dict[str, Any]:
        models = []
        for key in self.model_keys():
            model = self.config.config.models[key]
            models.append(
                {
                    "key": key,
                    "model": model.name,
                    "provider": model.provider,
                    "description": model.description,
                    "capabilities": list(model.capabilities or []),
                    "context_window": model.context_window,
                    "max_output_tokens": model.max_tokens,
                }
            )

        tools = []
        for name in self.tool_names():
            tool = self._tools[name]
            tools.append(
                {
                    "name": name,
                    "description": str(getattr(tool, "description", "") or name),
                }
            )

        return {
            "workflow": {
                "launch_actions": 2,
                "step_1": "Call grid_get_system once and cache this response.",
                "step_2": "Call grid_start_agent with one returned model key, the task, and returned tool names.",
                "after_launch": "Continue other work, then use grid_wait_agents or grid_get_agents.",
            },
            "provider": self.provider_key,
            "models": models,
            "tools": tools,
            "constraints": constraints,
        }
