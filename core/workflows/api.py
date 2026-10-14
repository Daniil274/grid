"""Workflow API and the engine's step invoker (C5 of the durable-workflows plan).

This module is the *only* place a caller meets a workflow: it owns run scope
(user / context / workspace), builds the shared :class:`WorkflowService`, and
provides :func:`build_invoker`, the single :data:`StepInvoker` adapter that
turns the engine's steps into real calls.

Safety rules enforced here:

* **No bypass of the tools' policy.** A function step is run through the tool
  the factory itself resolved (``factory._resolve_function_tools``), whose
  ``on_invoke_tool`` is already wrapped by the action gate
  (``core.factory.policy``). The adapter never calls a raw function.
* **Trusted metadata, not ``step.kind``.** The tool's *declared* effect
  (``utils.tool_effects`` / the operator's ``tool_effects``) is cross-checked
  against the step's trusted effect class. A read-like step (effect class
  ``none``) may not replay a tool that declares a workspace/external/unknown
  effect — otherwise an effectful tool would be recorded as effect-free (I9).
* **Lane write_paths.** A ``write`` step's arguments must fall inside the
  step's own ``write_paths`` (``core.parallel_lanes``), so a step cannot write
  outside the zone its spec declared.
* **Gates are deterministic.** A gate step runs a registered machine check over
  its dependencies' *data* and evidence — never a model's ``passed``.
* **Agent steps are honestly unsupported in v1.** The adapter returns a
  structured failure rather than silently delegating (an unmanaged,
  unaudited sub-agent), leaving a safe path for a later wrapped integration.
* **Ownership.** A run's ``user_id`` / ``context_id`` is checked on every read,
  resume and cancel; a caller cannot reach another user's or another
  conversation's run (``None`` never opens someone else's run).

The engine owns scheduling; this module owns scope, wiring and the adapter.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

from utils.tool_effects import (
    DELEGATE_KIND,
    EGRESS,
    EXEC,
    EXTERNAL,
    READ,
    WRITE,
    Effect,
)

from .engine import WorkflowEngine
from .failures import MalformedResult, PolicyViolation
from .journal import JournalError, NotFoundError, WorkflowJournal
from .reconcile import (
    ReconcileError,
    ReconcileReport,
    prepare_resume,
    reconcile,
)
from .spec import (
    GateOutcome,
    GateSpec,
    Step,
    StepInputs,
    StepResult,
    WorkflowRunView,
    WorkflowSpec,
    effect_class,
    pin,
)

__all__ = [
    "WorkflowScopeError",
    "WorkflowRuntime",
    "WorkflowService",
    "build_invoker",
    "get_service",
    "workflow_runtime",
    "default_checks",
    "tool_effect_class",
    "register_check",
]

_log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- scope ----------------------------------------------------------------------


class WorkflowScopeError(JournalError, PermissionError):
    """A caller tried to reach a run that is not theirs."""


def _assert_owned(run: Mapping[str, Any], *, context_id: Optional[str], user_id: Optional[str]) -> None:
    """Refuse access unless the caller presents the run's own identity.

    A run created with a ``user_id`` / ``context_id`` may only be reached by a
    caller carrying the same one. ``None`` matches only an unscoped run — it
    never opens another user's or another conversation's run.
    """
    owner_user = run.get("user_id")
    if owner_user is not None and owner_user != user_id:
        raise WorkflowScopeError(
            f"run {run.get('run_id')!r} belongs to another user")
    owner_context = run.get("context_id")
    if owner_context is not None and owner_context != context_id:
        raise WorkflowScopeError(
            f"run {run.get('run_id')!r} belongs to another context")


# -- per-call runtime binding ---------------------------------------------------


@dataclass(frozen=True)
class WorkflowRuntime:
    """What one workflow tool call may exercise, and on whose authority.

    Bound for the duration of a call (``workflow_runtime``) and read by the
    step invoker: the live factory, the caller's trusted ``action_state``, the
    calling agent's own tool allowlist and the isolation its space requires.
    It is a context-local value — never stored on the shared
    :class:`WorkflowService` — so one agent's authority cannot leak into
    another agent's later call.
    """

    factory: Any = None
    action_state: Any = None
    required_isolation: Optional[frozenset] = None
    allowed_tools: Optional[frozenset] = None
    context_id: Optional[str] = None
    user_id: Optional[str] = None
    container_id: Optional[str] = None
    run_control: Any = None
    stream_observer: Any = None
    agent_id: Optional[str] = None
    session: Any = None
    pipeline_id: Optional[str] = None
    system_access_active: bool = False
    action_depth: int = 0


_RUNTIME: contextvars.ContextVar[Optional[WorkflowRuntime]] = contextvars.ContextVar(
    "workflow_runtime", default=None)


@contextmanager
def workflow_runtime(runtime: WorkflowRuntime):
    """Bind *runtime* for the duration of one workflow tool call.

    The binding is context-local: it reaches the engine's tasks of this call
    and no other, and it is reset when the call returns.
    """
    token = _RUNTIME.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME.reset(token)


def _active_runtime() -> Optional[WorkflowRuntime]:
    return _RUNTIME.get()


# -- trusted tool metadata ------------------------------------------------------

#: A tool's declared effect kind -> the step effect class it may serve.
_TOOL_EFFECT_CLASS: dict[str, str] = {
    READ: "none",
    WRITE: "workspace",
    EXEC: "workspace",
    EGRESS: "external",
    EXTERNAL: "external",
    DELEGATE_KIND: "delegate",
}


def tool_effect_class(effect: Optional[Effect]) -> str:
    """Trusted effect class of a *declared* tool effect.

    ``None`` (a tool that declares nothing) is ``unknown``: undeclared is not
    trusted to be effect-free.
    """
    if effect is None:
        return "unknown"
    return _TOOL_EFFECT_CLASS.get(effect.kind, "unknown")


# -- gate registry ---------------------------------------------------------------

#: A deterministic gate check: reads the dependencies' data/evidence, returns a
#: machine outcome. It must not call a model to decide ``passed``.
CheckFn = Callable[[GateSpec, StepInputs], GateOutcome]

_CHECKS: dict[str, CheckFn] = {}


def register_check(name: str, check: CheckFn) -> None:
    """Register a deterministic gate check under a namespaced *name*."""
    if not name or " " in name:
        raise ValueError("a check name must be non-empty and namespaced")
    _CHECKS[name] = check


def _evidence_ref(step: GateSpec, inputs: StepInputs) -> str:
    """A deterministic reference over the gate's inputs (data + evidence)."""
    material = json.dumps(
        {
            sid: {"ok": r.ok, "ref": r.result_ref, "data": r.data}
            for sid, r in sorted(inputs.items())
        },
        sort_keys=True,
        default=str,
    )
    return f"wf-gate:{step.check}:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _check_deps_ok(step: GateSpec, inputs: StepInputs) -> GateOutcome:
    passed = bool(inputs) and all(r.ok for r in inputs.values())
    detail = "every dependency completed" if passed else "a dependency did not complete"
    return GateOutcome(check=step.check, passed=passed, evidence_ref=_evidence_ref(step, inputs), detail=detail)


def _check_data_present(step: GateSpec, inputs: StepInputs) -> GateOutcome:
    def present(value: Any) -> bool:
        return value not in (None, "", [], {})

    missing = sorted(sid for sid, r in inputs.items() if not present(r.data))
    passed = bool(inputs) and not missing
    detail = "all dependency data present" if passed else f"missing data: {', '.join(missing) or 'none'}"
    return GateOutcome(check=step.check, passed=passed, evidence_ref=_evidence_ref(step, inputs), detail=detail)


def default_checks() -> dict[str, CheckFn]:
    """The deterministic checks the invoker knows out of the box."""
    return {
        "deps.ok": _check_deps_ok,
        "deps.data_present": _check_data_present,
    }


def _gate_result(step: GateSpec, inputs: StepInputs, checks: Mapping[str, CheckFn]) -> StepResult:
    check = checks.get(step.check)
    if check is None:
        # A gate naming an unknown check must not silently pass: the engine
        # turns this failed gate into GATE_FAILED upstream.
        return StepResult(
            step_id=step.id,
            ok=True,
            effect_status="none",
            gate=GateOutcome(
                check=step.check,
                passed=False,
                evidence_ref=None,
                detail=f"no registered machine check named {step.check!r}",
            ),
        )
    outcome = check(step, inputs)
    if outcome.check != step.check:
        outcome = outcome.model_copy(update={"check": step.check})
    return StepResult(
        step_id=step.id,
        ok=True,
        result_ref=outcome.evidence_ref,
        data=None,
        effect_status="none",
        gate=outcome,
    )


# -- invoker ---------------------------------------------------------------------


def _default_effect_of(factory: Any) -> Callable[[str], Optional[Effect]]:
    def lookup(name: str) -> Optional[Effect]:
        try:
            from tools import tool_effect

            loader = getattr(getattr(factory, "config", None), "project_tools_loader", None)
            return tool_effect(name, loader)
        except Exception:  # noqa: BLE001 - unreadable metadata is "no declaration"
            return None

    return lookup


def _default_isolation_of(factory: Any) -> Callable[[str], Optional[str]]:
    def lookup(name: str) -> Optional[str]:
        try:
            from tools import tool_isolation

            loader = getattr(getattr(factory, "config", None), "project_tools_loader", None)
            return tool_isolation(name, loader)
        except Exception:  # noqa: BLE001
            return None

    return lookup


def _normalize_path(path: str) -> str:
    from core.parallel_lanes import normalize

    return normalize(path)


def _path_covered(scope: str, path: str) -> bool:
    from core.parallel_lanes import covers

    return covers(_normalize_path(scope), _normalize_path(path))


def _enforce_write_paths(step: Step, effect: Optional[Effect]) -> None:
    """A ``write`` step may only touch paths inside its own ``write_paths``."""
    zones = list(step.write_paths or [])
    args = step.arguments or {}
    names = effect.paths if effect is not None else ()
    for name in names:
        raw = args.get(name)
        if not isinstance(raw, str) or not raw.strip():
            continue
        if not any(_path_covered(zone, raw) for zone in zones):
            raise PolicyViolation(
                f"step {step.id!r}: {raw!r} is outside the step's write_paths {zones}",
                allowed_write_paths=zones,
            )


def _canonical_tool_name(name: str) -> str:
    """*name* as its declared tool key (config/model aliases collapsed)."""
    try:
        from tools.function_tools import TOOL_ALIASES
    except Exception:  # noqa: BLE001 - no registry means "use the name as given"
        return name
    return TOOL_ALIASES.get(name, name)


def _resolve_named_tool(factory: Any, tool_name: str, allowed_tools: Optional[frozenset]) -> Any:
    """The tool *tool_name* names, **only** when it is in the agent's allowlist.

    *allowed_tools* is the set of tool keys the live agent's own config grants
    (``None`` for a caller with no identifiable agent). When it is given, the
    name is canonicalized through the alias table first, so a step cannot reach
    an allowed tool's neighbour — or a delegate/nested-workflow tool — under an
    alias the workflow spec chose. The spec never supplies the allowlist.
    """
    if not tool_name:
        raise PolicyViolation("a workflow step names no tool")
    if allowed_tools is not None and _canonical_tool_name(tool_name) not in allowed_tools:
        raise PolicyViolation(
            f"tool {tool_name!r} is not in this agent's allowlist")
    resolver = getattr(factory, "_resolve_function_tools", None)
    if resolver is None:
        raise MalformedResult("the factory cannot resolve tools for a workflow step")
    try:
        found = resolver([tool_name])
    except Exception as exc:  # noqa: BLE001 - resolution failure is a policy problem
        raise PolicyViolation(f"tool {tool_name!r} could not be resolved: {exc}") from exc
    if not found:
        raise PolicyViolation(
            f"tool {tool_name!r} is not available to this agent (not in its allowlist)"
        )
    tool = found[0]
    if allowed_tools is not None:
        resolved = _canonical_tool_name(getattr(tool, "name", tool_name) or tool_name)
        if resolved not in allowed_tools:
            raise PolicyViolation(
                f"resolved tool {resolved!r} is not in this agent's allowlist")
    return tool


def _resolve_step_tool(factory: Any, step: Step, allowed_tools: Optional[frozenset] = None) -> Any:
    if not step.tool:
        raise PolicyViolation(f"step {step.id!r} names no tool")
    return _resolve_named_tool(factory, step.tool, allowed_tools)


def _tool_context(
    factory: Any,
    *,
    tool_name: str,
    tool_call_id: str,
    action_state: Any,
    inputs: StepInputs,
    live: Optional["WorkflowRuntime"] = None,
) -> Any:
    """A tool context as the SDK/gate expect, carrying the run's trusted state.

    The step's dependency results are exposed on the context
    (``workflow_inputs`` / ``workflow_data``), so a workflow-aware tool reads
    real persisted data from its predecessors — a generic tool ignores it. The
    live caller's action state, user, container and stop control travel with
    it, so the tool and the action gate judge the same trusted run, not a
    fabricated one.
    """
    from types import SimpleNamespace

    from agents.tool_context import ToolContext
    from agents.usage import Usage

    def field(name: str, default: Any = None) -> Any:
        return getattr(live, name, default) if live is not None else default

    context_id = getattr(action_state, "context_id", None) or field("context_id")
    raw = SimpleNamespace(
        factory=factory,
        action_state=action_state,
        context_id=context_id,
        user_id=field("user_id"),
        container_id=field("container_id"),
        run_control=field("run_control"),
        stream_observer=field("stream_observer"),
        agent_id=field("agent_id"),
        session=field("session"),
        pipeline_id=field("pipeline_id"),
        system_access_active=field("system_access_active", False),
        action_depth=field("action_depth", 0),
        workflow_inputs={sid: r.model_dump(mode="json") for sid, r in inputs.items()},
        workflow_data={sid: r.data for sid, r in inputs.items()},
    )
    return ToolContext(
        context=raw,
        usage=Usage(),
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_arguments="",
    )


def _step_context(
    factory: Any, step: Step, action_state: Any, inputs: StepInputs,
    live: Optional["WorkflowRuntime"] = None,
) -> Any:
    """The step tool's context: the live run's trusted state + dependency data."""
    return _tool_context(
        factory,
        tool_name=step.tool or step.id,
        tool_call_id=f"wf-{step.id}",
        action_state=action_state,
        inputs=inputs,
        live=live,
    )


def _to_json_value(output: Any) -> Any:
    """Normalize a tool output to a JSON-compatible value (never model text)."""
    if isinstance(output, str):
        try:
            parsed: Any = json.loads(output)
        except ValueError:
            parsed = output
    else:
        parsed = output
    try:
        return json.loads(json.dumps(parsed, default=str))
    except (TypeError, ValueError):
        return str(parsed)


def _result_from_output(step: Step, output: Any) -> StepResult:
    data = _to_json_value(output)
    material = json.dumps(data, sort_keys=True, default=str)
    ref = f"wf-step:{step.id}:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return StepResult(step_id=step.id, ok=True, result_ref=ref, data=data, effect_status="none")


def build_invoker(
    factory: Any,
    *,
    effect_of: Optional[Callable[[str], Optional[Effect]]] = None,
    isolation_of: Optional[Callable[[str], Optional[str]]] = None,
    checks: Optional[Mapping[str, CheckFn]] = None,
    required_isolation: Optional[frozenset[str]] = None,
    action_state: Any = None,
    allowed_tools: Optional[frozenset] = None,
) -> Callable[[Step, StepInputs], Awaitable[StepResult]]:
    """Build the single :data:`StepInvoker` for a factory's workflows.

    *effect_of* / *isolation_of* read the tool's trusted declaration
    (defaults to ``tools.tool_effect`` / ``tools.tool_isolation``). *checks*
    are the gate checks (defaults to :func:`default_checks`). *required_isolation*
    — when given — refuses tools whose declared isolation is not among it
    (the confined-workspace case). *action_state* is the trusted run state the
    gate judges against. *allowed_tools* — when given — is the calling agent's
    own tool keys: a step naming anything else is refused. All three may also
    arrive at call time through :func:`workflow_runtime`, which is how the
    shared service carries per-call authority without storing it.
    """
    registry: dict[str, CheckFn] = dict(default_checks())
    if checks:
        registry.update(checks)

    async def invoke(step: Step, inputs: StepInputs) -> StepResult:
        if isinstance(step, GateSpec):
            return _gate_result(step, inputs, registry)
        if step.kind == "agent":
            # Honest, structured refusal: a v1 workflow does not silently start
            # an unmanaged sub-agent. A wrapped orchestrate integration is future work.
            raise MalformedResult(
                f"agent step {step.id!r} is not supported by the v1 workflow invoker"
            )

        live = _active_runtime()
        active_factory = live.factory if live is not None and live.factory is not None else factory
        state = live.action_state if live is not None else action_state
        isolation_gate = (
            live.required_isolation
            if live is not None and live.required_isolation is not None
            else required_isolation
        )
        allow = (
            live.allowed_tools
            if live is not None and live.allowed_tools is not None
            else allowed_tools
        )
        lookup_effect = effect_of or _default_effect_of(active_factory)
        lookup_isolation = isolation_of or _default_isolation_of(active_factory)

        effect = lookup_effect(step.tool or "")
        required = effect_class(step)
        declared = tool_effect_class(effect)
        if required == "none" and declared != "none":
            raise PolicyViolation(
                f"step {step.id!r} is effect-free ({required}) but tool {step.tool!r} "
                f"declares {effect.kind if effect else 'no effect'}; refusing to replay an "
                "effectful tool as effect-free"
            )
        if isolation_gate is not None:
            isolation = lookup_isolation(step.tool or "")
            if isolation not in isolation_gate:
                raise PolicyViolation(
                    f"tool {step.tool!r} declares isolation {isolation!r}; this workspace "
                    f"requires one of {sorted(isolation_gate)}"
                )
        if step.kind == "write":
            _enforce_write_paths(step, effect)

        tool = _resolve_step_tool(active_factory, step, allow)
        ctx = _step_context(active_factory, step, state, inputs, live=live)
        raw = json.dumps(step.arguments or {})
        output = await tool.on_invoke_tool(ctx, raw)
        return _result_from_output(step, output)

    return invoke


async def _runtime_init_invoke(call: Any) -> bool:
    """Run one ``init_tool`` through the live caller's mediated path (R4).

    The init tool is resolved **from the calling agent's own allowlist** (the
    same one the steps use) and invoked through its policy-wrapped
    ``on_invoke_tool`` under the live ``action_state``, so it is judged by the
    action gate like any other call. No binding (a direct service call with no
    caller) means no authority: the init tool is refused.
    """
    live = _active_runtime()
    if live is None or live.factory is None:
        return False
    tool = _resolve_named_tool(live.factory, call.name, live.allowed_tools)
    ctx = _tool_context(
        live.factory,
        tool_name=call.name,
        tool_call_id=f"wf-init:{call.name}",
        action_state=live.action_state,
        inputs={},
        live=live,
    )
    raw = json.dumps(dict(getattr(call, "parameters", None) or {}))
    await tool.on_invoke_tool(ctx, raw)
    return True


# -- service ---------------------------------------------------------------------


class WorkflowService:
    """A shared workflow front-end for one workspace/owner (engine + journal).

    One instance drives every run of a workspace: it scopes access to a run's
    user/context, resumes safely through ``reconcile``, and exposes the
    engine's ``run``/``cancel`` synchronously. Live runs are never touched by
    startup reconciliation (only stale ones — ``reconcile`` skips a live lease).
    """

    def __init__(
        self,
        *,
        journal: WorkflowJournal,
        invoke: Callable[[Step, StepInputs], Awaitable[StepResult]],
        owner: str = "workflow-api",
        workspace_root: Optional[str | Path] = None,
        max_global_parallel: int = 16,
        clock: Callable[[], datetime] = _utcnow,
        lease_seconds: float = 300.0,
        reconcile_on_start: bool = True,
        init_invoke: Optional[Callable[[Any], Awaitable[bool]]] = None,
    ) -> None:
        self._journal = journal
        self._owner = owner
        self._workspace_root = str(workspace_root) if workspace_root is not None else ""
        self._lease_seconds = lease_seconds
        self._engine = WorkflowEngine(
            journal=journal,
            invoke=invoke,
            owner=owner,
            max_global_parallel=max_global_parallel,
            clock=clock,
            lease_seconds=lease_seconds,
            init_invoke=init_invoke or _runtime_init_invoke,
        )
        self._reconcile_on_start = reconcile_on_start
        self._started = False

    @property
    def engine(self) -> WorkflowEngine:
        return self._engine

    @property
    def journal(self) -> WorkflowJournal:
        return self._journal

    @property
    def owner(self) -> str:
        return self._owner

    # -- reconciliation ---------------------------------------------------------

    def reconcile(
        self,
        *,
        now: Optional[datetime] = None,
        context_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> ReconcileReport:
        """Finalise stale runs (never live ones); scoped by context/user if given."""
        return reconcile(self._journal, now=now, context_id=context_id, user_id=user_id)

    def _ensure_started(self) -> None:
        if not self._reconcile_on_start or self._started:
            return
        self._started = True
        try:
            reconcile(self._journal)
        except JournalError:  # pragma: no cover - a broken journal must not hide runs
            _log.warning("workflow startup reconciliation failed", exc_info=True)

    # -- reads ------------------------------------------------------------------

    def _load_owned(self, run_id: str, *, context_id: Optional[str], user_id: Optional[str]) -> dict:
        run = self._journal.get_run(run_id)
        if run is None:
            raise NotFoundError(f"run not found: {run_id}")
        _assert_owned(run, context_id=context_id, user_id=user_id)
        return run

    def get(self, run_id: str, *, context_id: Optional[str] = None,
            user_id: Optional[str] = None) -> WorkflowRunView:
        self._load_owned(run_id, context_id=context_id, user_id=user_id)
        return self._engine._view(run_id)

    def list(self, *, context_id: Optional[str] = None, user_id: Optional[str] = None,
             statuses: Optional[list[str]] = None, limit: int = 50, offset: int = 0) -> list[WorkflowRunView]:
        self._ensure_started()
        page = self._journal.load_runs(
            statuses=statuses, context_id=context_id, user_id=user_id, limit=limit, offset=offset
        )
        return [self._engine._view(row["run_id"]) for row in page["items"]]

    # -- lifecycle --------------------------------------------------------------

    async def start(
        self,
        spec: WorkflowSpec | dict,
        *,
        context_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
        budget_usd_micros: Optional[int] = None,
        deadline_at: Optional[str] = None,
    ) -> WorkflowRunView:
        """Create a scoped run and execute it to a terminal/paused state."""
        self._ensure_started()
        parsed = spec if isinstance(spec, WorkflowSpec) else WorkflowSpec.model_validate(spec)
        run_id = run_id or self._journal.new_run_id()
        now = self._engine._clock()
        queue_deadline_at = None
        if parsed.queue_timeout_seconds is not None:
            queue_deadline_at = (now + timedelta(seconds=parsed.queue_timeout_seconds)).isoformat()
        if deadline_at is None and parsed.deadline_seconds is not None:
            deadline_at = (now + timedelta(seconds=parsed.deadline_seconds)).isoformat()
        self._journal.create_run(
            run_id=run_id,
            spec=parsed.model_dump(mode="json"),
            spec_sha256=pin(parsed),
            context_id=context_id,
            user_id=user_id,
            deadline_at=deadline_at,
            queue_deadline_at=queue_deadline_at,
            budget_usd_micros=parsed.budget_usd_micros(),
        )
        return await self._engine.run(
            run_id, parsed, deadline_at=deadline_at, budget_usd_micros=budget_usd_micros
        )

    async def cancel(
        self, run_id: str, *, context_id: Optional[str] = None,
        user_id: Optional[str] = None, reason: str = "cancelled",
    ) -> WorkflowRunView:
        self._load_owned(run_id, context_id=context_id, user_id=user_id)
        return await self._engine.cancel(run_id, reason)

    async def resume(
        self,
        run_id: str,
        *,
        context_id: Optional[str] = None,
        user_id: Optional[str] = None,
        write_policy: str = "never",
        verifier: Optional[Callable[[WorkflowJournal, str, str], bool]] = None,
        budget_usd_micros: Optional[int] = None,
    ) -> WorkflowRunView:
        """Resume an interrupted/paused/queued run under the caller's scope.

        An interrupted run is prepared through ``reconcile.prepare_resume``
        with **this service's owner**: ``prepare_resume`` claims the run
        (``interrupted -> running``, lease) and only then applies the safe /
        refused step decisions. A concurrent resumer loses the claim and
        mutates nothing, and the engine's later re-claim under the same owner
        is a no-op. A ``paused_budget`` or ``queued`` run is simply executed.
        """
        run = self._load_owned(run_id, context_id=context_id, user_id=user_id)
        status = run["status"]
        if status == "interrupted":
            prepare_resume(
                self._journal,
                run_id,
                write_policy=write_policy,  # type: ignore[arg-type]
                verifier=verifier,
                owner=self._owner,  # claim before any reset; engine re-claims as a no-op
                lease_seconds=self._lease_seconds,
                context_id=context_id,
                user_id=user_id,
            )
        elif status not in ("paused_budget", "queued"):
            raise ReconcileError(
                f"run {run_id} is {status!r} and cannot be resumed")
        parsed = WorkflowSpec.model_validate(json.loads(run["spec_json"]))
        return await self._engine.run(run_id, parsed, budget_usd_micros=budget_usd_micros)


# -- shared registry -------------------------------------------------------------

_SERVICES: dict[tuple[str, str], WorkflowService] = {}
_SERVICES_LOCK = threading.Lock()


def get_service(
    db_path: str | Path,
    *,
    owner: str,
    invoke_factory: Callable[[], Callable[[Step, StepInputs], Awaitable[StepResult]]],
    workspace_root: Optional[str | Path] = None,
    max_global_parallel: int = 16,
) -> WorkflowService:
    """The shared :class:`WorkflowService` for *(db_path, owner)*, built once.

    *invoke_factory* builds the step invoker lazily (it needs the factory that
    runs the call). The journal is opened once per key and reused, so a
    process serves one durable store per workspace/owner.
    """
    key = (str(db_path), owner)
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
        if service is None:
            journal = WorkflowJournal(db_path)
            service = WorkflowService(
                journal=journal,
                invoke=invoke_factory(),
                owner=owner,
                workspace_root=workspace_root,
                max_global_parallel=max_global_parallel,
            )
            _SERVICES[key] = service
        return service
