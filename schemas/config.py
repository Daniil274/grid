"""Optional persistent-memory runtime settings (host-owned identity)."""
from typing import Literal

from pydantic import BaseModel, Field


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
    snapshot_turns: bool = True       # ... and keep the exchange itself as an L0 record
    tenant_id: str = Field(default="default", min_length=1)
    team_id: str = "default"
    user_id: str = "default"
    agent_id: str = "grid"
