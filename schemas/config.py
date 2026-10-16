"""Optional persistent-memory runtime settings (host-owned identity)."""
from typing import Literal

from pydantic import BaseModel, Field


class MemoryEmbeddingsConfig(BaseModel):
    """Semantic retrieval: which memories reach the prompt is decided by embeddings.

    The defaults were calibrated on real RU/EN memories: with Qwen3-Embedding-8B
    and its query instruction the wanted record ranked first for every request,
    while requests about something else stayed below 0.45.
    """
    provider: str | None = None       # key of providers:; None turns semantic retrieval off
    model: str = "qwen/qwen3-embedding-8b"
    query_instruction: str | None = None  # None: the one the default model was calibrated with
    min_similarity: float = Field(default=0.45, ge=-1.0, le=1.0)
    max_records: int = Field(default=6, ge=0, le=100)   # topical records in the prompt
    batch_size: int = Field(default=32, ge=1, le=512)


class MemoryConfig(BaseModel):
    enabled: bool = False
    backend: Literal["sqlite", "tencent", "hybrid"] = "sqlite"
    storage_path: str = ".grid/memory.sqlite"
    tencent_url: str = "http://127.0.0.1:8000"
    service_id: str = ""
    api_key_env: str | None = None
    auto_inject: bool = True
    auto_inject_tokens: int = Field(default=1000, ge=0, le=100000)
    extract_on_compact: bool = True
    extract_on_turn: bool = True      # distil each finished turn into memory
    snapshot_turns: bool = False      # ... and keep the exchange itself as an L0 record (the chat already does)
    extract_with_model: bool = True   # a model writes the turn's memories (heuristic only without a model)
    extract_model: str | None = None  # model key for it; default: compact.summary_model, else the agent's
    tenant_id: str = Field(default="default", min_length=1)
    team_id: str = "default"
    user_id: str = "default"
    agent_id: str = "grid"
    embeddings: MemoryEmbeddingsConfig = Field(default_factory=MemoryEmbeddingsConfig)
