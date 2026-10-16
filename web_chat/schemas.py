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
    """``None`` on either key means the router decides per message; on
    ``workspace``, the chat works in the space's own workspace."""

    system_key: Optional[str] = None
    agent_key: Optional[str] = None
    workspace: Optional[str] = Field(default=None, max_length=4096)
    project_id: Optional[str] = Field(default=None, max_length=64)


class ProjectCreateRequest(BaseModel):
    """A new project; ``parent_id`` nests it (at most three levels deep)."""

    title: str = Field(min_length=1, max_length=120)
    parent_id: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = Field(default=None, max_length=4000)


class ProjectUpdateRequest(BaseModel):
    """Only the keys given change; ``parent_id=None`` moves a project to the root."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    parent_id: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = Field(default=None, max_length=4000)
    notes: Optional[str] = Field(default=None, max_length=32768)
    archived: Optional[bool] = None


class ConversationProjectRequest(BaseModel):
    """Which project a chat belongs to; ``None`` detaches it."""

    project_id: Optional[str] = Field(default=None, max_length=64)


class ContinueRequest(BaseModel):
    """What a continued chat starts from: the source chat's plan, or its tail."""

    strategy: Literal["plan", "tail"] = "plan"


class WorkspaceRequest(BaseModel):
    """The directory a chat works in, as a full path on the server."""

    path: str = Field(min_length=1, max_length=4096)


class ActionReviewRequest(BaseModel):
    decision: Literal["approve", "deny"]
