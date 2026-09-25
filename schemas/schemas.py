"""
Legacy schemas for backward compatibility.
These are copies of the main schemas to avoid circular imports.
"""

from typing import List, Dict, Any, Optional, Union, Literal
from pydantic import BaseModel, Field, field_validator
from enum import Enum
from .action_policy import ActionPolicyConfig


class ToolType(str, Enum):
    """Tool types."""
    FUNCTION = "function"
    MCP = "mcp"
    AGENT = "agent"


class ProviderConfig(BaseModel):
    """Configuration for LLM providers."""
    name: str
    base_url: str
    api_key_env: Optional[str] = None
    api_key: Optional[str] = None
    default_headers: Dict[str, str] = Field(default_factory=dict)
    timeout: int = Field(default=30, ge=1, le=300)
    max_retries: int = Field(default=2, ge=0, le=1000)


class ModelConfig(BaseModel):
    """Configuration for LLM models."""
    name: str
    provider: str
    policy_api: Literal["decisions", "chat"] = "decisions"
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4000, ge=1, le=100000)
    context_window: int = Field(
        default=128000, ge=1024,
        description="Model context window size in tokens. Used for auto-compact."
    )
    description: str = ""
    use_responses_api: bool = False
    reasoning: Optional[Dict[str, Any]] = None
    """Reasoning control. Use one of:
      reasoning: {effort: "none"}     — SDK-native (OpenAI reasoning_effort param)
      reasoning: {enabled: false}     — via extra_body (OpenRouter / any provider)
    """
    capabilities: List[str] = Field(default_factory=list)
    """Model capability flags used for modality-aware tool filtering.
    Examples: ["vision", "text", "code", "audio", "reasoning"]
    Models without this field are treated as ["text"] only.
    """
    request_timeout: Optional[float] = Field(default=None, gt=0, le=300)
    """Seconds one Decisions API request to this model may take (the action
    policy validator, the router). A slower request is abandoned, so a caller
    that retries - the policy validator does, within its own budget - gets a
    fresh attempt instead of waiting out a stalled one. Defaults to the
    provider's timeout.
    """
    preserve_reasoning_content: bool = False
    """When True, reasoning_content from thinking-enabled models is preserved in
    assistant messages that contain tool_calls. Required for providers like
    Moonshot AI (kimi) that enable thinking by default and reject requests where
    reasoning_content is missing from the conversation history.
    """


class ToolConfig(BaseModel):
    """Configuration for tools."""
    type: ToolType
    name: Optional[str] = None
    description: str = ""
    prompt_addition: Optional[str] = None
    
    # For MCP tools
    server_command: Optional[List[str]] = None
    env_vars: Optional[Dict[str, str]] = None
    add_working_directory: Optional[bool] = None
    
    # For agent tools
    target_agent: Optional[str] = None
    context_strategy: Optional[str] = None
    context_depth: Optional[int] = None
    include_tool_history: Optional[bool] = None


class AgentConfig(BaseModel):
    """Configuration for agents."""
    name: str
    model: Union[str, List[str]]
    tools: List[str] = Field(default_factory=list)
    base_prompt: str = "base"
    custom_prompt: Optional[str] = None
    system_skills: List[str] = Field(default_factory=list)
    description: str = ""
    routable: bool = Field(
        default=True,
        description="Whether the router may pick this agent for a user message. Disable for background agents."
    )
    mcp_enabled: bool = False
    auto_run_tools: Optional[List[Dict[str, Any]]] = Field(
        default=None, 
        description="List of tools to run automatically on agent startup. Each item: {'name': 'tool_name', 'parameters': {}}"
    )
    parallel_tool_calls: bool = Field(
        default=False,
        description=(
            "Let the model emit several tool calls in one response. The calls "
            "still run one after another; the gain is one model turn per batch."
        ),
    )

    @field_validator("model")
    @classmethod
    def validate_model_chain(cls, value):
        """Accept one model key or a non-empty ordered fallback chain."""
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("Agent model must not be empty")
            return value
        cleaned = [str(model).strip() for model in value]
        if not cleaned or any(not model for model in cleaned):
            raise ValueError("Agent model fallback list must contain model keys")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("Agent model fallback list must not contain duplicates")
        return cleaned

    def model_keys(self) -> List[str]:
        """Return the configured model chain in request order."""
        return [self.model] if isinstance(self.model, str) else list(self.model)

    @property
    def primary_model(self) -> str:
        """Model key used for metadata and context-window defaults."""
        return self.model_keys()[0]


class AgentLoggingConfig(BaseModel):
    """Configuration for agent logging."""
    enabled: bool = True
    level: str = "full"
    save_prompts: bool = True
    save_conversations: bool = True
    save_executions: bool = True


class ImageProcessingConfig(BaseModel):
    """Configuration for image processing."""
    enabled: bool = True
    auto_resize: bool = True
    max_width: int = Field(default=512, ge=64, le=4096)
    max_height: int = Field(default=512, ge=64, le=4096)
    max_file_size_mb: int = Field(default=10, ge=1, le=100)
    jpeg_quality: int = Field(default=70, ge=1, le=100)


class IsolationConfig(BaseModel):
    """Configuration for agent isolation."""
    enabled: bool = False
    type: str = "docker"
    image: str = "grid-agent:latest"


class ProjectToolsConfig(BaseModel):
    """Configuration for project-specific tools loading."""
    enabled: bool = False
    tools_directory: str = "./tools"
    base_tools: List[str] = Field(default_factory=list)


class Settings(BaseModel):
    """Global system settings."""
    action_policy: ActionPolicyConfig = Field(default_factory=ActionPolicyConfig)
    default_agent: str = "assistant"
    max_history: int = Field(default=15, ge=1, le=1000)
    max_turns: int = Field(default=10, ge=1, le=300)
    agent_timeout: int = Field(default=300, ge=30, le=1800)
    debug: bool = False
    mcp_enabled: bool = False
    project_tools: Optional[ProjectToolsConfig] = Field(default=None)
    working_directory: str = "."
    config_directory: str = "."
    logs_directory: Optional[str] = Field(
        default=None,
        description="Logs directory. When unset, defaults to ~/.grid/logs.",
    )
    allow_path_override: bool = True
    agent_logging: AgentLoggingConfig = Field(default_factory=AgentLoggingConfig)
    image_processing: ImageProcessingConfig = Field(default_factory=ImageProcessingConfig)
    tools_common_rules: Optional[str] = None
    allowed_models: Optional[List[str]] = Field(
        default=None,
        description="Whitelist of allowed model keys. If None or empty, all models are allowed."
    )
    max_tool_output: Optional[int] = Field(
        default=None,
        ge=100,
        description="Maximum tool output length in characters. None = no limit."
    )
    max_tool_output_tokens: Optional[int] = Field(
        default=None,
        ge=100,
        description="Maximum number of tokens in tool output. If exceeded, agent gets an error. None = no limit."
    )
    proxy: Optional[str] = Field(
        default=None,
        description="Proxy for all outgoing requests. Example: http://127.0.0.1:10809"
    )


class RoutedSystemConfig(BaseModel):
    """A Grid system the router can send a user message to."""
    config: str = Field(description="Path to the system's config.yaml, relative to this config file")
    requires: List[str] = Field(default_factory=list, description="External programs that must be on PATH, e.g. ffmpeg")
    description: str = Field(default="", description="What the system is for; the router picks by this text")


class RoutingConfig(BaseModel):
    """Automatic routing of a user message to a system and then to one of its agents."""
    model: Optional[str] = Field(
        default=None,
        description="Key from models: used as the router. Routing is off while unset."
    )
    api: Literal["chat", "decisions"] = Field(
        default="chat",
        description="'decisions' for decisions models (e.g. typesafe/jev-1.13), 'chat' for regular chat models"
    )
    systems: Dict[str, RoutedSystemConfig] = Field(
        default_factory=dict,
        description="Systems to choose from. Empty means only this config's own agents are routed."
    )
    default_system: Optional[str] = Field(
        default=None,
        description="System used when the router fails. Defaults to the first listed system."
    )


class GridConfig(BaseModel):
    """Complete Grid system configuration."""
    settings: Settings = Field(default_factory=Settings)
    isolation: IsolationConfig = Field(default_factory=IsolationConfig)
    compact: "CompactConfig" = Field(default_factory=lambda: CompactConfig())
    providers: Dict[str, ProviderConfig] = Field(default_factory=dict)
    models: Dict[str, ModelConfig] = Field(default_factory=dict)
    tools: Dict[str, ToolConfig] = Field(default_factory=dict)
    agents: Dict[str, AgentConfig] = Field(default_factory=dict)
    prompt_templates: Dict[str, str] = Field(default_factory=dict)
    scenarios: Optional[Dict[str, Any]] = None
    voice: Dict[str, Any] = Field(default_factory=dict, description="Local web speech and voice routing settings")
    routing: RoutingConfig = Field(default_factory=RoutingConfig, description="Automatic system and agent routing")
    
    @field_validator('agents')
    @classmethod
    def validate_agent_models(cls, v, info):
        """Validate that all agent models exist."""
        models = info.data.get('models', {})
        for agent_key, agent_config in v.items():
            for model_key in agent_config.model_keys():
                if model_key not in models:
                    raise ValueError(f"Model '{model_key}' for agent '{agent_key}' not found")
        return v
    
    @field_validator('agents')
    @classmethod
    def validate_agent_tools(cls, v, info):
        """Validate that all agent tools exist."""
        tools = info.data.get('tools', {})
        for agent_key, agent_config in v.items():
            for tool_name in agent_config.tools:
                if tool_name not in tools:
                    raise ValueError(f"Tool '{tool_name}' for agent '{agent_key}' not found")
        return v


class ImageUrl(BaseModel):
    """Image URL or base64 data."""
    url: str = Field(..., description="URL or base64-encoded image data (data:image/...;base64,...)")
    detail: Optional[Literal["auto", "low", "high"]] = Field(default="auto", description="Image detail level")


class ImageContent(BaseModel):
    """Image content part."""
    type: Literal["image_url"] = "image_url"
    image_url: ImageUrl


class TextContent(BaseModel):
    """Text content part."""
    type: Literal["text"] = "text"
    text: str


class FileImageContent(BaseModel):
    """File path image content (internal use)."""
    type: Literal["image_file"] = "image_file"
    file_path: str = Field(..., description="Local file path to image")
    detail: Optional[Literal["auto", "low", "high"]] = Field(default="auto", description="Image detail level")


ContentPart = Union[TextContent, ImageContent, FileImageContent]


class ContextMessage(BaseModel):
    """Message in conversation context with multimodal support."""
    role: str = Field(..., pattern=r'^(user|assistant|system)$')
    content: Union[str, List[ContentPart]] = Field(
        ...,
        description="Message content: string for text-only or list of content parts for multimodal"
    )
    timestamp: str
    metadata: Optional[Dict[str, Any]] = None

    def get_text_content(self) -> str:
        """Extract text content from message."""
        if isinstance(self.content, str):
            return self.content

        text_parts = []
        for part in self.content:
            if isinstance(part, TextContent):
                text_parts.append(part.text)
            elif isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))

        return " ".join(text_parts) if text_parts else "[multimodal content]"

    def get_images(self) -> List[Union[ImageContent, FileImageContent]]:
        """Extract image content from message."""
        if isinstance(self.content, str):
            return []

        images = []
        for part in self.content:
            if isinstance(part, (ImageContent, FileImageContent)):
                images.append(part)
            elif isinstance(part, dict) and part.get("type") in ["image_url", "image_file"]:
                if part.get("type") == "image_url":
                    images.append(ImageContent(**part))
                else:
                    images.append(FileImageContent(**part))

        return images

    def has_images(self) -> bool:
        """Check if message contains images."""
        return len(self.get_images()) > 0


class AgentExecution(BaseModel):
    """Agent execution tracking."""
    agent_name: str
    input_message: str
    start_time: float
    context_id: Optional[str] = None
    end_time: Optional[float] = None
    output: Optional[str] = None
    error: Optional[str] = None
    tools_used: List[str] = Field(default_factory=list)
    token_usage: Optional[Dict[str, int]] = None


# =============================================================================
# COMPACT SYSTEM CONFIGURATION
# =============================================================================

class CompactMicroConfig(BaseModel):
    """Configuration for microcompact (tool result clearing)."""
    enabled: bool = True
    max_age_hours: float = Field(default=1.0, ge=0.1, description="Clear results older than this")
    gap_threshold_minutes: float = Field(
        default=60.0, ge=1.0,
        description="Minimum gap (min) since last assistant response for time-based microcompact"
    )
    preserve_last_n: int = Field(default=5, ge=1, description="Keep last N tool results")
    compactable_tools: List[str] = Field(
        default_factory=lambda: [
            "read_file", "read_text_file", "read_media_file",
            "list_directory", "list_directory_with_sizes", "directory_tree",
            "search_files", "execute_command",
            "web_fetch", "web_search",
            "git_diff", "git_log"
        ],
        description="Tools whose results can be cleared"
    )


class CompactAutoConfig(BaseModel):
    """Configuration for automatic compaction trigger."""
    enabled: bool = True
    buffer_tokens: int = Field(default=13000, ge=1000, description="Token buffer before auto-compact threshold")
    warning_buffer_tokens: int = Field(default=20000, ge=1000, description="Buffer before warning threshold")
    error_buffer_tokens: int = Field(default=20000, ge=1000, description="Buffer before error threshold")
    manual_buffer_tokens: int = Field(default=3000, ge=100, description="Buffer before hard lock (manual compact)")
    max_output_tokens_for_summary: int = Field(default=20000, ge=1000, description="Reserve tokens for LLM summary")
    max_consecutive_failures: int = Field(default=3, ge=1, description="Circuit breaker: max consecutive errors")


class CompactConfig(BaseModel):
    """Complete configuration for the compact system.
    
    Compact manages context window limits through intelligent compaction:
    - Microcompact: Tool result clearing for token efficiency
    - Auto Compact: Automatic triggering on threshold
    - Reactive Compact: Handle context_length_exceeded errors
    """
    enabled: bool = True
    micro: CompactMicroConfig = Field(default_factory=CompactMicroConfig)
    auto: CompactAutoConfig = Field(default_factory=CompactAutoConfig)
    
    # Model for summarization (optional, uses default if None)
    summary_model: Optional[str] = None
    summary_max_output_tokens: int = Field(default=20000, ge=1000)


GridConfig.model_rebuild()
