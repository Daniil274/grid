"""
Enterprise-grade configuration management for Grid system.
"""

import os
import yaml
from typing import Dict, Any, Optional
from pathlib import Path
from functools import lru_cache

from schemas import GridConfig, ProviderConfig, ModelConfig, AgentConfig, ToolConfig
from utils.exceptions import ConfigError
from utils.logger import Logger

logger = Logger(__name__)


class Config:
    """Thread-safe configuration manager with validation and caching."""
    
    def __init__(self, config_path: str = "config.yaml", working_directory: Optional[str] = None):
        """
        Initialize configuration manager.
        
        Args:
            config_path: Path to configuration file
            working_directory: Working directory override
        """
        self.config_path = Path(config_path)
        self._config: Optional[GridConfig] = None
        self._working_directory = working_directory or os.getcwd()
        self._load_config()
    
    def _load_config(self) -> None:
        """Load and validate configuration from YAML file."""
        try:
            if not self.config_path.exists():
                raise ConfigError(f"Configuration file {self.config_path} not found")
            
            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw_config = yaml.safe_load(f)
            
            # Validate using Pydantic
            self._config = GridConfig(**raw_config)
            
            logger.info(f"Configuration loaded from {self.config_path}")
            logger.debug(f"Loaded {len(self._config.agents)} agents, {len(self._config.models)} models")
            
            # Do NOT change process working directory to preserve project-relative paths (e.g., logs/)
            # All file resolutions must go through get_absolute_path/working_directory
            target_working_dir = self.get_working_directory()
            if target_working_dir and target_working_dir != os.getcwd():
                if os.path.exists(target_working_dir):
                    logger.info(f"Using configured working directory for path resolution only (no chdir): {target_working_dir}")
                else:
                    logger.warning(
                        "Configured working directory does not exist (will be ignored for path resolution): %s",
                        target_working_dir,
                    )
            
        except FileNotFoundError as e:
            raise ConfigError(f"Configuration file not found: {e}")
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML format: {e}")
        except Exception as e:
            raise ConfigError(f"Configuration validation failed: {e}")
    
    def reload(self) -> None:
        """Reload configuration from file."""
        logger.info("Reloading configuration...")
        self._load_config()
        # Clear cached properties
        self._clear_cache()
    
    def _clear_cache(self) -> None:
        """Clear LRU cache for property methods."""
        # Clear any cached methods if needed
        pass
    
    @property
    def config(self) -> GridConfig:
        """Get validated configuration."""
        if self._config is None:
            raise ConfigError("Configuration not loaded")
        return self._config
    
    # Working directory methods
    def get_working_directory(self) -> str:
        """Get current working directory."""
        return self.config.settings.working_directory or self._working_directory
    
    def get_config_directory(self) -> str:
        """Get configuration directory."""
        return self.config.settings.config_directory or str(self.config_path.parent)
    
    def set_working_directory(self, path: str) -> None:
        """Set working directory if allowed."""
        if not self.config.settings.allow_path_override:
            logger.warning("Path override is disabled in configuration")
            return
        
        self._working_directory = os.path.abspath(path)
        logger.info(f"Working directory set to: {self._working_directory}")
    
    def get_absolute_path(self, relative_path: str) -> str:
        """Convert relative path to absolute based on working directory."""
        if os.path.isabs(relative_path):
            return relative_path
        return os.path.join(self.get_working_directory(), relative_path)
    
    # Provider methods
    def get_provider(self, provider_key: str) -> ProviderConfig:
        """Get provider configuration."""
        if provider_key not in self.config.providers:
            raise ConfigError(f"Provider '{provider_key}' not found")
        return self.config.providers[provider_key]
    
    def get_api_key(self, provider_key: str) -> Optional[str]:
        """Get API key for provider from environment or config."""
        provider = self.get_provider(provider_key)
        
        # Direct API key in config (not recommended for production)
        if provider.api_key:
            return provider.api_key
        
        # Environment variable
        if provider.api_key_env:
            api_key = os.getenv(provider.api_key_env)
            if api_key:
                return api_key
        
        logger.warning(f"No API key found for provider '{provider_key}'")
        return None
    
    # Model methods
    def get_model(self, model_key: str) -> ModelConfig:
        """Get model configuration."""
        if model_key not in self.config.models:
            raise ConfigError(f"Model '{model_key}' not found")
        return self.config.models[model_key]
    
    # Agent methods
    def get_agent(self, agent_key: str) -> AgentConfig:
        """Get agent configuration."""
        if agent_key not in self.config.agents:
            raise ConfigError(f"Agent '{agent_key}' not found")
        return self.config.agents[agent_key]
    
    def get_default_agent(self) -> str:
        """Get default agent key."""
        return self.config.settings.default_agent
    
    def list_agents(self) -> Dict[str, str]:
        """List all available agents with descriptions."""
        return {
            key: agent.description or agent.name 
            for key, agent in self.config.agents.items()
        }
    
    # Tool methods
    def get_tool(self, tool_key: str) -> ToolConfig:
        """Get tool configuration."""
        if tool_key not in self.config.tools:
            raise ConfigError(f"Tool '{tool_key}' not found")
        return self.config.tools[tool_key]
    
    # Prompt methods
    def get_prompt_template(self, template_key: str) -> str:
        """Get prompt template."""
        if template_key not in self.config.prompt_templates:
            raise ConfigError(f"Prompt template '{template_key}' not found")
        return self.config.prompt_templates[template_key]
    
    def build_agent_prompt(self, agent_key: str) -> str:
        """Build complete prompt for agent including tool descriptions."""
        agent_config = self.get_agent(agent_key)
        
        # Base prompt
        if agent_config.custom_prompt:
            base_prompt = agent_config.custom_prompt
        else:
            base_prompt = self.get_prompt_template(agent_config.base_prompt)
        
        # Tool descriptions
        tool_descriptions = []
        for tool_name in agent_config.tools:
            try:
                tool_config = self.get_tool(tool_name)
                if tool_config.prompt_addition:
                    tool_descriptions.append(tool_config.prompt_addition)
            except ConfigError:
                logger.warning(f"Tool '{tool_name}' not found for agent '{agent_key}'")
        
        # Combine parts
        parts = [base_prompt]
        # Общие правила для инструментов (если заданы) — добавляем один раз
        common_rules = getattr(self.config.settings, 'tools_common_rules', None)
        if common_rules:
            parts.append("\nПравила использования инструментов (общие):")
            parts.append(str(common_rules))
        if tool_descriptions:
            parts.append("\nДоступные инструменты:")
            parts.extend(tool_descriptions)
        
        return "\n".join(parts)
    
    # Settings methods
    def is_debug(self) -> bool:
        """Check if debug mode is enabled."""
        return self.config.settings.debug
    
    def is_mcp_enabled(self) -> bool:
        """Check if MCP is globally enabled."""
        return self.config.settings.mcp_enabled
    
    def get_max_history(self) -> int:
        """Get maximum history size."""
        return self.config.settings.max_history
    
    def get_max_turns(self) -> int:
        """Get maximum turns limit for agents."""
        return self.config.settings.max_turns
    
    def get_agent_timeout(self) -> int:
        """Get agent execution timeout in seconds."""
        return self.config.settings.agent_timeout