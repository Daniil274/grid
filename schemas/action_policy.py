"""Operator-owned settings for the policy gate.

Everything the gate enforces is declared here by the operator: the filters a
user picks between, the paths it protects, the rules its policy model judges
against and the numeric budgets of a run. What a tool does is declared by the
tool (utils.tool_effects) or here, never guessed from its name.
"""

from typing import Literal, Union

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


#: What happens to a call: it runs, the policy model judges it, the user is
#: asked, or it is refused.
Route = Literal["allow", "judge", "review", "deny"]

#: The routes from the least to the most strict; of two, the stricter wins.
ROUTE_ORDER: tuple[str, ...] = ("allow", "judge", "review", "deny")

#: The kinds of effect a tool declares (utils.tool_effects).
EffectKind = Literal["read", "write", "exec", "egress", "external", "delegate"]


class ActionPolicyFilter(BaseModel):
    """One setting of the user's policy switch: a route per kind of effect.

    A call's route comes from what its tool declares it does: reading, writing
    the workspace, running commands, reaching out, acting outside. ``unknown``
    is the route of a tool that declares nothing. ``protected`` applies to
    secrets and to the files that define what agents may do (``secret_paths``,
    ``protected_paths``), unless the effect's own route is stricter. With
    ``follow_flows``, a run that took in outside content has its changes and
    commands judged, and a run that read a secret has what leaves the machine
    judged. Delegation always runs: the agent it starts is judged call by call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)
    description: str = ""
    read: Route = "allow"
    write: Route = "allow"
    exec: Route = "judge"
    egress: Route = "allow"
    external: Route = "judge"
    unknown: Route = "judge"
    protected: Route = "review"
    follow_flows: bool = True


def default_filters() -> dict[str, ActionPolicyFilter]:
    return {
        "read_only": ActionPolicyFilter(
            label="Read only",
            description=(
                "Agents look and change nothing: edits, commands outside the "
                "read-only list and outside actions are refused."
            ),
            write="deny",
            exec="deny",
            external="deny",
        ),
        "strict": ActionPolicyFilter(
            label="Strict",
            description=(
                "Every change, command and request out is judged by the policy "
                "model; outside actions and protected files wait for you."
            ),
            write="judge",
            egress="judge",
            external="review",
        ),
        "balanced": ActionPolicyFilter(
            label="Balanced",
            description=(
                "Reading, editing the workspace and delegating run freely. "
                "Commands, unknown tools and outside actions are judged; "
                "secrets and the policy's own files wait for you."
            ),
        ),
        "trusted": ActionPolicyFilter(
            label="Trusted",
            description=(
                "Everything runs, commands included. Only outside actions and "
                "protected files are judged."
            ),
            exec="allow",
            unknown="allow",
            protected="judge",
            follow_flows=False,
        ),
    }


#: A read-only command: a prefix such as ``git diff``, or a prefix with the
#: options that would make it write (``{"find": ["-delete", "-exec"]}``).
ReadonlyCommand = Union[str, dict[str, tuple[str, ...]]]

DEFAULT_READONLY_COMMANDS: tuple[ReadonlyCommand, ...] = (
    "cd", "pwd", "ls", "dir", "tree", "cat", "type", "head", "tail", "wc",
    "stat", "file", "du", "df", "date", "echo", "which", "where", "grep",
    "egrep", "fgrep", "rg", "findstr", "uniq", "cut", "diff", "basename",
    "dirname", "realpath",
    {"sort": ("-o", "--output")},
    {"find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls")},
    {"sed -n": ("-i", "--in-place")},
    "git status", "git rev-parse", "git ls-files", "git blame", "git branch --show-current",
    "git branch --list", "git remote -v", "git stash list", "git describe",
    {"git diff": ("--output", "--ext-diff")},
    {"git log": ("--output",)},
    {"git show": ("--output", "--ext-diff")},
)


class ActionPolicySystem(BaseModel):
    """What one system adds to the policy it runs under.

    The base policy - the routing catalog's, or the system's own when no catalog
    policy is enabled - holds what every system shares. What is true only of
    one system's tools and work (its MCP tools, its own config files, guidance
    for the policy model) lives here, so it never reaches the judgment of
    another system's calls. Operator-authored: private systems cannot set it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    rules: tuple[ActionPolicyRule, ...] = ()
    # Appended to the base question's instructions; criteria stay the base's.
    action: str = ""
    protected_paths: tuple[str, ...] = ()
    tool_effects: dict[str, EffectKind] = Field(default_factory=dict)


class ActionPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # shadow still counts attempts and stops a run; only the verdict is advisory.
    mode: Literal["off", "shadow", "enforce"] = "off"
    policy_file: str | None = Field(
        default=None,
        description="Optional policy YAML, resolved relative to the system config.",
    )
    version: str = "1"
    # The user's switch: each conversation runs under one of these filters.
    filters: dict[str, ActionPolicyFilter] = Field(default_factory=default_filters)
    default_filter: str = "balanced"
    # Who answers a call held for review in the chat it came from: the user who
    # runs it, or only the operator (the host's review API, admins).
    approvals: Literal["user", "operator"] = "user"
    # Workspace paths (globs; * also crosses directories) whose contents are
    # secret: reading or changing them takes the filter's `protected` route,
    # and a run that read one has what leaves the machine judged.
    secret_paths: tuple[str, ...] = (
        ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
        "id_ecdsa*", "id_ed25519*", "*credentials*", ".netrc", ".npmrc", ".pypirc",
    )
    # Workspace paths that define what agents may do: changing them takes the
    # filter's `protected` route.
    protected_paths: tuple[str, ...] = (".git/*", ".github/*", "policies/*", "*action-policy*.yaml")
    # Reading these brings outside content into the run, as an untrusted tool does.
    untrusted_paths: tuple[str, ...] = ("uploads/*",)
    readonly_commands: tuple[ReadonlyCommand, ...] = DEFAULT_READONLY_COMMANDS
    # Effects of tools that declare none in code - MCP tools above all - by
    # tool name (globs).
    tool_effects: dict[str, EffectKind] = Field(default_factory=dict)
    rules: tuple[ActionPolicyRule, ...] = ()
    prompts: ActionPolicyPrompts = Field(default_factory=ActionPolicyPrompts)
    # What the gate mediates. Anything not listed runs unchecked.
    kinds: tuple[Literal["function", "agent", "mcp"], ...] = (
        "function",
        "agent",
        "mcp",
    )
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
    # How long a held call stays open for an answer, and waits for one in the chat.
    review_ttl_seconds: int = Field(default=300, ge=10, le=86400)
    max_pending_reviews: int = Field(default=100, ge=1, le=10000)
    max_delegation_depth: int | None = Field(default=8, ge=1, le=100)
    max_action_bytes: int = Field(default=65536, ge=1, le=1048576)
    # The run's earlier calls the policy model sees beside a judged call.
    max_history_bytes: int = Field(default=32768, ge=0, le=1048576)
    max_history_events: int = Field(default=20, ge=0, le=200)
    max_reasoning_bytes: int = Field(default=8192, ge=0, le=262144)
    max_task_context_messages: int = Field(default=8, ge=1, le=50)
    max_task_context_bytes: int = Field(default=16384, ge=256, le=262144)
    # One assistant reply in the task context; the rest of it is clipped.
    max_reply_bytes: int = Field(default=4096, ge=0, le=65536)
    # Per agent run: the top-level agent and each sub-agent have their own.
    max_attempts_per_run: int = Field(default=100, ge=1)
    # Every call in the turn, sub-agents included.
    max_attempts_per_turn: int = Field(default=1000, ge=1)
    max_denials_per_run: int = Field(default=3, ge=1)
    validator: ActionValidatorConfig = Field(default_factory=ActionValidatorConfig)
    # This system's addition to whichever base policy governs it: inline, or
    # read from system_file, resolved relative to the system config.
    system: ActionPolicySystem | None = None
    system_file: str | None = None

    @model_validator(mode="after")
    def validate_filters(self):
        if not self.filters:
            raise ValueError("At least one filter is required")
        if self.default_filter not in self.filters:
            raise ValueError(
                f"default_filter {self.default_filter!r} is not one of the filters: "
                + ", ".join(self.filters)
            )
        return self

    def filter(self, name: str | None) -> tuple[str, ActionPolicyFilter]:
        """The filter called *name*, or the default for an unknown or missing name."""
        if name in self.filters:
            return name, self.filters[name]
        return self.default_filter, self.filters[self.default_filter]

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
        action = self.prompts.action
        if system.action.strip():
            action = action.model_copy(
                update={
                    "instructions": "\n\n".join(
                        part for part in (action.instructions.strip(), system.action.strip()) if part
                    )
                }
            )
        return self.model_copy(
            update={
                "version": f"{self.version}+{system.version}",
                "rules": (*self.rules, *system.rules),
                "prompts": ActionPolicyPrompts(action=action),
                "protected_paths": (*self.protected_paths, *system.protected_paths),
                "tool_effects": {**self.tool_effects, **system.tool_effects},
                "system": None,
            }
        )
