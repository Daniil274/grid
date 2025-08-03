"""
OpenAI-compatible API models for GRID Agent System.
Provides Pydantic models that match OpenAI API format.
"""

from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any, Union
from enum import Enum

class MessageRole(str, Enum):
    """Chat message roles."""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"

class ChatMessage(BaseModel):
    """Individual chat message."""
    role: MessageRole = Field(..., description="Message role")
    content: str = Field(..., description="Message content")
    name: Optional[str] = Field(None, description="Name of the message author")

    @validator('content')
    def content_not_empty(cls, v):
        if not v.strip():
            raise ValueError('Content cannot be empty')
        return v

class GridContext(BaseModel):
    """GRID-specific context extensions."""
    working_directory: Optional[str] = Field(None, description="Working directory for agent")
    session_id: Optional[str] = Field(None, description="Session identifier")
    tools_enabled: Optional[bool] = Field(True, description="Enable agent tools")
    security_level: Optional[str] = Field("standard", description="Security analysis level")
    timeout: Optional[int] = Field(300, description="Execution timeout in seconds")
    context_data: Optional[Dict[str, Any]] = Field(None, description="Additional context data")

class ChatCompletionRequest(BaseModel):
    """Chat completion request matching OpenAI format."""
    model: str = Field(..., description="Model/Agent to use")
    messages: List[ChatMessage] = Field(..., description="Chat messages")
    max_tokens: Optional[int] = Field(None, description="Maximum tokens to generate", ge=1, le=4000)
    temperature: Optional[float] = Field(0.7, description="Sampling temperature", ge=0.0, le=2.0)
    top_p: Optional[float] = Field(1.0, description="Nucleus sampling parameter", ge=0.0, le=1.0)
    n: Optional[int] = Field(1, description="Number of completions", ge=1, le=1)  # GRID supports only 1
    stream: Optional[bool] = Field(False, description="Stream partial results")
    stop: Optional[Union[str, List[str]]] = Field(None, description="Stop sequences")
    presence_penalty: Optional[float] = Field(0.0, description="Presence penalty", ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(0.0, description="Frequency penalty", ge=-2.0, le=2.0)
    logit_bias: Optional[Dict[str, float]] = Field(None, description="Logit bias")
    user: Optional[str] = Field(None, description="User identifier")
    
    # GRID-specific extensions
    grid_context: Optional[GridContext] = Field(None, description="GRID-specific context")

    @validator('messages')
    def messages_not_empty(cls, v):
        if not v:
            raise ValueError('Messages list cannot be empty')
        return v

    @validator('model')
    def model_not_empty(cls, v):
        if not v.strip():
            raise ValueError('Model name cannot be empty')
        return v

class ChatCompletionChoice(BaseModel):
    """Individual completion choice."""
    index: int = Field(..., description="Choice index")
    message: ChatMessage = Field(..., description="Completion message")
    finish_reason: Optional[str] = Field(None, description="Reason for completion finish")

class ChatCompletionUsage(BaseModel):
    """Token usage information."""
    prompt_tokens: int = Field(..., description="Tokens in prompt")
    completion_tokens: int = Field(..., description="Tokens in completion")
    total_tokens: int = Field(..., description="Total tokens used")

class GridMetadata(BaseModel):
    """GRID-specific response metadata."""
    agent_used: str = Field(..., description="Agent that processed the request")
    execution_time: float = Field(..., description="Execution time in seconds")
    tools_called: List[str] = Field(default_factory=list, description="Tools used by agent")
    security_analysis: Optional[Dict[str, Any]] = Field(None, description="Security analysis results")
    session_id: Optional[str] = Field(None, description="Session identifier")
    trace_id: Optional[str] = Field(None, description="Trace identifier")
    working_directory: Optional[str] = Field(None, description="Working directory used")

class ChatCompletionResponse(BaseModel):
    """Chat completion response matching OpenAI format."""
    id: str = Field(..., description="Completion ID")
    object: str = Field("chat.completion", description="Object type")
    created: int = Field(..., description="Creation timestamp")
    model: str = Field(..., description="Model used")
    choices: List[ChatCompletionChoice] = Field(..., description="Completion choices")
    usage: ChatCompletionUsage = Field(..., description="Token usage")
    
    # GRID-specific extensions
    grid_metadata: Optional[GridMetadata] = Field(None, description="GRID execution metadata")

class ChatCompletionChunkChoice(BaseModel):
    """Streaming completion choice."""
    index: int = Field(..., description="Choice index")
    delta: Dict[str, Any] = Field(..., description="Delta content")
    finish_reason: Optional[str] = Field(None, description="Finish reason")

class ChatCompletionChunk(BaseModel):
    """Streaming completion chunk."""
    id: str = Field(..., description="Completion ID")
    object: str = Field("chat.completion.chunk", description="Object type")
    created: int = Field(..., description="Creation timestamp")
    model: str = Field(..., description="Model used")
    choices: List[ChatCompletionChunkChoice] = Field(..., description="Chunk choices")

# Legacy Completions API Models
class CompletionRequest(BaseModel):
    """Legacy completion request."""
    model: str = Field(..., description="Model to use")
    prompt: str = Field(..., description="Prompt text")
    max_tokens: Optional[int] = Field(None, description="Maximum tokens", ge=1, le=4000)
    temperature: Optional[float] = Field(0.7, description="Temperature", ge=0.0, le=2.0)
    top_p: Optional[float] = Field(1.0, description="Top-p", ge=0.0, le=1.0)
    n: Optional[int] = Field(1, description="Number of completions", ge=1, le=1)
    stream: Optional[bool] = Field(False, description="Stream results")
    logprobs: Optional[int] = Field(None, description="Log probabilities")
    echo: Optional[bool] = Field(False, description="Echo prompt")
    stop: Optional[Union[str, List[str]]] = Field(None, description="Stop sequences")
    presence_penalty: Optional[float] = Field(0.0, description="Presence penalty")
    frequency_penalty: Optional[float] = Field(0.0, description="Frequency penalty")
    best_of: Optional[int] = Field(1, description="Best of", ge=1, le=1)
    logit_bias: Optional[Dict[str, float]] = Field(None, description="Logit bias")
    user: Optional[str] = Field(None, description="User ID")

class CompletionChoice(BaseModel):
    """Legacy completion choice."""
    text: str = Field(..., description="Generated text")
    index: int = Field(..., description="Choice index")
    logprobs: Optional[Dict[str, Any]] = Field(None, description="Log probabilities")
    finish_reason: Optional[str] = Field(None, description="Finish reason")

class CompletionResponse(BaseModel):
    """Legacy completion response."""
    id: str = Field(..., description="Completion ID")
    object: str = Field("text_completion", description="Object type")
    created: int = Field(..., description="Creation timestamp")
    model: str = Field(..., description="Model used")
    choices: List[CompletionChoice] = Field(..., description="Completion choices")
    usage: ChatCompletionUsage = Field(..., description="Token usage")

# Models API
class ModelPermission(BaseModel):
    """Model permission."""
    id: str
    object: str = "model_permission"
    created: int
    allow_create_engine: bool = False
    allow_sampling: bool = True
    allow_logprobs: bool = False
    allow_search_indices: bool = False
    allow_view: bool = True
    allow_fine_tuning: bool = False
    organization: str = "*"
    group: Optional[str] = None
    is_blocking: bool = False

class ModelInfo(BaseModel):
    """Model information."""
    id: str = Field(..., description="Model identifier")
    object: str = Field("model", description="Object type")
    created: int = Field(..., description="Creation timestamp")
    owned_by: str = Field(..., description="Owner organization")
    permission: List[ModelPermission] = Field(default_factory=list, description="Permissions")
    root: Optional[str] = Field(None, description="Root model")
    parent: Optional[str] = Field(None, description="Parent model")
    description: Optional[str] = Field(None, description="Model description")
    
    # GRID-specific extensions
    agent_type: Optional[str] = Field(None, description="Corresponding GRID agent type")
    capabilities: Optional[List[str]] = Field(None, description="Agent capabilities")
    tools: Optional[List[str]] = Field(None, description="Available tools")

class ModelList(BaseModel):
    """List of available models."""
    object: str = Field("list", description="Object type")
    data: List[ModelInfo] = Field(..., description="List of models")

# Error Models
class ErrorDetail(BaseModel):
    """Error detail."""
    message: str = Field(..., description="Error message")
    type: str = Field(..., description="Error type")
    param: Optional[str] = Field(None, description="Parameter that caused error")
    code: Optional[str] = Field(None, description="Error code")

class ErrorResponse(BaseModel):
    """Error response."""
    error: ErrorDetail = Field(..., description="Error details")