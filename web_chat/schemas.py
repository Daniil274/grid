from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class PrepareAgentRequest(BaseModel):
    """Warm one agent. ``system_key`` defaults to the catalog's default system."""

    agent_key: str = Field(..., min_length=1)
    system_key: Optional[str] = None


class SettingsStructuredUpdateRequest(BaseModel):
    config: Dict[str, Any]


class SettingsYamlUpdateRequest(BaseModel):
    yaml_content: str


class ConversationRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ConversationCreateRequest(BaseModel):
    """``None`` on either key means the router decides per message."""

    system_key: Optional[str] = None
    agent_key: Optional[str] = None


class ActionReviewRequest(BaseModel):
    decision: Literal["approve", "deny"]
