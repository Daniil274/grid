from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class PrepareAgentRequest(BaseModel):
    """Warm one agent. ``system_key`` defaults to the catalog's default system."""

    agent_key: str = Field(..., min_length=1)
    system_key: Optional[str] = None


class SettingsStructuredUpdateRequest(BaseModel):
    """``target`` names the file (web_chat.deployment.config_files); none is the default system's."""

    config: Dict[str, Any]
    target: Optional[str] = None


class SettingsYamlUpdateRequest(BaseModel):
    yaml_content: str
    target: Optional[str] = None


class ConversationRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class BranchRequest(BaseModel):
    """The user message a new branch replaces (its ``message_id``)."""

    message_id: str = Field(min_length=1, max_length=64)


class ConversationCreateRequest(BaseModel):
    """``None`` on either key means the router decides per message."""

    system_key: Optional[str] = None
    agent_key: Optional[str] = None


class ActionReviewRequest(BaseModel):
    decision: Literal["approve", "deny"]
