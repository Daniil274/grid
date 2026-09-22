"""Coordinator tools: run a bounded worker pipeline from inside a Grid chat.

The same engine the CLI example drives (``core/pipeline_runtime.py``): the
coordinator opens a run for one goal, launches workers under explicit contracts,
waits for their evidence, reviews it and closes the run. Making these ordinary
Grid tools is what lets the router hand a large task to the coordinator agent
like any other system, instead of the pipeline living only behind its own CLI.

One run per factory at a time. State is written next to the agent logs; it is an
audit record, not a resumable checkpoint.
"""

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional
from weakref import WeakKeyDictionary

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents import RunContextWrapper, function_tool

from core.pipeline_runtime import PipelineRuntime, TOOL_PROFILES

EXAMPLE = ROOT / "examples" / "coordinator-pipeline"
DEFAULT_TOOLS_DIRECTORY = "examples/coder/tools"


@dataclass
class _Session:
    supervisor: Any
    catalog: Any
    runtime: Optional[PipelineRuntime] = None


_SESSIONS: "WeakKeyDictionary[Any, _Session]" = WeakKeyDictionary()
# A host that builds the run itself (the CLI example) registers it here.
_HOST_SESSION: Optional[_Session] = None


def attach_runtime(runtime: PipelineRuntime, *, supervisor=None, catalog=None, factory=None) -> None:
    """Drive an already-built run with these tools.

    The CLI opens the run before the coordinator starts, so both entry points
    expose one vocabulary instead of two sets of look-alike tools.
    """
    global _HOST_SESSION
    session = _Session(
        supervisor=supervisor or runtime.supervisor, catalog=catalog, runtime=runtime
    )
    if factory is not None:
        _SESSIONS[factory] = session
    else:
        _HOST_SESSION = session


def _factory(context: Any) -> Any:
    """The factory running this call, or the process-wide current one."""
    factory = getattr(getattr(context, "context", None), "factory", None)
    if factory is None:
        from utils.path_utils import get_current_factory

        factory = get_current_factory()
    if factory is None:
        raise LookupError("No running Grid factory; pipeline tools need a live run")
    return factory


def _lookup_session(context: Any) -> Optional[_Session]:
    try:
        session = _SESSIONS.get(_factory(context))
    except LookupError:
        session = None
    return session or _HOST_SESSION


def _error(message: str, **extra) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


def _worker_provider(factory: Any, context: Any) -> str:
    """Workers use the provider of the calling agent's own model."""
    config = factory.config
    agent_id = getattr(getattr(context, "context", None), "agent_id", None)
    try:
        agent = config.get_agent(agent_id or config.get_default_agent())
    except Exception:
        agent = config.get_agent(config.get_default_agent())
    return config.get_model(agent.model).provider


def _tools_directory(factory: Any) -> str:
    project_tools = getattr(factory.config.config.settings, "project_tools", None)
    directory = getattr(project_tools, "tools_directory", None)
    return directory or str(ROOT / DEFAULT_TOOLS_DIRECTORY)


def _session(context: Any, *, deadline: int) -> _Session:
    """Build the supervisor and catalog once per factory."""
    from core.agent_catalog import AgentCatalog
    from core.background_agents import BackgroundAgentSupervisor

    factory = _factory(context)
    session = _SESSIONS.get(factory)
    if session is None:
        catalog = AgentCatalog(
            factory.config,
            tools_directory=_tools_directory(factory),
            provider_key=_worker_provider(factory, context),
        )
        supervisor = BackgroundAgentSupervisor(
            factory=factory,
            catalog=catalog,
            max_concurrency=4,
            default_timeout_seconds=min(300, deadline),
            max_timeout_seconds=deadline,
            worker_instructions=(EXAMPLE / "skills" / "worker.md").read_text(
                encoding="utf-8"
            ),
        )
        session = _Session(supervisor=supervisor, catalog=catalog)
        _SESSIONS[factory] = session
    return session


def _open_runtime(context: Any) -> PipelineRuntime:
    """The runtime of the open run, or an explanation of what to do first."""
    session = _lookup_session(context)
    runtime = session.runtime if session else None
    if runtime is None or runtime.state["status"] != "running":
        raise LookupError("No open pipeline run: call pipeline_start with the goal first")
    return runtime


@function_tool
async def pipeline_start(
    context: RunContextWrapper,
    goal: str,
    deadline_seconds: int = 900,
    max_attempts: int = 3,
    max_launches: int = 20,
) -> str:
    """Open a pipeline run for one goal and return the worker system catalog.

    Call this before any other pipeline tool. The answer lists the exact model
    keys, tool names and limits a task may use. The deadline covers the whole
    run: your own reasoning, the queue and every worker.
    """
    if not goal.strip():
        return _error("Supply a concrete goal for the run")
    factory = _factory(context)
    session = _session(context, deadline=deadline_seconds)
    if session.runtime is not None and session.runtime.state["status"] == "running":
        return _error(
            "A run is already open; call pipeline_finish or pipeline_block before starting another",
            run_id=session.runtime.state["run_id"],
        )
    workdir = Path(factory.config.get_working_directory()).resolve()
    state_path = (
        Path(factory.config.get_logs_directory()).resolve()
        / "pipeline-runs"
        / f"{uuid.uuid4().hex}.json"
    )
    session.runtime = PipelineRuntime(
        session.supervisor,
        goal=goal,
        workdir=workdir,
        state_path=state_path,
        deadline_seconds=deadline_seconds,
        max_attempts=max_attempts,
        max_launches=max_launches,
    )
    return json.dumps(
        {
            "run_id": session.runtime.state["run_id"],
            "workdir": str(workdir),
            "state_path": str(state_path),
            "system": session.runtime.system(),
        },
        ensure_ascii=False,
    )


@function_tool
async def pipeline_task(
    context: RunContextWrapper,
    goal: str,
    scope: str,
    inputs: str,
    done_criteria: str,
    limits: str,
    kind: Literal["research", "change", "verification"],
    model: str,
    tools: list[str],
    timeout_seconds: int = 300,
    verification_for: Optional[str] = None,
) -> str:
    """Launch one bounded worker. Only research tasks may run concurrently.

    Use exact model and tool names from the catalog of pipeline_start. A change
    or verification task holds the shared workspace alone. Verification must
    name the completed, unreviewed change it checks in verification_for.
    """
    try:
        runtime = _open_runtime(context)
        return json.dumps(
            await runtime.start_task(
                goal=goal,
                scope=scope,
                inputs=inputs,
                done_criteria=done_criteria,
                limits=limits,
                kind=kind,
                model=model,
                tools=tools,
                timeout_seconds=timeout_seconds,
                verification_for=verification_for,
            ),
            ensure_ascii=False,
        )
    except (LookupError, ValueError) as exc:
        return _error(str(exc), tool_profiles={k: sorted(v) for k, v in TOOL_PROFILES.items()})


@function_tool
async def pipeline_wait(
    context: RunContextWrapper, task_ids: list[str], timeout_seconds: float = 60
) -> str:
    """Wait for the next worker to finish (at most 60 seconds) and read its state."""
    try:
        runtime = _open_runtime(context)
        return json.dumps(
            await runtime.wait_tasks(task_ids, timeout_seconds), ensure_ascii=False
        )
    except (LookupError, ValueError) as exc:
        return _error(str(exc))


@function_tool
async def pipeline_inspect(context: RunContextWrapper) -> str:
    """Read contracts, attempts, worker results and reviews. Do not poll this tool."""
    try:
        runtime = _open_runtime(context)
        return json.dumps(await runtime.snapshot(), ensure_ascii=False)
    except LookupError as exc:
        return _error(str(exc))


@function_tool
async def pipeline_review(
    context: RunContextWrapper,
    task_id: str,
    decision: Literal["accepted", "rejected", "discarded"],
    evidence: str,
    verification_task_id: Optional[str] = None,
) -> str:
    """Record a review with the inspected files and checks as evidence.

    Accept verification only if its checks passed. Accept a change only with the
    id of that accepted verification. Reject failed work before retrying it.
    Discard only unnecessary research or verification, never a change.
    """
    try:
        runtime = _open_runtime(context)
        return json.dumps(
            await runtime.review_task(
                task_id,
                decision=decision,
                evidence=evidence,
                verification_task_id=verification_task_id,
            ),
            ensure_ascii=False,
        )
    except (LookupError, ValueError) as exc:
        return _error(str(exc))


@function_tool
async def pipeline_retry(context: RunContextWrapper, task_id: str, feedback: str) -> str:
    """Start a correction of rejected work, keeping its contract and history."""
    try:
        runtime = _open_runtime(context)
        return json.dumps(await runtime.retry_task(task_id, feedback), ensure_ascii=False)
    except (LookupError, ValueError) as exc:
        return _error(str(exc))


@function_tool
async def pipeline_interrupt(context: RunContextWrapper, task_id: str, reason: str) -> str:
    """Cancel an unnecessary worker. Its task still needs a review or disposition."""
    try:
        runtime = _open_runtime(context)
        return json.dumps(await runtime.interrupt_task(task_id, reason), ensure_ascii=False)
    except (LookupError, ValueError) as exc:
        return _error(str(exc))


@function_tool
async def pipeline_finish(context: RunContextWrapper, summary: str) -> str:
    """Close the run after the required work is accepted; state results and limits."""
    try:
        runtime = _open_runtime(context)
        state = await runtime.finish_run(summary)
        return json.dumps(
            {"status": state["status"], "summary": state["summary"]}, ensure_ascii=False
        )
    except (LookupError, ValueError) as exc:
        return _error(str(exc))


@function_tool
async def pipeline_block(context: RunContextWrapper, reason: str) -> str:
    """Stop the run with an explicit blocker when the goal cannot be met in limits."""
    if not reason.strip():
        return _error("Supply a concrete blocker")
    try:
        runtime = _open_runtime(context)
        state = await runtime.close("blocked", reason)
        return json.dumps(
            {"status": state["status"], "summary": state["summary"]}, ensure_ascii=False
        )
    except LookupError as exc:
        return _error(str(exc))


PIPELINE_TOOLS = {
    "pipeline_start": pipeline_start,
    "pipeline_task": pipeline_task,
    "pipeline_wait": pipeline_wait,
    "pipeline_inspect": pipeline_inspect,
    "pipeline_review": pipeline_review,
    "pipeline_retry": pipeline_retry,
    "pipeline_interrupt": pipeline_interrupt,
    "pipeline_finish": pipeline_finish,
    "pipeline_block": pipeline_block,
}
