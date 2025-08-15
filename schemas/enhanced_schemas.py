"""
Enhanced Pydantic schemas for improved configuration validation.
Provides comprehensive validation, type checking, and configuration management.
"""

from typing import Dict, List, Any, Optional, Union
from pydantic import BaseModel, Field, validator, model_validator
from enum import Enum


class ProviderType(str, Enum):
    """Supported AI providers."""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LMSTUDIO = "lmstudio"
    OPENROUTER = "openrouter"
    CUSTOM = "custom"


class ToolType(str, Enum):
    """Types of tools."""
    FUNCTION = "function"
    AGENT = "agent"
    MCP = "mcp"
    COMPOSITE = "composite"


class GuardrailType(str, Enum):
    """Types of guardrails."""
    INPUT_VALIDATION = "input_validation"
    PATH_SAFETY = "path_safety"
    CODE_SAFETY = "code_safety"
    OUTPUT_SANITIZATION = "output_sanitization"
    TASK_VALIDATION = "task_validation"


class ProfileTemplate(str, Enum):
    """Agent profile templates."""
    FILE_OPERATIONS = "file_operations"
    DEVELOPMENT = "development"
    ANALYSIS = "analysis"
    COORDINATION = "coordination"
    SECURITY = "security"


class EnhancedProviderConfig(BaseModel):
    """Enhanced provider configuration with validation."""
    
    name: str = Field(..., min_length=1, description="Provider display name")
    provider_type: ProviderType = Field(default=ProviderType.OPENAI, description="Provider type")
    base_url: str = Field(..., description="API base URL")
    api_key_env: str = Field(..., min_length=1, description="Environment variable for API key")
    timeout: float = Field(default=30.0, ge=1.0, le=300.0, description="Request timeout in seconds")
    max_retries: int = Field(default=2, ge=0, le=10, description="Maximum retry attempts")
    headers: Dict[str, str] = Field(default_factory=dict, description="Additional headers")
    rate_limit: Optional[Dict[str, Any]] = Field(default=None, description="Rate limiting configuration")
    
    @validator('base_url')
    def validate_base_url(cls, v):
        """Validate base URL format."""
        if not v.startswith(('http://', 'https://')):
            raise ValueError('Base URL must start with http:// or https://')
        return v.rstrip('/')
    
    @validator('api_key_env')
    def validate_api_key_env(cls, v):
        """Validate API key environment variable name."""
        if not v.isupper():
            raise ValueError('API key environment variable should be uppercase')
        return v


class EnhancedModelConfig(BaseModel):
    """Enhanced model configuration with validation."""
    
    name: str = Field(..., min_length=1, description="Model name")
    provider: str = Field(..., min_length=1, description="Provider key")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="Model temperature")
    max_tokens: int = Field(default=4000, ge=1, le=200000, description="Maximum tokens")
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Top-p sampling")
    frequency_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0, description="Frequency penalty")
    presence_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0, description="Presence penalty")
    use_responses_api: bool = Field(default=False, description="Use Responses API for reasoning models")
    description: str = Field(default="", description="Model description")
    capabilities: List[str] = Field(default_factory=list, description="Model capabilities")
    context_window: Optional[int] = Field(default=None, ge=1, description="Context window size")
    
    @validator('max_tokens')
    def validate_max_tokens(cls, v, values):
        """Validate max_tokens against context window."""
        context_window = values.get('context_window')
        if context_window and v > context_window:
            raise ValueError('max_tokens cannot exceed context_window')
        return v


class EnhancedToolConfig(BaseModel):
    """Enhanced tool configuration with validation."""
    
    name: str = Field(..., min_length=1, description="Tool name")
    type: ToolType = Field(..., description="Tool type")
    description: str = Field(default="", description="Tool description")
    category: str = Field(default="general", description="Tool category")
    tags: List[str] = Field(default_factory=list, description="Tool tags")
    dependencies: List[str] = Field(default_factory=list, description="Tool dependencies")
    
    # Function tool specific
    function_name: Optional[str] = Field(default=None, description="Function name for function tools")
    parameters: Optional[Dict[str, Any]] = Field(default=None, description="Function parameters schema")
    
    # Agent tool specific
    agent_key: Optional[str] = Field(default=None, description="Agent key for agent tools")
    context_strategy: str = Field(default="conversation", description="Context sharing strategy")
    context_depth: int = Field(default=5, ge=1, le=50, description="Context depth")
    include_tool_history: bool = Field(default=True, description="Include tool execution history")
    
    # MCP tool specific
    server_command: Optional[List[str]] = Field(default=None, description="MCP server command")
    env_vars: Optional[Dict[str, str]] = Field(default=None, description="Environment variables")
    
    # Composite tool specific
    tool_chain: Optional[List[str]] = Field(default=None, description="Tool chain for composite tools")
    
    @model_validator(mode='before')
    def validate_tool_specific_fields(cls, values):
        """Validate tool-specific field requirements."""
        tool_type = values.get('type')
        
        if tool_type == ToolType.FUNCTION:
            if not values.get('function_name'):
                raise ValueError('Function tools require function_name')
        
        elif tool_type == ToolType.AGENT:
            if not values.get('agent_key'):
                raise ValueError('Agent tools require agent_key')
        
        elif tool_type == ToolType.MCP:
            if not values.get('server_command'):
                raise ValueError('MCP tools require server_command')
        
        elif tool_type == ToolType.COMPOSITE:
            tool_chain = values.get('tool_chain')
            if not tool_chain or len(tool_chain) < 2:
                raise ValueError('Composite tools require at least 2 tools in tool_chain')
        
        return values


class GuardrailConfig(BaseModel):
    """Guardrail configuration."""
    
    name: str = Field(..., min_length=1, description="Guardrail name")
    type: GuardrailType = Field(..., description="Guardrail type")
    enabled: bool = Field(default=True, description="Whether guardrail is enabled")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Guardrail parameters")
    description: str = Field(default="", description="Guardrail description")


class AgentProfileConfig(BaseModel):
    """Agent profile configuration."""
    
    name: str = Field(..., min_length=1, description="Profile name")
    template: ProfileTemplate = Field(..., description="Profile template")
    capabilities: List[str] = Field(..., min_items=1, description="Agent capabilities")
    tools: List[str] = Field(default_factory=list, description="Default tools")
    guardrails: List[str] = Field(default_factory=list, description="Default guardrails")
    max_tools_per_turn: int = Field(default=3, ge=1, le=20, description="Maximum tools per turn")
    context_strategy: str = Field(default="conversation", description="Context strategy")
    base_instructions: str = Field(default="", description="Base instructions")
    
    @validator('capabilities')
    def validate_capabilities(cls, v):
        """Validate capabilities list."""
        if not v:
            raise ValueError('Agent profile must have at least one capability')
        return [cap.lower() for cap in v]


class EnhancedAgentConfig(BaseModel):
    """Enhanced agent configuration with validation."""
    
    name: str = Field(..., min_length=1, description="Agent display name")
    model: str = Field(..., min_length=1, description="Model key")
    tools: List[str] = Field(default_factory=list, description="Tool keys")
    base_prompt: str = Field(default="base", description="Base prompt template")
    custom_prompt: Optional[str] = Field(default=None, description="Custom prompt override")
    description: str = Field(default="", description="Agent description")
    
    # Profile support
    profile: Optional[str] = Field(default=None, description="Agent profile key")
    required_capabilities: List[str] = Field(default_factory=list, description="Required capabilities")
    
    # Guardrails
    guardrails: List[str] = Field(default_factory=list, description="Guardrail keys")
    
    # Performance settings
    max_tools_per_turn: int = Field(default=3, ge=1, le=20, description="Maximum tools per turn")
    timeout: Optional[float] = Field(default=None, ge=1.0, le=600.0, description="Agent timeout")
    
    # Context settings
    context_strategy: str = Field(default="conversation", description="Context strategy")
    context_depth: int = Field(default=10, ge=1, le=100, description="Context depth")
    
    # Feature flags
    mcp_enabled: Optional[bool] = Field(default=None, description="MCP enabled for this agent")
    streaming_enabled: bool = Field(default=True, description="Streaming enabled")
    
    @validator('tools')
    def validate_tools_limit(cls, v):
        """Validate tool count limit."""
        if len(v) > 50:
            raise ValueError('Agent cannot have more than 50 tools')
        return v
    
    @validator('name')
    def validate_name_format(cls, v):
        """Validate agent name format."""
        if not v.replace(' ', '').replace('_', '').replace('-', '').isalnum():
            raise ValueError('Agent name must contain only alphanumeric characters, spaces, underscores, and hyphens')
        return v


class EnhancedSettings(BaseModel):
    """Enhanced system settings with validation."""
    
    default_agent: str = Field(..., min_length=1, description="Default agent key")
    max_history: int = Field(default=15, ge=1, le=1000, description="Maximum conversation history")
    max_turns: int = Field(default=50, ge=1, le=1000, description="Maximum turns per agent execution")
    debug: bool = Field(default=False, description="Debug mode enabled")
    agent_timeout: float = Field(default=300.0, ge=1.0, le=3600.0, description="Agent timeout in seconds")
    
    # Directory settings
    working_directory: str = Field(default=".", description="Working directory")
    config_directory: str = Field(default=".", description="Configuration directory")
    allow_path_override: bool = Field(default=True, description="Allow path override")
    
    # Feature flags
    mcp_enabled: bool = Field(default=True, description="MCP globally enabled")
    metrics_enabled: bool = Field(default=True, description="Metrics collection enabled")
    evals_enabled: bool = Field(default=True, description="Evaluations enabled")
    
    # Agent logging
    agent_logging: Dict[str, Any] = Field(default_factory=dict, description="Agent logging configuration")
    
    # Performance settings
    parallel_tool_execution: bool = Field(default=False, description="Allow parallel tool execution")
    tool_timeout: float = Field(default=60.0, ge=1.0, le=600.0, description="Tool execution timeout")
    
    # Security settings
    enforce_guardrails: bool = Field(default=True, description="Enforce guardrails globally")
    allowed_file_extensions: List[str] = Field(
        default_factory=lambda: ['.txt', '.md', '.py', '.js', '.json', '.yaml', '.yml'],
        description="Allowed file extensions for file operations"
    )
    
    @validator('working_directory', 'config_directory')
    def validate_directory_paths(cls, v):
        """Validate directory paths."""
        import os
        if not os.path.isdir(v):
            raise ValueError(f'Directory does not exist: {v}')
        return os.path.abspath(v)


class EnhancedConfig(BaseModel):
    """Enhanced main configuration with validation."""
    
    settings: EnhancedSettings = Field(..., description="System settings")
    providers: Dict[str, EnhancedProviderConfig] = Field(..., description="Provider configurations")
    models: Dict[str, EnhancedModelConfig] = Field(..., description="Model configurations")
    tools: Dict[str, EnhancedToolConfig] = Field(default_factory=dict, description="Tool configurations")
    agents: Dict[str, EnhancedAgentConfig] = Field(..., description="Agent configurations")
    
    # Enhanced features
    agent_profiles: Dict[str, AgentProfileConfig] = Field(default_factory=dict, description="Agent profiles")
    guardrails: Dict[str, GuardrailConfig] = Field(default_factory=dict, description="Guardrail configurations")
    
    # Prompt templates
    prompt_templates: Dict[str, str] = Field(default_factory=dict, description="Prompt templates")
    
    @model_validator(mode='before')
    def validate_references(cls, values):
        """Validate cross-references between configurations."""
        settings = values.get('settings')
        providers = values.get('providers', {})
        models = values.get('models', {})
        agents = values.get('agents', {})
        tools = values.get('tools', {})
        agent_profiles = values.get('agent_profiles', {})
        guardrails = values.get('guardrails', {})
        
        errors = []
        
        # Validate default agent exists
        if settings and settings.default_agent not in agents:
            errors.append(f"Default agent '{settings.default_agent}' not found in agents")
        
        # Validate model provider references
        for model_key, model_config in models.items():
            if model_config.provider not in providers:
                errors.append(f"Model '{model_key}' references unknown provider '{model_config.provider}'")
        
        # Validate agent model references
        for agent_key, agent_config in agents.items():
            if agent_config.model not in models:
                errors.append(f"Agent '{agent_key}' references unknown model '{agent_config.model}'")
            
            # Validate agent tool references
            for tool_key in agent_config.tools:
                if tool_key not in tools and tool_key not in agents:
                    errors.append(f"Agent '{agent_key}' references unknown tool/agent '{tool_key}'")
            
            # Validate agent profile references
            if agent_config.profile and agent_config.profile not in agent_profiles:
                errors.append(f"Agent '{agent_key}' references unknown profile '{agent_config.profile}'")
            
            # Validate guardrail references
            for guardrail_key in agent_config.guardrails:
                if guardrail_key not in guardrails:
                    errors.append(f"Agent '{agent_key}' references unknown guardrail '{guardrail_key}'")
        
        # Validate tool dependencies
        for tool_key, tool_config in tools.items():
            for dep in tool_config.dependencies:
                if dep not in tools:
                    errors.append(f"Tool '{tool_key}' depends on unknown tool '{dep}'")
            
            # Validate agent tool references
            if tool_config.type == ToolType.AGENT and tool_config.agent_key:
                if tool_config.agent_key not in agents:
                    errors.append(f"Tool '{tool_key}' references unknown agent '{tool_config.agent_key}'")
        
        if errors:
            raise ValueError("Configuration validation errors:\n" + "\n".join(f"- {error}" for error in errors))
        
        return values
    
    class Config:
        """Pydantic configuration."""
        validate_assignment = True
        extra = "forbid"
        use_enum_values = True


class ConfigValidator:
    """Utility class for configuration validation and enhancement."""
    
    @staticmethod
    def validate_config_file(config_data: Dict[str, Any]) -> EnhancedConfig:
        """Validate configuration from file data."""
        try:
            return EnhancedConfig(**config_data)
        except Exception as e:
            raise ValueError(f"Configuration validation failed: {str(e)}")
    
    @staticmethod
    def validate_agent_config(agent_data: Dict[str, Any]) -> EnhancedAgentConfig:
        """Validate individual agent configuration."""
        try:
            return EnhancedAgentConfig(**agent_data)
        except Exception as e:
            raise ValueError(f"Agent configuration validation failed: {str(e)}")
    
    @staticmethod
    def get_validation_errors(config_data: Dict[str, Any]) -> List[str]:
        """Get list of validation errors without raising exception."""
        try:
            EnhancedConfig(**config_data)
            return []
        except Exception as e:
            if hasattr(e, 'errors'):
                return [f"{'.'.join(str(loc) for loc in error['loc'])}: {error['msg']}" 
                       for error in e.errors()]
            else:
                return [str(e)]
    
    @staticmethod
    def suggest_fixes(config_data: Dict[str, Any]) -> List[str]:
        """Suggest fixes for common configuration issues."""
        suggestions = []
        
        # Check for common issues
        if 'settings' not in config_data:
            suggestions.append("Add 'settings' section with required configuration")
        
        if 'providers' not in config_data or not config_data['providers']:
            suggestions.append("Add at least one provider configuration")
        
        if 'models' not in config_data or not config_data['models']:
            suggestions.append("Add at least one model configuration")
        
        if 'agents' not in config_data or not config_data['agents']:
            suggestions.append("Add at least one agent configuration")
        
        # Check default agent
        settings = config_data.get('settings', {})
        agents = config_data.get('agents', {})
        default_agent = settings.get('default_agent')
        
        if default_agent and default_agent not in agents:
            available_agents = list(agents.keys())
            if available_agents:
                suggestions.append(f"Set default_agent to one of: {', '.join(available_agents)}")
            else:
                suggestions.append("Create an agent configuration for the default_agent")
        
        return suggestions