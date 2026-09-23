"""Operator-owned settings for the policy gate.

Everything the gate enforces is declared here by the operator: the rules it
judges against and the numeric budgets of a run. The gate itself knows nothing
about particular tools, paths or argument names.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionValidatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None = Field(
        default=None,
        min_length=1,
        description="Key from models; required when the policy is enabled.",
    )
    timeout_seconds: float = Field(default=5, gt=0, le=60)


class ActionPolicyRule(BaseModel):
    """One operator-authored rule interpreted by the policy classifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    decision: Literal["deny", "review"]
    when: str = Field(min_length=1)
    rationale: str = ""


class ActionPolicyQuestion(BaseModel):
    """The complete prompt for one Decisions question."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instructions: str = ""
    criteria: dict[Literal["allow", "deny", "review"], str] = Field(
        default_factory=dict
    )


class ActionPolicyPrompts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ActionPolicyQuestion = Field(default_factory=ActionPolicyQuestion)
    chain: ActionPolicyQuestion = Field(default_factory=ActionPolicyQuestion)


class ActionPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # shadow still counts attempts and stops a run; only the verdict is advisory.
    mode: Literal["off", "shadow", "enforce"] = "off"
    policy_file: str | None = Field(
        default=None,
        description="Optional policy YAML, resolved relative to the system config.",
    )
    version: str = "1"
    rules: tuple[ActionPolicyRule, ...] = ()
    prompts: ActionPolicyPrompts = Field(default_factory=ActionPolicyPrompts)
    # What the gate mediates. Anything not listed runs unchecked.
    kinds: tuple[Literal["function", "agent", "mcp"], ...] = (
        "function",
        "agent",
        "mcp",
    )
    # Judge the run so far (executed calls plus agent reasoning), not only this call.
    check_chain: bool = True
    # These fields are replaced by type/size/hash metadata in the policy view.
    sensitive_fields: tuple[str, ...] = (
        "authorization",
        "api_key",
        "cookie",
        "password",
        "secret",
        "token",
    )
    max_argument_value_bytes: int = Field(default=4096, ge=64, le=1048576)
    review_ttl_seconds: int = Field(default=300, ge=10, le=86400)
    max_pending_reviews: int = Field(default=100, ge=1, le=10000)
    max_delegation_depth: int | None = Field(default=8, ge=1, le=100)
    max_action_bytes: int = Field(default=65536, ge=1, le=1048576)
    max_chain_bytes: int = Field(default=32768, ge=0, le=1048576)
    max_chain_events: int = Field(default=20, ge=0, le=200)
    max_reasoning_bytes: int = Field(default=8192, ge=0, le=262144)
    max_task_context_messages: int = Field(default=8, ge=1, le=50)
    max_task_context_bytes: int = Field(default=16384, ge=256, le=262144)
    # Per agent run: the top-level agent and each sub-agent have their own.
    max_attempts_per_run: int = Field(default=100, ge=1)
    # Every call in the turn, sub-agents included.
    max_attempts_per_turn: int = Field(default=1000, ge=1)
    max_denials_per_run: int = Field(default=3, ge=1)
    validator: ActionValidatorConfig = Field(default_factory=ActionValidatorConfig)
