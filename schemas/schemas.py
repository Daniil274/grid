"""
Legacy schemas for backward compatibility.
These are copies of the main schemas to avoid circular imports.
"""

from typing import List, Dict, Any, Optional, Union, Literal
from pydantic import BaseModel, Field, field_validator
from enum import Enum


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
    timeout: int = Field(default=30, ge=1, le=300)
    max_retries: int = Field(default=2, ge=0, le=10)


class ModelConfig(BaseModel):
    """Configuration for LLM models."""
    name: str
    provider: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4000, ge=1, le=100000)
    description: str = ""
    use_responses_api: bool = False


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
    model: str
    tools: List[str] = Field(default_factory=list)
    base_prompt: str = "base"
    custom_prompt: Optional[str] = None
    description: str = ""
    mcp_enabled: bool = False


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


class Settings(BaseModel):
    """Global system settings."""
    default_agent: str = "assistant"
    max_history: int = Field(default=15, ge=1, le=100)
    max_turns: int = Field(default=10, ge=1, le=100)
    agent_timeout: int = Field(default=300, ge=30, le=1800)
    debug: bool = False
    mcp_enabled: bool = False
    working_directory: str = "."
    config_directory: str = "."
    allow_path_override: bool = True
    agent_logging: AgentLoggingConfig = Field(default_factory=AgentLoggingConfig)
    image_processing: ImageProcessingConfig = Field(default_factory=ImageProcessingConfig)
    tools_common_rules: Optional[str] = None


class GridConfig(BaseModel):
    """Complete Grid system configuration."""
    settings: Settings = Field(default_factory=Settings)
    providers: Dict[str, ProviderConfig] = Field(default_factory=dict)
    models: Dict[str, ModelConfig] = Field(default_factory=dict)
    tools: Dict[str, ToolConfig] = Field(default_factory=dict)
    agents: Dict[str, AgentConfig] = Field(default_factory=dict)
    prompt_templates: Dict[str, str] = Field(default_factory=dict)
    scenarios: Optional[Dict[str, Any]] = None
    
    @field_validator('agents')
    @classmethod
    def validate_agent_models(cls, v, info):
        """Validate that all agent models exist."""
        models = info.data.get('models', {})
        for agent_key, agent_config in v.items():
            if agent_config.model not in models:
                raise ValueError(f"Model '{agent_config.model}' for agent '{agent_key}' not found")
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
