"""Operator-owned settings for the policy gate.

Everything the gate enforces is declared here by the operator: the rules it
judges against and the numeric budgets of a run. The gate itself knows nothing
about particular tools, paths or argument names.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionValidatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | tuple[str, ...] | None = Field(
        default=None,
        description=(
            "Key from models, or keys in order of preference: the first is "
            "always tried first, the next one only when those before it are "
            "unavailable. Required when the policy is enabled."
        ),
    )
    timeout_seconds: float = Field(default=5, gt=0, le=60)
    fallback_models: tuple[str, ...] = Field(default=(), max_length=4)
    # Defaults to one attempt per route, at least two.
    max_attempts: int = Field(default=2, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=0.25, ge=0, le=5)
    max_concurrency: int = Field(default=4, ge=1, le=100)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=100)
    circuit_cooldown_seconds: float = Field(default=15, gt=0, le=300)

    @property
    def routes(self) -> tuple[str, ...]:
        """Model keys in the order they are tried: model, then fallback_models."""
        primary = (self.model,) if isinstance(self.model, str) else self.model or ()
        return (*primary, *self.fallback_models)

    @model_validator(mode="after")
    def validate_routes(self):
        if isinstance(self.model, tuple) and not self.model:
            raise ValueError("Validator model list must not be empty")
        keys = self.routes
        if any(not key.strip() for key in keys):
            raise ValueError("Validator model keys must not be blank")
        if len(set(keys)) != len(keys):
            raise ValueError("Validator model keys must be unique")
        if len(keys) > 5:
            raise ValueError("At most 5 validator models")
        if "max_attempts" not in self.model_fields_set:
            object.__setattr__(self, "max_attempts", max(2, len(keys)))
        if self.max_attempts < len(keys):
            raise ValueError("max_attempts must cover every validator model")
        return self


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


class ActionPolicySystem(BaseModel):
    """What one system adds to the policy it runs under.

    The base policy - the routing catalog's, or the system's own when no catalog
    policy is enabled - holds the rules every system shares. Guidance that is
    true only of one system's tools and work (its stores, its tracker, its
    version control) lives here, so it never reaches the validator judging
    another system's calls. Operator-authored: private systems cannot set it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    rules: tuple[ActionPolicyRule, ...] = ()
    # Appended to the base question's instructions; criteria stay the base's.
    action: str = ""
    chain: str = ""


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
    # This system's addition to whichever base policy governs it. A string in
    # the YAML is a file resolved relative to the system config.
    system: ActionPolicySystem | None = None

    def with_system(self, system: "ActionPolicySystem | None") -> "ActionPolicyConfig":
        """This policy as the base, with one system's rules and guidance added.

        The base's own ``system`` is never carried over: a catalog policy
        governs many systems and adds nothing system-specific to any of them.
        """
        if system is None:
            return self.model_copy(update={"system": None})
        ids = [rule.id for rule in (*self.rules, *system.rules)]
        if len(set(ids)) != len(ids):
            raise ValueError("System policy rules must not reuse the ids of base rules")

        def extended(question: ActionPolicyQuestion, extra: str) -> ActionPolicyQuestion:
            if not extra.strip():
                return question
            instructions = "\n\n".join(
                part for part in (question.instructions.strip(), extra.strip()) if part
            )
            return question.model_copy(update={"instructions": instructions})

        return self.model_copy(
            update={
                "version": f"{self.version}+{system.version}",
                "rules": (*self.rules, *system.rules),
                "prompts": ActionPolicyPrompts(
                    action=extended(self.prompts.action, system.action),
                    chain=extended(self.prompts.chain, system.chain),
                ),
                "system": None,
            }
        )
