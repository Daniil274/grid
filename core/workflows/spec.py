"""Typed workflow contracts: specs, validation, pin, and shared result types.

C1 of docs/durable-workflows-plan.md, with the design fixes this task owes:

- ``egress`` is NOT automatically safe: it has effects outside the workspace
  that no checkpoint can verify, so it is never auto-retryable.
- A self-declared ``idempotent`` flag is a recorded claim, never replay
  authority: retry decisions are computed from the trusted effect
  classification of the step kind, not from what the model wrote.
- Gates are machine-checkable by construction: ``GateSpec`` survives parsing
  (``kind="gate"`` is a distinct literal, so a gate step never silently
  downgrades to a plain ``StepSpec``) and must name a registered check; the
  outcome is a boolean from the checker, and model text is kept separate as
  unverified claims.
- Result dataflow: ``StepResult.result_ref`` points at evidence (journal rows
  / events), never at trusted model text; downstream steps receive their
  dependencies' ``StepResult`` objects (``StepInputs``), not raw strings.
- Budgets are finite and non-negative; queue and work deadlines are separate.

Nothing here talks to the network or the database; parsing and validation are
pure so the LLM-facing tool can report structured ``SPEC_INVALID`` problems.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# -- kinds, statuses, codes -----------------------------------------------------

#: Kinds that name an executable step. "gate" is deliberately excluded so a
#: gate step can only parse as :class:`GateSpec` (never a plain StepSpec).
ExecutableKind = Literal["read", "egress", "write", "exec", "agent"]
GateKind = Literal["gate"]
#: All step kinds, as in the plan §3.
StepKind = Literal["read", "egress", "write", "exec", "agent", "gate"]

RunStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "interrupted",
    "paused_budget",
    "cancelling",
    "emergency_stopped",
]
StepStatus = Literal[
    "pending", "ready", "running", "completed", "failed", "interrupted", "skipped"
]

#: Effect of a *finished* attempt: none / unknown until verified / confirmed.
EffectStatus = Literal["none", "unknown", "confirmed"]

#: Trusted effect classification, computed from the kind by code (never from
#: model claims): "none" — no side effects; "workspace" — effects verifiable
#: against a workspace checkpoint; "external" — effects outside the workspace
#: that no checkpoint can verify; "unknown" — depends on the step's tools.
EffectClass = Literal["none", "workspace", "external", "unknown"]

FailureCode = Literal[
    "SPEC_INVALID",
    "POLICY_VIOLATION",
    "TIMEOUT",
    "TRANSIENT_ERROR",
    "PERMANENT_ERROR",
    "GATE_FAILED",
    "BUDGET_PAUSED",
    "INTERRUPTED",
    "UNKNOWN_EFFECT",
    "DEPENDENCY_FAILED",
    "INIT_REQUIRED_FAILED",
    "MALFORMED",
]

#: Kinds with no side effects by construction — the only kinds an engine may
#: auto-retry. NOTE: "egress" is NOT here (external effects, unverifiable),
#: and "write"/"exec" only retry after checkpoint verification (engine policy).
EFFECT_FREE_KINDS = frozenset({"read", "gate"})

#: Codes that are never retryable, whatever the step says.
NEVER_RETRYABLE = frozenset(
    {
        "SPEC_INVALID",
        "POLICY_VIOLATION",
        "PERMANENT_ERROR",
        "GATE_FAILED",
        "BUDGET_PAUSED",
        "INTERRUPTED",
        "UNKNOWN_EFFECT",
        "DEPENDENCY_FAILED",
        "INIT_REQUIRED_FAILED",
        "MALFORMED",
    }
)

MAX_STEPS = 256
MAX_PARALLEL = 16
MAX_RETRIES = 3
MAX_REVIEW_ROUNDS = 5

_STEP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:\-]{0,127}$")
#: A registered machine check: namespaced identifier, no prose, no whitespace.
_CHECK = re.compile(r"^[A-Za-z][A-Za-z0-9_.:\-]{0,127}$")

_KIND_EFFECT_CLASS: dict[str, EffectClass] = {
    "read": "none",
    "gate": "none",
    "write": "workspace",
    "exec": "workspace",
    "egress": "external",
    "agent": "unknown",  # may hold WRITE tools; unproven, so treated as effectful
}

#: Steps as stored in a spec: :data:`Step`, assigned after GateSpec below.
#: GateSpec first: pydantic's smart union keeps kind="gate" as a GateSpec and
#: every other kind as a StepSpec, so gates never downgrade at parsing.


def effect_class(step: "Step") -> EffectClass:
    """Trusted classification of a step's effects, computed from its kind.

    This is the authority for retry/replay decisions (invariant I8). The
    step's ``idempotent`` flag does NOT change it: that is the model's claim
    about itself, not a fact.
    """
    return _KIND_EFFECT_CLASS[step.kind]


def is_retryable(step: "Step", code: FailureCode) -> bool:
    """Machine decision whether a failed attempt of *step* may be retried.

    Computed from the code and the trusted effect class (I8) — never from
    model text. Only effect-free kinds (read, gate) are auto-retryable;
    write/exec retries go through the verify-on-resume path in the engine,
    and egress never auto-retries because its effects cannot be verified.
    """
    if code in NEVER_RETRYABLE:
        return False
    if code in ("TIMEOUT", "TRANSIENT_ERROR"):
        return step.kind in EFFECT_FREE_KINDS
    return False


# -- write paths ----------------------------------------------------------------


class WritePathError(ValueError):
    """A write path is not a normalizable workspace-relative path."""


def normalize_write_path(path: str) -> str:
    """Normalize one workspace-relative write path.

    Lexically only: absolute paths, "~", and any ".." segment (before or
    after collapsing) are rejected, so the normalized path can never escape
    the workspace. Symlink escape is a runtime property of the workspace and
    is checked by :func:`validate` when a ``workspace_root`` is given.
    """
    if not isinstance(path, str) or not path.strip():
        raise WritePathError("empty write path")
    raw = path.strip()
    if "\x00" in raw or any(ord(ch) < 32 for ch in raw):
        raise WritePathError(f"control characters in write path {path!r}")
    if len(raw) > 512:
        raise WritePathError(f"write path too long ({len(raw)} chars)")
    if raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", raw):
        raise WritePathError(f"write path must be workspace-relative, got {path!r}")
    parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]
    if ".." in parts:
        raise WritePathError(f"write path must not traverse upward, got {path!r}")
    if not parts:
        raise WritePathError(f"write path is empty after normalization, got {path!r}")
    return "/".join(parts)


def paths_escape_workspace(root: Path, paths: list[str]) -> list[str]:
    """Write paths that resolve outside *root* (symlink bypass included)."""
    root_real = Path(os.path.realpath(root))
    escaped = []
    for rel in paths:
        target = Path(os.path.realpath(root / rel))
        if target != root_real and root_real not in target.parents:
            escaped.append(rel)
    return escaped


def zones_overlap(a: str, b: str) -> bool:
    """Whether two normalized write zones cover a common path."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


# -- spec models ----------------------------------------------------------------


class InitToolCall(BaseModel):
    """A tool call made once before the workflow starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_TOOL.pattern)
    parameters: dict[str, Any] = Field(default_factory=dict)
    required: bool = True


class StepSpec(BaseModel):
    """One executable step. kind="gate" steps are :class:`GateSpec`."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_STEP_ID.pattern)
    kind: ExecutableKind
    tool: str | None = Field(default=None, pattern=_TOOL.pattern)
    task: str | None = None
    arguments: dict[str, Any] | None = None
    depends_on: list[str] = Field(default_factory=list)
    write_paths: list[str] = Field(default_factory=list)
    #: Work deadline for this step, counted from slot acquisition (P0-2).
    #: Only tightens the run deadline; it never extends it.
    timeout_seconds: int | None = Field(default=None, gt=0)
    #: A recorded claim by the spec author. It is NOT replay authority
    #: (see :func:`effect_class` and :func:`is_retryable`).
    idempotent: bool = False
    max_retries: int = Field(default=0, ge=0, le=MAX_RETRIES)
    #: False: a failure skips this step and its dependents, not the run.
    required: bool = True

    @model_validator(mode="after")
    def _normalize(self) -> "StepSpec":
        # Deduplicate dependencies, preserving order.
        self.depends_on = list(dict.fromkeys(self.depends_on))
        # Write paths are always stored normalized; lexical escapes are a
        # parse error (SPEC_INVALID at the tool boundary).
        self.write_paths = [normalize_write_path(p) for p in self.write_paths]
        return self

    @property
    def effects(self) -> EffectClass:
        return effect_class(self)


class GateSpec(StepSpec):
    """A machine-checkable acceptance step.

    ``check`` names a registered check (namespaced identifier, e.g.
    "tool:pytest" or "quality.lint") that the gate runner resolves and
    executes; the outcome must be a boolean produced by that check, plus an
    evidence pointer. Free-form model judgment is not a gate outcome: model
    text only ever lands in ``StepResult.unverified_claims``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: GateKind = "gate"
    check: str = Field(pattern=_CHECK.pattern)
    check_params: dict[str, Any] = Field(default_factory=dict)
    max_review_rounds: int = Field(default=2, ge=1, le=MAX_REVIEW_ROUNDS)


Step = Union[GateSpec, StepSpec]  # noqa: F811 — the public annotation


class WorkflowSpec(BaseModel):
    """A whole workflow: a validated DAG of steps plus run-wide limits."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME.pattern)
    goal: str = Field(min_length=1)
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)
    max_parallel: int = Field(default=4, ge=1, le=MAX_PARALLEL)
    #: Run lifetime deadline, from creation.
    deadline_seconds: int | None = Field(default=None, gt=0)
    #: Queue deadline, from enqueue — separate from work deadlines, which
    #: steps carry as ``timeout_seconds`` (from slot acquisition).
    queue_timeout_seconds: int | None = Field(default=None, gt=0)
    budget_usd: float | None = None
    init_tools: list[InitToolCall] = Field(default_factory=list)

    @field_validator("budget_usd")
    @classmethod
    def _finite_non_negative(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("budget_usd must be a finite number")
        if value < 0:
            raise ValueError("budget_usd must be non-negative")
        return value

    def budget_usd_micros(self) -> int | None:
        """The budget in micro-dollars, as the journal stores it."""
        if self.budget_usd is None:
            return None
        return round(self.budget_usd * 1_000_000)


# -- validation -----------------------------------------------------------------

SpecErrorCode = Literal[
    "CYCLE",
    "DUPLICATE_STEP_ID",
    "SELF_DEPENDENCY",
    "UNKNOWN_DEPENDENCY",
    "TOOL_REQUIRED",
    "TASK_REQUIRED",
    "WRITE_PATHS_EMPTY",
    "WRITE_ZONE_OVERLAP",
    "RETRY_UNSAFE",
    "SYMLINK_ESCAPE",
    "QUEUE_DEADLINE_GT_LIFETIME",
    "DUPLICATE_INIT_TOOL",
]


class SpecError(BaseModel):
    """One machine-checkable problem found by :func:`validate`."""

    model_config = ConfigDict(extra="forbid")

    code: SpecErrorCode
    message: str
    step_id: str | None = None
    path: str | None = None


def _reaches(edges: dict[str, list[str]], start: str, target: str) -> bool:
    seen = set()
    stack = list(edges.get(start, ()))
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return False


def validate(
    spec: WorkflowSpec, *, workspace_root: str | Path | None = None
) -> list[SpecError]:
    """Structural validation of a parsed spec. Empty list ⇒ I1 and I2 hold.

    ``workspace_root`` (optional): when given, write paths are also checked
    against the real filesystem, so a symlink leading outside the workspace
    is rejected (lexical normalization alone cannot catch that).
    """
    errors: list[SpecError] = []
    steps = spec.steps
    by_id: dict[str, Step] = {}
    for position, step in enumerate(steps):
        if step.id in by_id:
            errors.append(
                SpecError(
                    code="DUPLICATE_STEP_ID",
                    message=f"step id {step.id!r} is used twice",
                    step_id=step.id,
                    path=f"steps[{position}]",
                )
            )
        else:
            by_id[step.id] = step
    unique = list(dict.fromkeys(step.id for step in steps))

    for step in steps:
        for dep in step.depends_on:
            if dep == step.id:
                errors.append(
                    SpecError(
                        code="SELF_DEPENDENCY",
                        message=f"step {step.id!r} depends on itself",
                        step_id=step.id,
                    )
                )
            elif dep not in by_id:
                errors.append(
                    SpecError(
                        code="UNKNOWN_DEPENDENCY",
                        message=f"step {step.id!r} depends on unknown step {dep!r}",
                        step_id=step.id,
                    )
                )
        if isinstance(step, GateSpec):
            pass  # machine check is validated at parse time
        elif step.kind == "agent":
            if not step.task:
                errors.append(
                    SpecError(
                        code="TASK_REQUIRED",
                        message=f"agent step {step.id!r} needs a task",
                        step_id=step.id,
                    )
                )
        elif not step.tool:
            errors.append(
                SpecError(
                    code="TOOL_REQUIRED",
                    message=f"{step.kind} step {step.id!r} needs a tool",
                    step_id=step.id,
                )
            )
        if step.kind == "write" and not step.write_paths:
            errors.append(
                SpecError(
                    code="WRITE_PATHS_EMPTY",
                    message=f"write step {step.id!r} declares no write_paths",
                    step_id=step.id,
                )
            )
        if step.max_retries > 0 and step.kind not in EFFECT_FREE_KINDS:
            errors.append(
                SpecError(
                    code="RETRY_UNSAFE",
                    message=(
                        f"step {step.id!r} (kind {step.kind!r}) asks for auto-retry, "
                        "but only read/gate steps are effect-free; self-declared "
                        "idempotent is a claim, not replay authority"
                    ),
                    step_id=step.id,
                )
            )

    # I1: acyclicity (Kahn's algorithm over known ids).
    edges = {
        sid: [dep for dep in by_id[sid].depends_on if dep in by_id]
        for sid in unique
        if sid in by_id
    }
    remaining = dict(edges)
    resolved: set[str] = set()
    while remaining:
        ready = [n for n, deps in remaining.items() if all(d in resolved for d in deps)]
        if not ready:
            for node in sorted(remaining):
                errors.append(
                    SpecError(
                        code="CYCLE",
                        message=f"dependency cycle involving step {node!r}",
                        step_id=node,
                    )
                )
            break
        for node in ready:
            resolved.add(node)
            remaining.pop(node)

    # I2: overlapping write zones must be ordered by some dependency path.
    writers = [step for step in steps if step.write_paths]
    for i, first in enumerate(writers):
        for second in writers[i + 1 :]:
            if not any(
                zones_overlap(a, b)
                for a in first.write_paths
                for b in second.write_paths
            ):
                continue
            if not (_reaches(edges, first.id, second.id) or _reaches(edges, second.id, first.id)):
                errors.append(
                    SpecError(
                        code="WRITE_ZONE_OVERLAP",
                        message=(
                            f"steps {first.id!r} and {second.id!r} write overlapping "
                            "zones without a dependency path between them"
                        ),
                        step_id=second.id,
                        path=first.id,
                    )
                )

    if workspace_root is not None:
        root = Path(workspace_root)
        for step in writers:
            escaped = paths_escape_workspace(root, step.write_paths)
            if escaped:
                errors.append(
                    SpecError(
                        code="SYMLINK_ESCAPE",
                        message=(
                            f"step {step.id!r} write paths resolve outside the "
                            f"workspace: {', '.join(escaped)}"
                        ),
                        step_id=step.id,
                    )
                )

    if (
        spec.queue_timeout_seconds is not None
        and spec.deadline_seconds is not None
        and spec.queue_timeout_seconds > spec.deadline_seconds
    ):
        errors.append(
            SpecError(
                code="QUEUE_DEADLINE_GT_LIFETIME",
                message=(
                    f"queue_timeout_seconds ({spec.queue_timeout_seconds}) must not "
                    f"exceed deadline_seconds ({spec.deadline_seconds})"
                ),
            )
        )

    names = [call.name for call in spec.init_tools]
    for name in list(dict.fromkeys(names)):
        if names.count(name) > 1:
            errors.append(
                SpecError(
                    code="DUPLICATE_INIT_TOOL",
                    message=f"init tool {name!r} is called twice",
                )
            )

    return errors


# -- canonical pin --------------------------------------------------------------


def canonical_json(spec: WorkflowSpec) -> str:
    """Deterministic JSON for a spec: sorted keys, no whitespace."""
    return json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def pin(spec: WorkflowSpec) -> str:
    """sha256 of the canonical JSON: the spec's identity (P1-4)."""
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


# -- shared result contracts (engine C3, journal C2, api C5) ---------------------

#: Results of a step's dependencies, keyed by step id — the only dataflow
#: into a step. The invoker receives these, not raw model strings.
StepInputs = dict[str, "StepResult"]


#: A JSON-compatible value carried between steps (dataflow). It is structured
#: tool output / evidence — never model authorization: a gate and a dependent
#: step may read it, but it grants nothing (the spec's ``kind`` and the tool's
#: declared effect remain the authority, invariant I8). Containers are typed
#: as ``Any`` element/value so the alias stays non-recursive (Python 3.11).
JsonValue = Union[None, bool, int, float, str, list[Any], dict[str, Any]]


class StepFailure(BaseModel):
    """A structured step failure; ``retryable`` is computed by code (I8)."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    code: FailureCode
    retryable: bool
    detail: str
    #: For POLICY_VIOLATION: the paths the coordinator may still approve.
    allowed_write_paths: list[str] | None = None
    attempt: int = Field(default=0, ge=0)


class GateOutcome(BaseModel):
    """The machine-checkable result of one gate round."""

    model_config = ConfigDict(extra="forbid")

    check: str
    #: Produced by the registered check code — never by model judgment.
    passed: bool
    rounds: int = Field(default=1, ge=1)
    #: Evidence the check produced (a pointer, not a claim).
    evidence_ref: str | None = None
    detail: str = ""


class StepResult(BaseModel):
    """What one attempt of a step produced.

    Dataflow rule: ``result_ref`` is a pointer to evidence (journal row,
    event, artifact path). Model-generated text goes to
    ``unverified_claims`` and is never trusted as a result (§6).
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str
    ok: bool
    attempt: int = Field(default=1, ge=1)
    result_ref: str | None = None
    #: Structured data the step produced for its dependents and gates. It is
    #: persisted with the result and flows into :data:`StepInputs`; it is
    #: evidence, not authorization.
    data: JsonValue = None
    effect_status: EffectStatus = "none"
    failure: StepFailure | None = None
    gate: GateOutcome | None = None
    unverified_claims: list[str] = Field(default_factory=list)


StepInvoker = Callable[[Step, StepInputs], Awaitable[StepResult]]


class StepView(BaseModel):
    """A step's state as the journal reports it."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: StepStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    result_ref: str | None = None
    error_code: FailureCode | None = None
    effect_status: EffectStatus = "none"


class WorkflowRunView(BaseModel):
    """A run's state as the API and tools expose it (minimal, stable)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    spec_sha256: str
    status: RunStatus
    created_at: str | None = None
    updated_at: str | None = None
    deadline_at: str | None = None
    queue_deadline_at: str | None = None
    budget_usd_micros: int | None = None
    spent_usd_micros: int = Field(default=0, ge=0)
    steps: list[StepView] = Field(default_factory=list)
    #: Machine-readable final report (§9): per-step status/failure/result_ref,
    #: gate outcomes, spent_usd, wall_seconds, unknown_effects.
    final_report: dict[str, Any] | None = None
