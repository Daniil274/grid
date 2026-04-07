"""
Enterprise-grade configuration management for Grid system.
"""

import os
import yaml
from typing import Dict, Any, List, Optional
from pathlib import Path
from functools import lru_cache
import logging
import ipaddress
from urllib.parse import urlparse

from schemas import GridConfig, ProviderConfig, ModelConfig, AgentConfig, ToolConfig
from utils.exceptions import ConfigError
from core.tracing_config import get_tracing_config
from core.prompt_sections import PromptSection

tracing_config = get_tracing_config()
logger = logging.getLogger("grid.config")


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
        self._cli_working_directory = working_directory
        self._working_directory = working_directory or os.getcwd()
        self._load_config()

    def _resolve_config_relative_path(self, path: Optional[str]) -> Optional[str]:
        """Resolve a config-defined path relative to the config file directory."""
        if not path:
            return path

        raw_path = Path(path).expanduser()
        if raw_path.is_absolute():
            return str(raw_path.resolve(strict=False))

        return str((self.config_path.parent.resolve() / raw_path).resolve(strict=False))
    
    def _load_config(self) -> None:
        """Load and validate configuration from YAML file."""
        try:
            if not self.config_path.exists():
                raise ConfigError(f"Configuration file {self.config_path} not found")

            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw_config = yaml.safe_load(f)

            # -----------------------------------------------------------------
            # Auto-attach base system tools for project configs
            # -----------------------------------------------------------------
            try:
                settings_dict = (raw_config or {}).get("settings") or {}
                project_tools_dict = settings_dict.get("project_tools") or {}
                base_tools = project_tools_dict.get("base_tools") or []

                if base_tools:
                    # Import system tools registry lazily to avoid heavy imports on startup
                    from tools.function_tools import AVAILABLE_TOOLS, TOOL_ALIASES, get_tool_info

                    tools_section = raw_config.setdefault("tools", {})
                    attached_count = 0

                    for tool_name in base_tools:
                        # Skip if tool is already declared explicitly in this config
                        if tool_name in tools_section:
                            continue

                        # Resolve actual system tool name via aliases
                        actual_name = TOOL_ALIASES.get(tool_name, tool_name)
                        if actual_name not in AVAILABLE_TOOLS:
                            logger.warning(
                                "Base tool '%s' from settings.project_tools.base_tools "
                                "not found in system tools registry; skipping",
                                tool_name,
                            )
                            continue

                        # Try to get human-friendly description from system tools
                        desc = ""
                        try:
                            info = get_tool_info(tool_name)
                            if isinstance(info, dict) and "error" not in info:
                                desc = info.get("description") or ""
                        except Exception:
                            # Fallback description if inspection fails
                            desc = ""

                        tools_section[tool_name] = {
                            "type": "function",
                            "description": desc or f"System tool '{tool_name}'",
                        }
                        attached_count += 1

                    if attached_count:
                        logger.info(
                            "Attached %s base system tool(s) to config from settings.project_tools.base_tools: %s",
                            attached_count,
                            base_tools,
                        )
            except Exception as e:
                logger.error(
                    "Failed to attach base system tools from settings.project_tools.base_tools: %s",
                    e,
                    exc_info=True,
                )

            # Validate using Pydantic
            self._config = GridConfig(**raw_config)
            
            # Validate system correctness (agents, tools, models integration) immediately after parsing
            self._validate_consistency()

            # Determine effective working directory
            config_wd = self._resolve_config_relative_path(self.config.settings.working_directory)
            allow_override = self.config.settings.allow_path_override

            if self._cli_working_directory:
                if allow_override:
                    self._working_directory = self._cli_working_directory
                else:
                    logger.warning("Path override disabled in config. Ignoring CLI working directory.")
                    self._working_directory = config_wd or os.getcwd()
            else:
                # No CLI arg, prefer config, fall back to CWD
                self._working_directory = config_wd or os.getcwd()

            self._working_directory = os.path.abspath(self._working_directory)

            # Configuration loaded successfully - this will be traced automatically by Agents SDK

            # Do NOT change process working directory to preserve project-relative paths (e.g., logs/)
            # All file resolutions must go through get_absolute_path/working_directory
            if self._working_directory != os.getcwd():
                if os.path.exists(self._working_directory):
                    pass  # Using configured working directory for path resolution only (no chdir)
                else:
                    pass  # Configured working directory does not exist (will be ignored for path resolution)

            # Initialize project tools loader (if enabled)
            self._init_project_tools()

        except FileNotFoundError as e:
            raise ConfigError(f"Configuration file not found: {e}")
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML format: {e}")
        except Exception as e:
            raise ConfigError(f"Configuration validation failed: {e}")

    def _init_project_tools(self) -> None:
        """Initialize project tools loader if enabled in configuration."""
        try:
            # Get project_tools settings
            project_tools_config = self.config.settings.project_tools

            logger.info(f"Project tools config: {project_tools_config}")

            # Check if configured and enabled
            if project_tools_config is None:
                logger.info("Project tools config is None - not configured")
                return

            if not project_tools_config.enabled:
                logger.info("Project tools loader is disabled in config")
                return

            # Set ISKOR serial retries and command timeout for project tools (get_screen, key_press, key_sequence)
            serial_cfg = getattr(self.config.settings, "serial", None)
            if serial_cfg is not None:
                if getattr(serial_cfg, "retries", None) is not None:
                    os.environ["ISKOR_RETRIES"] = str(serial_cfg.retries)
                    logger.debug(f"ISKOR_RETRIES set to {serial_cfg.retries} from config")
                if getattr(serial_cfg, "command_timeout_sec", None) is not None:
                    os.environ["ISKOR_COMMAND_TIMEOUT"] = str(serial_cfg.command_timeout_sec)
                    logger.debug(f"ISKOR_COMMAND_TIMEOUT set to {serial_cfg.command_timeout_sec} from config")
            if "ISKOR_RETRIES" not in os.environ:
                os.environ.setdefault("ISKOR_RETRIES", "3")
            if "ISKOR_COMMAND_TIMEOUT" not in os.environ:
                os.environ.setdefault("ISKOR_COMMAND_TIMEOUT", "0.5")

            # Set ISKOR window automation settings for project tools (get_screen, key_press via xdotool)
            window_cfg = getattr(self.config.settings, "window", None)
            if window_cfg is not None:
                if getattr(window_cfg, "title_pattern", None) is not None:
                    os.environ["ISKOR_WINDOW_TITLE"] = str(window_cfg.title_pattern)
                    logger.debug(f"ISKOR_WINDOW_TITLE set to {window_cfg.title_pattern} from config")
                if getattr(window_cfg, "key_delay_sec", None) is not None:
                    os.environ["ISKOR_KEY_DELAY"] = str(window_cfg.key_delay_sec)
                    logger.debug(f"ISKOR_KEY_DELAY set to {window_cfg.key_delay_sec} from config")
                if getattr(window_cfg, "screenshot_delay_sec", None) is not None:
                    os.environ["ISKOR_SCREEN_DELAY"] = str(window_cfg.screenshot_delay_sec)
                    logger.debug(f"ISKOR_SCREEN_DELAY set to {window_cfg.screenshot_delay_sec} from config")

            # Get tools directory
            tools_directory = project_tools_config.tools_directory
            config_dir = str(self.config_path.parent.resolve())

            logger.info(f"[INIT] Initializing project tools from: {config_dir}/{tools_directory}")

            # Import and initialize loader
            from core.managers.project_tools_loader import initialize_project_tools

            loader = initialize_project_tools(config_dir, tools_directory)
            loaded_tools = loader.get_all_tools()

            logger.info(f"[OK] Loaded {len(loaded_tools)} project tools: {list(loaded_tools.keys())}")

        except Exception as exc:
            logger.error(f"[ERROR] Failed to initialize project tools: {exc}", exc_info=True)
            
    def _validate_consistency(self) -> None:
        """Validate structural consistency of agents, tools, models, and providers to ensure no unseen references exist."""
        # 1. Models and Providers
        if getattr(self.config, 'models', None):
            for model_key, model_config in self.config.models.items():
                if model_config.provider and model_config.provider not in self.config.providers:
                    raise ConfigError(f"System validation failed: Model '{model_key}' references unknown provider '{model_config.provider}'")

        # 2. Agents
        if getattr(self.config, 'agents', None):
            for agent_key, agent_config in self.config.agents.items():
                if getattr(agent_config, 'model', None) and agent_config.model not in self.config.models:
                    raise ConfigError(f"System validation failed: Agent '{agent_key}' references unknown model '{agent_config.model}'")
                
                if getattr(agent_config, 'tools', None):
                    for tool in agent_config.tools:
                        if tool not in self.config.tools:
                            raise ConfigError(f"System validation failed: Agent '{agent_key}' references unknown tool '{tool}'")

        # 3. Checkers
        if getattr(self.config, 'checkers', None):
            for checker_key, checker_config in self.config.checkers.items():
                try: # handle dict vs Pydantic model representation
                    model_ref = checker_config.model if hasattr(checker_config, 'model') else checker_config.get('model')
                    if model_ref and model_ref not in self.config.models:
                        raise ConfigError(f"System validation failed: Checker '{checker_key}' references unknown model '{model_ref}'")
                except Exception:
                    pass
    
    def reload(self) -> None:
        """Reload configuration from file."""
        # Reloading configuration - this will be traced automatically by Agents SDK
        self._load_config()
        # Clear cached properties
        self._clear_cache()
    
    def _clear_cache(self) -> None:
        """Clear cached method results if any callables expose ``cache_clear``."""
        cleared = 0
        for attr_name in dir(self):
            if attr_name.startswith("_"):
                continue
            attr = getattr(self, attr_name)
            cache_clear = getattr(attr, "cache_clear", None)
            if callable(cache_clear):
                cache_clear()
                cleared += 1
        if cleared:
            logger.debug("Cleared %s cached accessor(s) after config reload", cleared)
    
    @property
    def config(self) -> GridConfig:
        """Get validated configuration."""
        if self._config is None:
            raise ConfigError("Configuration not loaded")
        return self._config
    
    # Working directory methods
    def get_working_directory(self) -> str:
        """Get current working directory."""
        return self._working_directory
    
    def get_config_directory(self) -> str:
        """Get configuration directory."""
        config_dir = self._resolve_config_relative_path(self.config.settings.config_directory)
        return config_dir or str(self.config_path.parent.resolve())
    
    def set_working_directory(self, path: str) -> None:
        """Set working directory if allowed."""
        if not self.config.settings.allow_path_override:
            # Path override is disabled in configuration
            logger.warning("Path override is disabled in configuration")
            return
        
        self._working_directory = os.path.abspath(path)
        logger.info(f"Working directory set to: {self._working_directory}")
    
    def get_absolute_path(self, relative_path: str) -> str:
        """Convert relative path to absolute based on working directory, returning POSIX-style path separators for consistency across OSes."""
        # If absolute, return normalized POSIX string
        if os.path.isabs(relative_path):
            return relative_path.replace('\\', '/')
        # Join using pathlib and normalize to POSIX string
        return (Path(self.get_working_directory()) / relative_path).as_posix()
    
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

    def _load_skill_file(self, skill_name: str) -> Optional[str]:
        """Load a system skill from the skills directory."""
        skills_dir_name = getattr(self.config.settings, "skills_directory", "skills")
        skills_dir = self.config_path.parent / skills_dir_name
        for ext in (".md", ".txt"):
            skill_path = skills_dir / f"{skill_name}{ext}"
            if skill_path.exists():
                try:
                    return skill_path.read_text(encoding="utf-8")
                except Exception as exc:
                    logger.warning(f"Failed to read skill file {skill_path}: {exc}")
        logger.warning(f"System skill '{skill_name}' not found in {skills_dir}")
        return None

    def build_agent_prompt_sections(self, agent_key: str) -> List[PromptSection]:
        """Build structured prompt sections for an agent."""
        agent_config = self.get_agent(agent_key)

        if agent_config.custom_prompt_skill:
            skill_content = self._load_skill_file(agent_config.custom_prompt_skill)
            base_prompt = skill_content if skill_content is not None else self.get_prompt_template(agent_config.base_prompt)
        elif agent_config.custom_prompt:
            base_prompt = agent_config.custom_prompt
        else:
            base_prompt = self.get_prompt_template(agent_config.base_prompt)

        sections = [
            PromptSection(
                key="base_prompt",
                content=base_prompt,
                scope="static",
            )
        ]

        tool_descriptions = []
        for tool_name in agent_config.tools:
            try:
                tool_config = self.get_tool(tool_name)
                if tool_config.prompt_addition:
                    tool_descriptions.append(tool_config.prompt_addition)
            except ConfigError:
                logger.warning(f"Tool '{tool_name}' not found for agent '{agent_key}'")

        common_rules = getattr(self.config.settings, 'tools_common_rules', None)
        if common_rules:
            sections.append(
                PromptSection(
                    key="tools_common_rules",
                    content="\nПравила использования инструментов (общие):\n" + str(common_rules),
                    scope="static",
                )
            )
        if tool_descriptions:
            sections.append(
                PromptSection(
                    key="tool_capabilities",
                    content="\n".join(["\nДоступные инструменты:", *tool_descriptions]),
                    scope="static",
                )
            )

        return sections
    
    def _legacy_build_agent_prompt(self, agent_key: str) -> str:
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
    
    def build_agent_prompt(self, agent_key: str) -> str:
        """Build complete prompt for agent including tool descriptions."""
        return "\n".join(
            section.content
            for section in self.build_agent_prompt_sections(agent_key)
            if section.content
        )

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

    def get_improvement_config(self):
        """Get controlled self-improvement configuration."""
        return self.config.improvement

    def get_system_registry_path(self) -> str:
        """Get absolute path to the system platform registry."""
        registry_path = self.config.settings.platform.registry_path
        return self.get_absolute_path(registry_path)

    def get_pattern_registry_path(self) -> str:
        """Get absolute path to the pattern registry used by the platform."""
        registry_path = self.config.settings.platform.pattern_registry_path
        return self.get_absolute_path(registry_path)
    
    def get_agent_timeout(self) -> int:
        """Get agent execution timeout in seconds."""
        return self.config.settings.agent_timeout

    def get_proxy(self) -> Optional[str]:
        """Get proxy URL for outgoing requests (API, Telegram, etc.).
        Priority: settings.proxy -> telegram.proxy -> HTTPS_PROXY -> HTTP_PROXY."""
        if getattr(self.config.settings, "proxy", None):
            return self.config.settings.proxy
        if self.config.telegram and self.config.telegram.get("proxy"):
            return self.config.telegram["proxy"]
        return os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")

    @staticmethod
    def _is_local_or_private_host(host: Optional[str]) -> bool:
        """
        Return True if host is localhost or a private/LAN IP.

        This is used to prevent routing local providers (e.g. LM Studio on LAN)
        through a global proxy intended for external APIs.
        """
        if not host:
            return False
        h = host.strip().lower()
        if h in ("localhost", "127.0.0.1", "::1"):
            return True
        if h.endswith(".local"):
            return True
        try:
            ip = ipaddress.ip_address(h)
            return bool(
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
            )
        except ValueError:
            # Not an IP (domain name). Treat as non-local unless it matches known patterns.
            return False

    def get_proxy_for_provider(self, provider_key: Optional[str] = None) -> Optional[str]:
        """
        Provider-aware proxy resolution.

        Rule:
        - For providers with local/private base_url host (LAN/localhost), return None.
        - Otherwise return global proxy (settings.proxy / telegram.proxy / env).
        """
        if not provider_key:
            return self.get_proxy()
        try:
            provider = self.get_provider(provider_key)
            base_url = getattr(provider, "base_url", None) or ""
            host = urlparse(base_url).hostname
            if self._is_local_or_private_host(host):
                return None
        except Exception:
            # If we cannot resolve provider details, fall back to global proxy.
            pass
        return self.get_proxy()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get arbitrary configuration value using dot notation.
        
        Args:
            key: Dot-separated key path (e.g., 'memory_optimizer.consolidation_batch_size')
            default: Default value if key not found
            
        Returns:
            Configuration value or default
            
        Example:
            config.get('memory_optimizer.consolidation_batch_size', 5)
        """
        try:
            keys = key.split('.')
            value = self.config
            for k in keys:
                # Try as attribute first (for Pydantic models)
                if hasattr(value, k):
                    value = getattr(value, k)
                elif isinstance(value, dict) and k in value:
                    value = value[k]
                else:
                    return default
            return value if value is not None else default
        except Exception:
            return default
