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
    model: str
    tools: List[str] = Field(default_factory=list)
    base_prompt: str = "base"
    custom_prompt: Optional[str] = None
    system_skills: List[str] = Field(default_factory=list)
    description: str = ""
    mcp_enabled: bool = False
    auto_run_tools: Optional[List[Dict[str, Any]]] = Field(
        default=None, 
        description="List of tools to run automatically on agent startup. Each item: {'name': 'tool_name', 'parameters': {}}"
    )


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


class SerialConfig(BaseModel):
    """Configuration for serial communication (retries and delay after command)."""
    retries: int = Field(default=3, ge=1, le=50, description="Number of retries per command (ISKOR_RETRIES)")
    command_timeout_sec: float = Field(default=0.5, ge=0.0, le=60.0, description="Pause in seconds after executing command (ISKOR_COMMAND_TIMEOUT)")
    # Extra fields from project config (baud, timeout, etc.) are allowed via model_config
    model_config = {"extra": "ignore"}


class WindowConfig(BaseModel):
    """Configuration for GUI window automation."""
    title_pattern: str = Field(default="ISKOR", description="Window title search pattern (ISKOR_WINDOW_TITLE)")
    key_delay_sec: float = Field(default=0.3, ge=0.0, le=10.0, description="Pause after key press (ISKOR_KEY_DELAY)")
    screenshot_delay_sec: float = Field(default=0.2, ge=0.0, le=10.0, description="Additional pause before screenshot (ISKOR_SCREEN_DELAY)")
    model_config = {"extra": "ignore"}


class PlatformConfig(BaseModel):
    """Configuration for the self-organizing system platform."""
    enabled: bool = False
    registry_path: str = "data/system_registry.json"
    pattern_registry_path: str = "data/pattern_registry.json"
    default_actor_role: str = "builder_agent"


class Settings(BaseModel):
    """Global system settings."""
    default_agent: str = "assistant"
    max_history: int = Field(default=15, ge=1, le=100)
    max_turns: int = Field(default=10, ge=1, le=300)
    agent_timeout: int = Field(default=300, ge=30, le=1800)
    debug: bool = False
    mcp_enabled: bool = False
    project_tools: Optional[ProjectToolsConfig] = Field(default=None)
    working_directory: str = "."
    config_directory: str = "."
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
        description="Proxy for all outgoing requests (API, Telegram). Example: http://127.0.0.1:10809"
    )
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    serial: Optional[SerialConfig] = Field(
        default=None,
        description="Serial settings for ISKOR: number of retries per command and command timeout"
    )
    window: Optional[WindowConfig] = Field(
        default=None,
        description="GUI automation settings: window title search, delays"
    )


class MemoryOptimizerConfig(BaseModel):
    """Configuration for memory optimizer."""
    consolidation_batch_size: int = Field(default=5, ge=1, le=100)
    consolidation_trigger: str = Field(default="on_save", description="on_save, periodic, or manual")
    consolidation_interval_seconds: int = Field(default=3600, ge=60)
    min_short_term_age_hours: float = Field(default=1.0, ge=0.0)


class EmbeddingsConfig(BaseModel):
    """Configuration for semantic search / embeddings."""
    model: str = Field(
        description="Key from models: section that points to an embedding model on OpenRouter"
    )
    request_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0,
        description="Timeout in seconds for each embedding API call"
    )
    persist: bool = Field(
        default=True,
        description="Persist ChromaDB vector index to disk (next to the SQLite memory DB)"
    )


class ImprovementConfig(BaseModel):
    """Configuration for the staged self-improvement loop."""

    enabled: bool = False
    registry_path: str = "data/improvement_registry.json"
    plans_directory: str = "plans"
    require_human_requirements_review: bool = True
    require_human_final_review: bool = True
    auto_promote_safe_changes: bool = False
    auto_evaluate_proposed_experiments: bool = False
    allowed_change_types: List[str] = Field(
        default_factory=lambda: ["tests", "logging", "refactor", "diagnostics"]
    )
    allowed_paths: List[str] = Field(default_factory=list)
    allowed_config_keys: List[str] = Field(default_factory=list)
    max_open_experiments: int = Field(default=5, ge=1, le=100)
    require_benchmark_before_promotion: bool = False
    promotion_threshold: float = Field(default=0.0, ge=-1.0, le=1.0)
    rollback_threshold: float = Field(default=-0.03, ge=-1.0, le=1.0)
    canary_window_minutes: int = Field(default=30, ge=1, le=1440)


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
    telegram: Optional[Dict[str, Any]] = None  # telegram bot config, incl. proxy for API requests
    memory_optimizer: Optional[MemoryOptimizerConfig] = Field(default=None, description="Memory optimizer configuration")
    embeddings: Optional[EmbeddingsConfig] = Field(default=None, description="Semantic search / embeddings configuration")
    improvement: ImprovementConfig = Field(default_factory=ImprovementConfig, description="Controlled self-improvement loop configuration")
    
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


# =============================================================================
# Social Intelligence Framework Schemas
# =============================================================================


class BlackboardConfig(BaseModel):
    """Configuration for blackboard shared memory."""
    enabled: bool = True
    persist_path: str = "logs/blackboard.json"
    max_entries: int = Field(default=1000, ge=100, le=10000)
    entry_ttl_hours: int = Field(default=24, ge=1, le=168)


class PipelineMemoryConfig(BaseModel):
    """Configuration for pipeline memory."""
    enabled: bool = True
    persist_path: str = "logs/pipeline_memory.json"
    similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    min_success_score: float = Field(default=0.6, ge=0.0, le=1.0)
    max_pipelines: int = Field(default=500, ge=10, le=5000)


class PrimitivesConfig(BaseModel):
    """Configuration for pipeline primitives."""
    default_validation: bool = True
    critique_model: Optional[str] = None
    validator_model: Optional[str] = None
    max_parallel_branches: int = Field(default=5, ge=1, le=20)


class MetaOrchestratorConfig(BaseModel):
    """Configuration for meta-orchestrator behavior."""
    auto_learn: bool = True
    reuse_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    exploration_rate: float = Field(default=0.2, ge=0.0, le=1.0)


class RefinementConfig(BaseModel):
    """Configuration for iterative refinement."""
    max_iterations: int = Field(default=2, ge=0, le=5)
    enabled: bool = True
    revise_model: Optional[str] = None


class SocialIntelligenceConfig(BaseModel):
    """Configuration for Social Intelligence Framework."""
    enabled: bool = True
    blackboard: BlackboardConfig = Field(default_factory=BlackboardConfig)
    pipeline_memory: PipelineMemoryConfig = Field(default_factory=PipelineMemoryConfig)
    primitives: PrimitivesConfig = Field(default_factory=PrimitivesConfig)
    meta_orchestrator: MetaOrchestratorConfig = Field(default_factory=MetaOrchestratorConfig)
    refinement: RefinementConfig = Field(default_factory=RefinementConfig)


class PipelineStepSchema(BaseModel):
    """Schema for a pipeline step."""
    primitive: str = Field(..., description="Primitive operation: execute, critique, validate, synthesize, branch, vote")
    params: Dict[str, Any] = Field(default_factory=dict)
    input_from: Optional[str] = Field(default=None, description="Reference to previous step output")
    output_key: str = Field(default="output", description="Key for storing this step's output")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PipelineRecordSchema(BaseModel):
    """Schema for a recorded pipeline."""
    id: str
    name: str
    description: str
    task_pattern: str
    task_examples: List[str] = Field(default_factory=list)
    steps: List[PipelineStepSchema]
    success_score: float = Field(ge=0.0, le=1.0)
    usage_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    created_at: str
    last_used: str
    parent_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BlackboardEntrySchema(BaseModel):
    """Schema for a blackboard entry."""
    id: str
    entry_type: str = Field(..., description="Type: hypothesis, fact, critique, vote, artifact, signal, decision, pipeline")
    author: str
    content: Any
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    timestamp: str
    parent_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PrimitiveResultSchema(BaseModel):
    """Schema for primitive operation result."""
    primitive: str
    success: bool
    output: Any
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    execution_time_ms: int = Field(default=0, ge=0)
    agent_name: Optional[str] = None


class CritiqueResultSchema(BaseModel):
    """Schema for critique operation result."""
    issues: List[str] = Field(default_factory=list)
    strengths: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    severity: str = Field(default="none", description="none, minor, major, critical")
    overall_score: float = Field(ge=0.0, le=1.0)
    raw_output: str = ""


class VoteResultSchema(BaseModel):
    """Schema for vote operation result."""
    vote: str = Field(..., description="approve, reject, abstain")
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""
    perspective: str
    conditions: List[str] = Field(default_factory=list)


class ValidationResultSchema(BaseModel):
    """Schema for validation operation result."""
    valid: bool
    issues: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    checks_performed: List[str] = Field(default_factory=list)
    raw_output: str = ""


class OrchestrateResultSchema(BaseModel):
    """Schema for orchestrate tool result."""
    task: str
    mode: str
    model_key: str
    executor_tools: List[str] = Field(default_factory=list)
    final: str
    pipeline_id: Optional[str] = None
    pipeline_used: Optional[str] = None
    blackboard_entries: List[str] = Field(default_factory=list)
    validation: Optional[ValidationResultSchema] = None
    committee: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# COMPACT SYSTEM CONFIGURATION
# =============================================================================

class CompactSessionMemoryConfig(BaseModel):
    """Configuration for session memory compaction."""
    enabled: bool = True
    min_tokens: int = Field(default=10000, ge=1000, description="Minimum tokens to preserve")
    max_tokens: int = Field(default=40000, ge=5000, description="Maximum tokens after compact")
    trigger_threshold: float = Field(default=0.75, ge=0.5, le=1.0, description="Trigger at % of context")


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


class CompactRestoreFilesConfig(BaseModel):
    """Configuration for file restoration after compact."""
    enabled: bool = True
    max_files: int = Field(default=5, ge=1)
    max_tokens_per_file: int = Field(default=5000, ge=500)
    token_budget: int = Field(default=50000, ge=10000)


class CompactRestoreSkillsConfig(BaseModel):
    """Configuration for skill restoration after compact."""
    enabled: bool = True
    token_budget: int = Field(default=25000, ge=5000)
    max_tokens_per_skill: int = Field(default=5000, ge=1000)


class CompactRestoreConfig(BaseModel):
    """Configuration for post-compact restoration."""
    files: CompactRestoreFilesConfig = Field(default_factory=CompactRestoreFilesConfig)
    skills: CompactRestoreSkillsConfig = Field(default_factory=CompactRestoreSkillsConfig)


class CompactConfig(BaseModel):
    """Complete configuration for the compact system.
    
    Compact manages context window limits through intelligent compaction:
    - Session Memory Compact: LLM-based conversation summarization
    - Microcompact: Tool result clearing for token efficiency
    - Auto Compact: Automatic triggering on threshold
    - Reactive Compact: Handle context_length_exceeded errors
    """
    enabled: bool = True
    session_memory: CompactSessionMemoryConfig = Field(default_factory=CompactSessionMemoryConfig)
    micro: CompactMicroConfig = Field(default_factory=CompactMicroConfig)
    auto: CompactAutoConfig = Field(default_factory=CompactAutoConfig)
    restore: CompactRestoreConfig = Field(default_factory=CompactRestoreConfig)
    
    # Model for summarization (optional, uses default if None)
    summary_model: Optional[str] = None
    summary_max_output_tokens: int = Field(default=20000, ge=1000)


GridConfig.model_rebuild()
