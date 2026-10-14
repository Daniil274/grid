"""Workflow tools: run, inspect, resume and cancel durable workflows.

These are ordinary Grid tools, discovered by the existing auto-discovery
(``tools.function_tools._discover_tool_modules`` finds ``WORKFLOW_TOOLS``
below). A tool appears to an agent only when the system's config names it —
the existing allowlist — so a workflow is never reachable by accident.

Every call goes through the action gate like any other tool (the module
declares ``TOOL_EFFECTS`` / ``TOOL_ISOLATION``). The *steps* a run schedules go
through the very tools the factory resolved for this agent
(``core.workflows.api.build_invoker``): the workflow layer never invokes a raw
function, and a read-like step cannot replay an effectful tool.

``workflow_run`` validates the spec first and returns a structured
``SPEC_INVALID`` with the problems, never a half-started run.

Minimal usable example (an agent whose config lists ``workflow_run``)::

    spec_json = '''{
      "name": "gather",
      "goal": "read two files and check the result",
      "steps": [
        {"id": "a", "kind": "read", "tool": "file_read",
         "arguments": {"filepath": "README.md"}},
        {"id": "b", "kind": "read", "tool": "file_read",
         "arguments": {"filepath": "docs/plan.md"}, "depends_on": ["a"]},
        {"id": "g", "kind": "gate", "check": "deps.data_present",
         "depends_on": ["b"]}
      ]}'''
    # workflow_run(spec_json=spec_json)  ->  {"run_id": "...", "status": "completed", ...}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from agents import RunContextWrapper, function_tool
from pydantic import ValidationError

from core.workflows.api import (
    WorkflowScopeError,
    build_invoker,
    get_service,
)
from core.workflows.journal import NotFoundError
from core.workflows.reconcile import ReconcileError
from core.workflows.spec import WorkflowSpec, WorkflowRunView, validate
from utils.tool_effects import DELEGATE, read
from utils.tool_isolation import WORKSPACE


# -- effects / isolation ---------------------------------------------------------

#: run/resume/cancel start or steer work (their own calls are judged one by one,
#: as ``orchestrate`` declares); status only reads.
TOOL_EFFECTS = {
    "workflow_run": DELEGATE,
    "workflow_resume": DELEGATE,
    "workflow_cancel": DELEGATE,
    "workflow_status": read(),
}

#: All four run in the server process, confined to the run's workspace.
TOOL_ISOLATION = {
    "workflow_run": WORKSPACE,
    "workflow_resume": WORKSPACE,
    "workflow_cancel": WORKSPACE,
    "workflow_status": WORKSPACE,
}


# -- helpers ---------------------------------------------------------------------


def _factory(context: RunContextWrapper) -> Any:
    raw = getattr(context, "context", None)
    return getattr(raw, "factory", None)


def _caller(context: RunContextWrapper) -> tuple[Optional[str], Optional[str]]:
    raw = getattr(context, "context", None)
    return getattr(raw, "context_id", None), getattr(raw, "user_id", None)


def _workspace(factory: Any) -> str:
    try:
        return str(factory.config.get_working_directory())
    except Exception:  # noqa: BLE001 - a missing working dir means "unscoped"
        return ""


def _db_path(factory: Any) -> Path:
    try:
        base = Path(factory._logs_directory_path())
    except Exception:  # noqa: BLE001
        try:
            base = Path(factory.config.get_logs_directory())
        except Exception:  # noqa: BLE001
            base = Path(".")
    base.mkdir(parents=True, exist_ok=True)
    return base / "workflow_runs.db"


def _service(factory: Any):
    workspace = _workspace(factory)
    return get_service(
        _db_path(factory),
        owner=f"workflow-api:{workspace}",
        invoke_factory=lambda: build_invoker(factory),
        workspace_root=workspace or None,
    )


def _context_id(factory: Any, context: RunContextWrapper) -> Optional[str]:
    context_id, _ = _caller(context)
    if context_id:
        return context_id
    try:
        return factory.get_active_context_id()
    except Exception:  # noqa: BLE001
        return None


def _error(message: str, *, code: str = "ERROR") -> str:
    return json.dumps({"status": code, "error": message}, ensure_ascii=False)


def _view_json(view: WorkflowRunView) -> str:
    """The machine-readable run view. Cost is reported truthfully: this layer
    does not tally model spend, so it says ``unknown`` rather than a false zero."""
    return json.dumps(
        {
            "run_id": view.run_id,
            "status": view.status,
            "spec_sha256": view.spec_sha256,
            "budget_usd_micros": view.budget_usd_micros,
            "spent_usd_micros": view.spent_usd_micros,
            "cost_status": "unknown",
            "steps": [step.model_dump() for step in view.steps],
            "final_report": view.final_report,
        },
        ensure_ascii=False,
        default=str,
    )


def _spec_invalid_from_validation(exc: ValidationError) -> str:
    errors = [
        {"code": "SPEC_INVALID", "message": item.get("msg", "invalid"), "path": list(item.get("loc", ()))}
        for item in exc.errors(include_url=False)
    ]
    return json.dumps({"status": "SPEC_INVALID", "errors": errors}, ensure_ascii=False, default=str)


# -- tools -----------------------------------------------------------------------


@function_tool
async def workflow_run(
    context: RunContextWrapper,
    spec_json: str,
    goal: str = "",
    budget_usd: Optional[float] = None,
) -> str:
    """Run a durable workflow: a validated DAG of typed steps, persisted and resumable.

    Args:
        spec_json: The workflow spec as JSON: `name`, `goal`, `steps` (each with
            `id`, `kind` = read/egress/write/exec/gate, `tool`, `arguments`,
            `depends_on`, `write_paths`, `timeout_seconds`, `required`) and
            optional `max_parallel`, `deadline_seconds`, `queue_timeout_seconds`,
            `budget_usd`, `init_tools`.
        goal: The goal, used when the spec omits it.
        budget_usd: An optional spend cap for the run.

    Every step runs through this agent's own tools under the action policy; the
    run is journaled and can be inspected or resumed later. Returns the run's
    id, status and per-step state, or a structured SPEC_INVALID with the
    problems when the spec is rejected (nothing is started then).
    """
    factory = _factory(context)
    if factory is None:
        return _error("no access to AgentFactory (expected context.context.factory)", code="NO_FACTORY")

    try:
        payload = json.loads(spec_json) if isinstance(spec_json, str) else spec_json
    except (ValueError, TypeError) as exc:
        return json.dumps(
            {"status": "SPEC_INVALID", "errors": [{"code": "SPEC_INVALID", "message": f"spec is not JSON: {exc}"}]},
            ensure_ascii=False,
        )
    if not isinstance(payload, dict):
        return json.dumps(
            {"status": "SPEC_INVALID", "errors": [{"code": "SPEC_INVALID", "message": "spec must be a JSON object"}]},
            ensure_ascii=False,
        )
    payload.setdefault("goal", goal or "")
    payload.setdefault("name", payload.get("name") or "workflow")

    try:
        parsed = WorkflowSpec.model_validate(payload)
    except ValidationError as exc:
        return _spec_invalid_from_validation(exc)

    errors = validate(parsed, workspace_root=_workspace(factory) or None)
    if errors:
        return json.dumps(
            {"status": "SPEC_INVALID", "errors": [error.model_dump() for error in errors]},
            ensure_ascii=False,
            default=str,
        )

    service = _service(factory)
    budget_micros = round(budget_usd * 1_000_000) if budget_usd is not None else None
    try:
        view = await service.start(
            parsed,
            context_id=_context_id(factory, context),
            user_id=_caller(context)[1],
            budget_usd_micros=budget_micros,
        )
    except WorkflowScopeError as exc:
        return _error(str(exc), code="FORBIDDEN")
    return _view_json(view)


@function_tool
async def workflow_status(context: RunContextWrapper, run_id: str) -> str:
    """Read a workflow run's status: per-step state, effects and final report.

    Args:
        run_id: The id `workflow_run` returned.
    """
    factory = _factory(context)
    if factory is None:
        return _error("no access to AgentFactory", code="NO_FACTORY")
    try:
        view = _service(factory).get(
            run_id, context_id=_context_id(factory, context), user_id=_caller(context)[1]
        )
    except NotFoundError as exc:
        return _error(str(exc), code="NOT_FOUND")
    except WorkflowScopeError as exc:
        return _error(str(exc), code="FORBIDDEN")
    return _view_json(view)


@function_tool
async def workflow_resume(
    context: RunContextWrapper,
    run_id: str,
    write_policy: str = "never",
    additional_budget_usd: Optional[float] = None,
) -> str:
    """Resume an interrupted (crashed/stopped) or budget-paused workflow run.

    Args:
        run_id: The id `workflow_run` returned.
        write_policy: 'never' (default, safe) refuses to replay an interrupted
            workspace step whose effect is unverified; 'verify' replays it only
            when a trusted verifier confirms it is safe. Egress/agent steps are
            never auto-replayed.
        additional_budget_usd: A new spend cap that continues a budget-paused run.
    """
    factory = _factory(context)
    if factory is None:
        return _error("no access to AgentFactory", code="NO_FACTORY")
    if write_policy not in ("never", "verify"):
        return _error("write_policy must be 'never' or 'verify'", code="BAD_REQUEST")
    budget_micros = round(additional_budget_usd * 1_000_000) if additional_budget_usd is not None else None
    try:
        view = await _service(factory).resume(
            run_id,
            context_id=_context_id(factory, context),
            user_id=_caller(context)[1],
            write_policy=write_policy,
            budget_usd_micros=budget_micros,
        )
    except NotFoundError as exc:
        return _error(str(exc), code="NOT_FOUND")
    except WorkflowScopeError as exc:
        return _error(str(exc), code="FORBIDDEN")
    except ReconcileError as exc:
        return _error(str(exc), code="NOT_RESUMABLE")
    return _view_json(view)


@function_tool
async def workflow_cancel(context: RunContextWrapper, run_id: str, reason: str = "cancelled") -> str:
    """Cancel a running workflow run; in-flight steps drain, unknown effects stay unknown.

    Args:
        run_id: The id `workflow_run` returned.
        reason: Why it is being cancelled (recorded with the run).
    """
    factory = _factory(context)
    if factory is None:
        return _error("no access to AgentFactory", code="NO_FACTORY")
    try:
        view = await _service(factory).cancel(
            run_id, context_id=_context_id(factory, context), user_id=_caller(context)[1], reason=reason
        )
    except NotFoundError as exc:
        return _error(str(exc), code="NOT_FOUND")
    except WorkflowScopeError as exc:
        return _error(str(exc), code="FORBIDDEN")
    return _view_json(view)


WORKFLOW_TOOLS = {
    "workflow_run": workflow_run,
    "workflow_status": workflow_status,
    "workflow_resume": workflow_resume,
    "workflow_cancel": workflow_cancel,
}
