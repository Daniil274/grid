from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class PrepareAgentRequest(BaseModel):
    agent_key: str = Field(..., min_length=1)


class SettingsStructuredUpdateRequest(BaseModel):
    config: Dict[str, Any]


class SettingsYamlUpdateRequest(BaseModel):
    yaml_content: str


class ConversationCreateRequest(BaseModel):
    agent_key: Optional[str] = None

