"""
Orchestrator tools: meta-tools that can spawn dynamic agents and run reviewed/committee workflows.

These tools are intentionally conservative: they require being executed inside Grid,
where `context.context.factory` (see `core.agent_factory.GridRunContext`) provides access
to the current `AgentFactory` instance.

"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from agents import RunContextWrapper, function_tool

from core.model_access import tally_spend
from core.pricing import MICRO
from core.run_stream import STOPPED_ERROR, RunOutcome

logger = logging.getLogger(__name__)
verbose_logger = logging.getLogger("grid.verbose")



@dataclass
class _ReviewDecision:
    decision: str  # "approve" | "revise"
    feedback: str = ""


@dataclass
class _Vote:
    vote: str  # "accept" | "reject"
    reason: str = ""


def _get_factory_from_context(ctx: RunContextWrapper) -> Any:
    raw = getattr(ctx, "context", None)
    return getattr(raw, "factory", None)


def _extract_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    # Best effort
    return str(output)


def _parse_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON parse: expects a JSON object, optionally wrapped in code fences."""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove first fence line and the last fence if present
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    # Try direct
    try:
        val = json.loads(stripped)
        return val if isinstance(val, dict) else None
    except Exception:
        return None


def _coerce_optional_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        v = val.strip()
        if v == "" or v.lower() in ("none", "null"):
            return None
        return v
    if isinstance(val, (int, float, bool)):
        return str(val)
    return None


_CONTEXT_ID_VALID = re.compile(r"^ctx-[0-9a-f]{8,}$", re.IGNORECASE)
_CONTEXT_ID_SEARCH = re.compile(r"ctx-[0-9a-f]{8,}", re.IGNORECASE)


def _sanitize_context_id(val: Any) -> Optional[str]:
    """
    Normalize context_id for orchestrate: accept only ctx-<hex> tokens.
    Strips wrappers like 'Context ID: ctx-…' and rejects arbitrary garbage.
    """
    raw = _coerce_optional_str(val)
    if not raw:
        return None
    if _CONTEXT_ID_VALID.match(raw):
        return raw.lower()
    match = _CONTEXT_ID_SEARCH.search(raw)
    if match:
        cleaned = match.group(0).lower()
        if _CONTEXT_ID_VALID.match(cleaned):
            return cleaned
    logger.debug("orchestrate: ignored invalid context_id: %r", raw[:120])
    return None


def _coerce_tool_list(val: Any) -> Optional[List[str]]:
    """
    Accept:
    - list[str]
    - JSON string like '["filesystem","git"]'
    - comma-separated string like 'filesystem,git'
    """
    if val is None:
        return None
    if isinstance(val, list):
        out: List[str] = []
        for item in val:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out or None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        # JSON list string
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return _coerce_tool_list(parsed)
            except Exception:
                pass
        # CSV fallback
        parts = [p.strip() for p in s.split(",")]
        parts = [p for p in parts if p]
        return parts or None
    # Fallback: stringified single value
    return [str(val).strip()] if str(val).strip() else None


async def _review_once(factory: Any, *, model_key: str, draft: str, goal: str) -> _ReviewDecision:
    reviewer_instructions = (
        "You are a reviewer. You will be given a goal and a draft solution.\n"
        "You need to find errors/gaps/risks and decide: approve or revise.\n"
        "Return strict JSON: {\"decision\":\"approve|revise\",\"feedback\":\"...\"}.\n"
        "If approve — feedback can be empty."
    )
    reviewer = await factory.create_dynamic_agent(
        name=f"reviewer-{uuid.uuid4().hex[:6]}",
        instructions=reviewer_instructions,
        model_key=model_key,
        tool_names=[],
    )
    prompt = f"GOAL:\n{goal}\n\nDRAFT:\n{draft}\n"
    out = await factory.run_agent_object_simple(reviewer, prompt)
    parsed = _parse_json_from_text(_extract_text(out)) or {}
    decision = str(parsed.get("decision", "")).strip().lower()
    feedback = str(parsed.get("feedback", "")).strip()
    if decision not in ("approve", "revise"):
        # Fail-safe: ask for revision
        return _ReviewDecision(decision="revise", feedback="Reviewer returned invalid format; revision required.")
    return _ReviewDecision(decision=decision, feedback=feedback)


async def _committee_vote(factory: Any, *, model_key: str, goal: str, draft: str, n: int) -> Tuple[bool, List[_Vote]]:
    judges: List[_Vote] = []
    for _ in range(max(1, n)):
        judge_instructions = (
            "You are a judge. You will be given a goal and a solution.\n"
            "Evaluate whether it is good enough for acceptance.\n"
            "Return strict JSON: {\"vote\":\"accept|reject\",\"reason\":\"...\"}."
        )
        judge = await factory.create_dynamic_agent(
            name=f"judge-{uuid.uuid4().hex[:6]}",
            instructions=judge_instructions,
            model_key=model_key,
            tool_names=[],
        )
        prompt = f"GOAL:\n{goal}\n\nSOLUTION:\n{draft}\n"
        out = await factory.run_agent_object_simple(judge, prompt)
        parsed = _parse_json_from_text(_extract_text(out)) or {}
        vote = str(parsed.get("vote", "")).strip().lower()
        reason = str(parsed.get("reason", "")).strip()
        if vote not in ("accept", "reject"):
            vote = "reject"
            reason = reason or "Invalid vote format."
        judges.append(_Vote(vote=vote, reason=reason))

    accept_count = sum(1 for j in judges if j.vote == "accept")
    reject_count = len(judges) - accept_count
    return accept_count > reject_count, judges


def _orchestrate_config(factory: Any) -> Any:
    try:
        return factory.config.get_tool("orchestrate")
    except Exception:
        return None


def _configured_models(factory: Any, tier: Optional[str] = None) -> List[str]:
    """The executor's model keys: the named tier's, else the tool's ``models``.

    ValueError for a tier the config does not define: the caller chose it from
    the tool's description, so it learns which ones there are.
    """
    tool = _orchestrate_config(factory)
    if tier:
        tiers = getattr(tool, "tiers", None)
        tiers = tiers if isinstance(tiers, dict) else {}
        if tier not in tiers:
            known = ", ".join(sorted(tiers)) or "none - omit tier"
            raise ValueError(f"unknown tier '{tier}'; this system's tiers: {known}")
        models = tiers[tier]
    else:
        models = getattr(tool, "models", None)
    if not isinstance(models, (str, list)):
        return []
    return _coerce_tool_list(models) or []


def _chosen_models(factory: Any, model: str, model_keys: List[str]) -> List[str]:
    """*model* first, then the rest of *model_keys*: a choice the system offers.

    Only with ``tools.orchestrate.model_choice``, and only among the system's
    executor models - ``models`` and the tiers. ValueError otherwise, naming
    what there is to choose from.
    """
    tool = _orchestrate_config(factory)
    if not getattr(tool, "model_choice", False):
        raise ValueError("this system offers no choice of model; pick a tier")
    tiers = getattr(tool, "tiers", None) or {}
    offered = list(dict.fromkeys(
        [*(_coerce_tool_list(getattr(tool, "models", None)) or []), *(key for tier in tiers.values() for key in tier)]
    ))
    if model not in offered:
        raise ValueError(f"model '{model}' is not one of this system's executor models: {', '.join(offered)}")
    return [model, *(key for key in model_keys if key != model)]


#: tools.orchestrate.min_timeout_seconds when the config does not say.
DEFAULT_MIN_TIMEOUT = 600
#: tools.orchestrate.resume_compact_tokens when the config does not say.
DEFAULT_RESUME_COMPACT_TOKENS = 50000


def _setting(tool: Any, name: str, default: Optional[int]) -> Optional[int]:
    value = getattr(tool, name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _executor_timeout(factory: Any, requested: Optional[int]) -> Optional[int]:
    """The executor's time: the caller's request, raised to the system's least
    (``min_timeout_seconds``) and held to its most (``max_timeout_seconds``,
    else settings.agent_timeout; 0 is no limit). None - the caller asked for
    nothing - leaves settings.agent_timeout to the run."""
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        return None
    tool = _orchestrate_config(factory)
    ceiling = _setting(tool, "max_timeout_seconds", None)
    if ceiling is None:
        ceiling = factory.config.get_agent_timeout()
    seconds = max(requested, _setting(tool, "min_timeout_seconds", DEFAULT_MIN_TIMEOUT) or 0)
    return min(seconds, ceiling) if isinstance(ceiling, int) and ceiling > 0 else seconds


# -- executors a conversation can continue -----------------------------------
# Every executor gets a context id of its own; its session (what it did, step
# by step) is keyed by it. Passing that id back to orchestrate continues the
# same agent: same role, tools and models unless the call gives new ones. How
# each was made is kept in the conversation's metadata, so the id works after
# a restart as the session does; runs outside any conversation keep it here.

_EXECUTORS_KEY = "orchestrate_executors"
_EXECUTORS_KEPT = 40
_LOOSE_EXECUTORS: Dict[str, Dict[str, Dict[str, Any]]] = {}
_LOOSE_KEPT = 200


def _executor_name(handle: str) -> str:
    return f"executor-{handle[4:]}"


def _in_conversation(factory: Any, chat_id: str) -> bool:
    manager = getattr(factory, "context_manager", None)
    try:
        return manager is not None and manager.has_context(chat_id) is True
    except Exception:
        return False


def _executor_book(factory: Any, chat_id: str) -> Dict[str, Dict[str, Any]]:
    """The executors of conversation *chat_id*: context id -> how it was made."""
    if _in_conversation(factory, chat_id):
        try:
            stored = factory.context_manager.get_metadata(_EXECUTORS_KEY, context_id=chat_id)
        except Exception:
            logger.debug("orchestrate: the executors of %s are unreadable", chat_id, exc_info=True)
            stored = None
        return dict(stored) if isinstance(stored, dict) else {}
    return dict(_LOOSE_EXECUTORS.get(chat_id, {}))


def _keep_executor(factory: Any, chat_id: str, handle: str, record: Dict[str, Any]) -> None:
    book = _executor_book(factory, chat_id)
    book.pop(handle, None)
    book[handle] = record
    while len(book) > _EXECUTORS_KEPT:
        book.pop(next(iter(book)))
    if _in_conversation(factory, chat_id):
        try:
            factory.context_manager.set_metadata(_EXECUTORS_KEY, book, context_id=chat_id)
            return
        except Exception:
            logger.warning("orchestrate: could not record executor %s of %s", handle, chat_id, exc_info=True)
    _LOOSE_EXECUTORS.pop(chat_id, None)
    _LOOSE_EXECUTORS[chat_id] = book
    while len(_LOOSE_EXECUTORS) > _LOOSE_KEPT:
        _LOOSE_EXECUTORS.pop(next(iter(_LOOSE_EXECUTORS)))


#: Put before the task of a continued executor.
_CONTINUED = (
    "[You are continuing your earlier work on this assignment: your previous "
    "steps and their results are above{summarized}. Do not repeat what is done.]\n\n"
)


def _is_model(factory: Any, key: str) -> bool:
    try:
        factory.config.get_model(key)
        return True
    except Exception:
        return False


@function_tool
async def orchestrate(
    context: RunContextWrapper,
    task: str,
    agent_system_prompt: Optional[str] = None,
    executor_tools: Optional[Union[List[str], str]] = None,
    context_id: Optional[str] = None,
    init_tools: Optional[str] = None,
    system_skills: Optional[Union[List[str], str]] = None,
    tier: Optional[str] = None,
    model: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    """Hand a subtask to an agent and get back its report.

    The agent works in its own context: the searching, reading and trial runs
    it does stay out of yours, and you receive only its answer, with status
    (completed, timeout, max_turns, error), seconds, model calls, tokens and
    cost when known, and its context_id. Several orchestrate calls inside
    ``parallel`` run at the same time.

    Args:
        task: What the agent should do and what to report back. When continuing an
            agent, what it should do next, e.g. "Finish the remaining steps and report".
        agent_system_prompt: The agent's role and standing rules.
        executor_tools: Keys of your own tools the agent gets, e.g. ["web_search", "web_fetch"].
        context_id: Empty starts a new agent. The context_id from an earlier orchestrate
            result of this conversation continues that agent: it keeps everything it already
            did (a long history is summarized first) and its role, tools and model unless
            you pass new ones. Use it after status timeout or max_turns, or to give the same
            agent a follow-up, instead of starting over.
        init_tools: JSON list of tool calls made before the agent starts; their results go
            into its instructions. Format: '[{"name": "tool_name", "parameters": {...}}]'.
        system_skills: Skill names to load for the agent, as a list or comma-separated.
        tier: A model tier of this system, e.g. "fast" for search and checks; empty for the
            default models.
        model: One executor model by key from the list in your instructions; tried before
            the tier's models.
        timeout_seconds: Time for the agent, within the system's least and most. Past it the
            agent writes a report of what it did, and you can continue it by its context_id.

    Only the summary and Args above reach the model. The executor runs on the
    first available model of the chosen one, the tier or the tool's
    ``models``, moving to the next when a request fails; without ``models``,
    on the default agent's model. Turns are bounded by settings.max_turns,
    which the agent is never told. The caller's own deadline is paused while
    the executor works (core.deadlines): the executor's time is its own.
    orchestrate calls of one conversation run one after another as steps of
    its serial pipeline (core.tracing.pipeline_registry); other conversations
    are not held up.
    """
    factory = _get_factory_from_context(context)
    if factory is None:
        return "❌ orchestrate: no access to AgentFactory (expected context.context.factory)."

    # Import pipeline registry (inside function to avoid circular import)
    from core.tracing.pipeline_registry import PipelineRegistry, PipelineStatus
    from core.deadlines import waiting_for_delegate

    registry = PipelineRegistry()
    raw_ctx = getattr(context, "context", None)
    # The conversation is the caller's: an id the model passes names an
    # executor to continue, never another conversation to work in.
    active_context_id = (
        _sanitize_context_id(getattr(raw_ctx, "context_id", None))
        or _sanitize_context_id(factory.get_active_context_id())
    )
    if not active_context_id:
        active_context_id = factory.context_manager.start_new_context()
    ctx_user_id = getattr(raw_ctx, "user_id", None)
    inherited_pipeline_id = _coerce_optional_str(getattr(raw_ctx, "pipeline_id", None))

    pipeline_id = inherited_pipeline_id or await registry.get_or_create_pipeline(
        orchestrator_name="orchestrate",
        context_id=active_context_id,
        user_id=ctx_user_id
    )
    if raw_ctx is not None:
        if getattr(raw_ctx, "context_id", None) is None:
            raw_ctx.context_id = active_context_id
        raw_ctx.pipeline_id = pipeline_id
        raw_ctx.execution_mode = "serial_subtree"

    # A new executor, or the one an earlier result named.
    requested = _sanitize_context_id(context_id)
    previous = _executor_book(factory, active_context_id).get(requested) if requested else None
    notes: List[str] = []
    if requested and previous is None:
        notes.append(
            f"context_id {requested} is not an agent started in this conversation; "
            "a new agent took the task"
        )
    handle = requested if previous is not None else f"ctx-{uuid.uuid4().hex[:8]}"
    previous = previous or {}

    logger.info(
        f"orchestrate: START | task_len={len(task)} | pipeline_id={pipeline_id} | "
        f"executor={handle} | continued={bool(previous)}"
    )
    verbose_logger.debug(
        f"\n{'='*80}\nORCHESTRATE START\n{'='*80}\n"
        f"Task: {task}\n"
        f"Agent system prompt: {agent_system_prompt[:500] if agent_system_prompt else 'None'}\n"
        f"Executor tools: {executor_tools}\n{'='*80}\n"
    )

    # A continued executor keeps how it was made unless this call says otherwise.
    tier = _coerce_optional_str(tier) or previous.get("tier")
    model = _coerce_optional_str(model) or previous.get("model")

    # The executor's models come from the orchestrate tool config only, in
    # order: the caller's own models are not the executor's, so when none of
    # these is available the step fails instead of moving on to another model.
    try:
        model_keys = _configured_models(factory, tier)
        if model:
            model_keys = _chosen_models(factory, model, model_keys)
    except ValueError as exc:
        return f"❌ orchestrate: {exc}."
    unknown = [key for key in model_keys if not _is_model(factory, key)]
    if unknown:
        # resolve_model_key would quietly put an unknown key on the default agent's model.
        source = f"tier '{tier}'" if tier else "tools.orchestrate.models"
        return (
            f"❌ orchestrate: {source} names models that are not in this "
            f"system's config: {', '.join(unknown)}."
        )
    run_timeout = _executor_timeout(factory, timeout_seconds)
    resolved_model_key = factory.resolve_model_key(model_keys[0] if model_keys else None)
    fallback_model_keys = model_keys[1:]
    coerced_executor_tools = _coerce_tool_list(executor_tools) or previous.get("tools")

    # Parse init_tools JSON (optional context-gathering tools)
    parsed_init_tools = None
    if init_tools:
        try:
            parsed = json.loads(init_tools)
            if isinstance(parsed, list):
                parsed_init_tools = parsed
            else:
                logger.warning(f"orchestrate: init_tools must be a JSON array, got {type(parsed).__name__}")
        except Exception as e:
            logger.warning(f"orchestrate: cannot parse init_tools JSON: {e} — value: {init_tools[:200]}")
            notes.append(f"init_tools was not valid JSON ({e}); the agent started without them")
    if parsed_init_tools is None:
        # Run again on a continued executor: what they read (its task in the
        # tracker, say) is current, and they belong to its instructions.
        parsed_init_tools = previous.get("init_tools")

    base_instructions = _coerce_optional_str(agent_system_prompt) or previous.get("instructions")

    coerced_system_skills = _coerce_tool_list(system_skills) or previous.get("skills")

    # The executor acts under the caller's trusted task; the task text here
    # was written by an agent and reaches the policy only as its purpose.
    # Its init_tools run under the same state as the executor itself.
    from core.action_policy import delegated_state

    action_state = delegated_state(
        getattr(raw_ctx, "action_state", None), "orchestrate", task
    )

    executor = await factory.create_dynamic_agent(
        name=_executor_name(handle),
        instructions=base_instructions,
        model_key=resolved_model_key,
        tool_names=coerced_executor_tools,
        init_tools=parsed_init_tools,
        system_skills=coerced_system_skills,
        action_state=action_state,
        fallback_model_keys=fallback_model_keys,
    )

    record = {
        "instructions": base_instructions,
        "tools": coerced_executor_tools,
        "skills": coerced_system_skills,
        "tier": tier,
        "model": model,
        "init_tools": parsed_init_tools,
        "runs": int(previous.get("runs") or 0) + 1,
        "status": "running",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # Kept before it runs: a run the user stops can be continued too.
    _keep_executor(factory, active_context_id, handle, record)

    run_input = task
    compacted = None
    if previous:
        # Its history is in its session, keyed by its name in this conversation;
        # a long one is summarized, so the next steps do not drag all of it along.
        limit = _setting(_orchestrate_config(factory), "resume_compact_tokens", DEFAULT_RESUME_COMPACT_TOKENS)
        if limit:
            try:
                session = factory._get_agent_session(executor.name, active_context_id)
                compacted = await factory.summarize_session(
                    session, resolved_model_key, above_tokens=limit
                )
            except Exception:
                logger.warning("orchestrate: could not summarize executor %s", handle, exc_info=True)
                compacted = None
            if not isinstance(compacted, dict):
                compacted = None
        run_input = _CONTINUED.format(summarized=", summarized" if compacted else "") + task

    # The executor reports into the caller's view (the web trace, not the
    # console) as a block of its own under this orchestrate call.
    observer = getattr(raw_ctx, "stream_observer", None)
    if hasattr(observer, "nested"):
        observer = observer.nested(
            "Executor", call_id=getattr(context, "tool_call_id", None)
        )
    else:
        observer = None
    run_error: Optional[str] = None
    caller_depth = getattr(raw_ctx, "action_depth", 0)
    action_depth = (caller_depth if isinstance(caller_depth, int) else 0) + 1

    # Execute with emergency shutdown handling
    started = time.monotonic()
    with tally_spend() as tally:
        try:
            async def run_executor() -> Any:
                return await factory.run_agent_object(
                    executor,
                    run_input,
                    context_id=active_context_id,
                    pipeline_id=pipeline_id,
                    stream_observer=observer,
                    action_state=action_state,
                    action_depth=action_depth,
                    user_id=ctx_user_id,
                    run_control=getattr(raw_ctx, "run_control", None),
                    timeout=run_timeout,
                )

            # The caller's clock stops while its executor works under its own.
            with waiting_for_delegate():
                outcome = await registry.run_serialized_step(
                    pipeline_id=pipeline_id,
                    agent_name=executor.name,
                    step_coro_factory=run_executor,
                    metadata={"model_key": resolved_model_key, "tools": coerced_executor_tools},
                )
        except asyncio.CancelledError:
            run_error = "Cancelled"
            _keep_executor(factory, active_context_id, handle, {**record, "status": "stopped"})
            # Task was cancelled - check if it was emergency shutdown
            status = await registry.get_pipeline_status(pipeline_id)

            if status.get("status") == PipelineStatus.EMERGENCY_STOPPED.value:
                # Return emergency shutdown information to orchestrator
                result = {
                    "emergency_stopped": True,
                    "emergency_reason": status.get("emergency_reason"),
                    "emergency_severity": status.get("emergency_severity"),
                    "pipeline_id": pipeline_id,
                    "completed_tasks": len(status.get("completed_tasks", [])),
                    "failed_tasks": len(status.get("failed_tasks", {})),
                    "total_tasks": status.get("all_tasks", 0),
                    "task": task,
                    "context_id": handle
                }
                result_json = json.dumps(result, ensure_ascii=False, indent=2)
                logger.warning(f"orchestrate: EMERGENCY STOPPED | pipeline_id={pipeline_id} | reason={status.get('emergency_reason')}")
                verbose_logger.debug(
                    f"\n{'='*80}\nORCHESTRATE EMERGENCY STOPPED\n{'='*80}\n"
                    f"Pipeline ID: {pipeline_id}\n"
                    f"Result:\n{result_json}\n{'='*80}\n"
                )
                return result_json
            # Re-raise if not emergency shutdown
            raise
        except Exception as exec_err:
            run_error = f"Executor failed: {exec_err}"
            logger.error(f"orchestrate: executor failed | error={exec_err}")
            outcome = RunOutcome(f"❌ Executor failed: {exec_err}", STOPPED_ERROR)
        finally:
            if observer is not None and hasattr(observer, "finish"):
                try:
                    observer.finish(error=run_error)
                except Exception:
                    logger.debug("orchestrate: failed to settle the trace", exc_info=True)

    # The model the executor was built on: the first available of the tool's
    # models, not necessarily the first one listed.
    executor_model_key = getattr(executor, "_grid_model_key", None)
    if isinstance(executor_model_key, str) and executor_model_key:
        resolved_model_key = executor_model_key

    status = outcome.stopped or "completed"
    _keep_executor(factory, active_context_id, handle, {**record, "status": status})

    # The tools the executor really had: a name the factory could not resolve
    # (a typo, a tool of an MCP server that did not start) is reported, so the
    # caller does not brief the next agent to use it. The task is not echoed:
    # the caller wrote it, and repeating it only fills the caller's context.
    reported_missing = getattr(executor, "_grid_missing_tools", None)
    missing_tools = list(reported_missing) if isinstance(reported_missing, (list, tuple)) else []
    result = {
        # The executor's own id: passed back as context_id, it continues this agent.
        "context_id": handle,
        "model_key": resolved_model_key,
        "executor_tools": [
            name for name in (coerced_executor_tools or []) if name not in missing_tools
        ],
        "final": _extract_text(outcome.text),
        "pipeline_id": pipeline_id,
        # What the run took, for planning the next ones.
        "status": status,
        "seconds": round(time.monotonic() - started, 1),
        "model_calls": outcome.model_calls,
        "tokens": {"input": outcome.input_tokens, "output": outcome.output_tokens},
    }
    if tier:
        result["tier"] = tier
    if previous:
        result["continued"] = True
    if compacted:
        result["summarized_history"] = compacted
    if run_timeout is not None:
        result["timeout_seconds"] = run_timeout
    if status != "completed":
        result["resume"] = (
            f'orchestrate(context_id="{handle}", task="<what to do next>") continues this agent '
            "where it stopped, with what it already did; do not start the task over."
        )
    if notes:
        result["notes"] = notes
    if tally.calls:
        # Only a metered client sees what calls cost (core.model_access).
        result["cost_usd"] = float(Decimal(tally.micro) / MICRO)
        if tally.subscription_calls:
            result["cost_note"] = (
                f"{tally.subscription_calls} of {tally.calls} calls ran on a subscription: "
                "priced at API rates, not billed"
            )
    if missing_tools:
        result["missing_tools"] = missing_tools
        result["missing_tools_note"] = (
            "The executor did not have these tools: they are not tool keys of this "
            "system or their server is unavailable. Do not ask agents to use them."
        )
    result_json = json.dumps(result, ensure_ascii=False, indent=2)
    logger.info(
        f"orchestrate: COMPLETE  | model={resolved_model_key} | status={status} | "
        f"executor={handle} | output_len={len(result.get('final', ''))}"
    )
    verbose_logger.debug(
        f"\n{'='*80}\nORCHESTRATE COMPLETE\n{'='*80}\n"
        f"Model: {resolved_model_key}\n"
        f"Result (first 3000 chars):\n{result_json[:3000]}\n{'='*80}\n"
    )
    return result_json


ORCHESTRATOR_TOOLS = {
    "orchestrate": orchestrate,
}


# Where these tools act (utils.tool_isolation): it starts agents, whose own tools are confined in turn
from utils.tool_isolation import WORKSPACE as _WORKSPACE  # noqa: E402

TOOL_ISOLATION = {
    "orchestrate": _WORKSPACE,
}

# What these tools do (utils.tool_effects): the agents it starts are judged call by call
from utils import tool_effects as _effects  # noqa: E402

TOOL_EFFECTS = {
    "orchestrate": _effects.DELEGATE,
}
