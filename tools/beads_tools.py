"""
Beads Tools - Integration with the Beads (bd) issue tracker.

This module provides tools for Grid agents to interact with Beads,
a git-backed, dependency-aware graph issue tracker.
"""

import asyncio
import logging
import subprocess
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional
from agents import function_tool, RunContextWrapper
from pydantic import BaseModel, ConfigDict, Field
from utils.path_utils import (
    CONTAINER_WORKDIR,
    container_path,
    resolve_agent_path,
    sanitize_text_for_agent_from_ctx,
)
from utils.tool_requirements import Requires

from core import tracker_gateway, tracker_lifecycle, tracker_operations

logger = logging.getLogger(__name__)

#: bd commands whose answer the tools read as JSON.
_JSON_COMMANDS = ("ready", "create", "show", "update", "list", "search", "close",
                  "agent", "mol", "wisp", "pour")


def _factory_of(context: Any) -> Any:
    """The factory of the run, else the one bound to this task.

    A caller that built its context without the factory (a dynamic agent
    started from a bare init_tools context) must still resolve paths and the
    container through the running factory: otherwise the same tool reaches a
    different bd on the host.
    """
    factory = getattr(getattr(context, "context", None), "factory", None)
    if factory is None:
        from utils.path_utils import get_current_factory

        factory = get_current_factory()
    return factory

def _get_isolation_image(context: Any) -> str:
    """
    Best-effort resolve Docker image to run bd in.
    Priority:
    - config.yaml isolation.image (if available via AgentFactory in context)
    - env GRID_ISOLATION_IMAGE / GRID_AGENT_IMAGE
    - default "grid-agent:latest"
    """
    try:
        factory = None
        raw = getattr(context, "context", None)
        factory = getattr(raw, "factory", None) if raw else None
        cfg = getattr(factory, "config", None) if factory else None
        isolation_cfg = getattr(getattr(cfg, "config", None), "isolation", None) if cfg else None
        if isolation_cfg:
            img = getattr(isolation_cfg, "image", None) if not isinstance(isolation_cfg, dict) else isolation_cfg.get("image")
            if img:
                return str(img)
    except Exception:
        pass
    return os.environ.get("GRID_ISOLATION_IMAGE") or os.environ.get("GRID_AGENT_IMAGE") or "grid-agent:latest"

def _get_container_id(context: Any) -> Optional[str]:
    """The container the run's commands go to: the run's own, else its factory's.

    Every agent of a factory must reach the same tracker. A run context that
    lacks the container (an older caller) would otherwise send bd to the host,
    where a different bd build opens the same mounted database.
    """
    container_id = getattr(getattr(context, "context", None), "container_id", None)
    if container_id:
        return container_id
    return getattr(_factory_of(context), "container_id", None)

#: A bead id as bd mints them (``grid-a1b``, a child ``grid-a1b.1``). It never
#: starts with ``-``, so bd cannot read an id as a flag.
BEAD_ID = re.compile(r"^\w[\w.-]{0,63}$")


def bd_flag(name: str, value: Any) -> str:
    """``--name=value`` in one argument: a value starting with ``-`` stays a value."""
    return f"--{name}={value}"


def _bad_ids(*bead_ids: Any) -> Optional[str]:
    """The tool's error for the first argument that is not a bead id; None when all are."""
    for bead_id in bead_ids:
        if not isinstance(bead_id, str) or not BEAD_ID.match(bead_id):
            return _error_text("tracker_args", f"not a bead id: {bead_id!r}")
    return None

def _map_path_to_container(path: Optional[str], context: Any) -> str:
    """The container path of a host path :func:`_resolve_directory` returned.

    The factory comes from the run, else from the task's bound one - the same
    fallback :func:`_get_container_id` uses. Without it a context that lost
    its factory cannot map the path its command must run in.
    """
    return container_path(path, _factory_of(context))


def _find_bd(container_id: Optional[str] = None) -> str:
    """
    Resolve path to bd CLI. In container use fixed path; on host use
    BEADS_BD_PATH, ~/.local/bin/bd, or bd from PATH (works on Windows when bd is in PATH).
    """
    if container_id:
        return "/usr/local/bin/bd"
    path = os.environ.get("BEADS_BD_PATH")
    if path and os.path.isfile(path):
        return path
    home_bd = Path.home() / ".local" / "bin" / "bd"
    if home_bd.exists():
        return str(home_bd)
    return shutil.which("bd") or "bd"

def _with_json(args: List[str]) -> List[str]:
    """The command with ``--json`` when bd supports it and the caller did not ask."""
    argv = list(args)
    if "--json" not in argv and argv and argv[0] in _JSON_COMMANDS:
        argv.append("--json")
    return argv


def _bd_failure(problem: Any, context: Any) -> Dict[str, Any]:
    """The tools' error answer: a refusal or a reason, never a fake success.

    ``code`` is the stable name of the refusal (a gateway ``TrackerError``
    carries its own), so a caller can tell "the tracker is busy" from "this
    path is not a tracker" without reading the sentence.
    """
    return {
        "success": False,
        "output": "",
        "error": sanitize_text_for_agent_from_ctx(str(problem), context),
        "data": None,
        "exit_code": -1,
        "code": getattr(problem, "code", "tracker_error"),
        # Nothing ran: a refusal by the gateway is not an unknown outcome.
        "failed_before_run": True,
        "timed_out": False,
    }


def _bd_answer(result: "tracker_gateway.ExecutionResult", argv: List[str], context: Any) -> Dict[str, Any]:
    """The gateway's result in the shape every bd caller already reads."""
    data = None
    if "--json" in argv and result.output:
        try:
            data = json.loads(result.output)
        except json.JSONDecodeError:
            # Not valid JSON, might be a warning + JSON or just plain text
            pass
    return {
        "success": result.success,
        "output": sanitize_text_for_agent_from_ctx(result.output, context),
        "error": sanitize_text_for_agent_from_ctx(result.error, context),
        "data": data,
        "exit_code": result.exit_code,
        # A command stopped on timeout may have committed before it died: the
        # answer alone cannot say whether the write happened.
        "timed_out": result.timed_out,
        "missing_executable": result.missing_executable,
    }


# ============================================================================
# Durable operation protocol (core.tracker_operations)
# ============================================================================

def _error_text(code: str, message: str) -> str:
    """A tool's refusal: the same ❌ a caller already reads, plus its stable code."""
    return f"❌ Error [{code}]: {message}"


def _typed_error(
    code: str,
    message: str,
    *,
    committed: Optional[bool] = False,
    retryable: bool = False,
    **fields: Any,
) -> str:
    """A write's refusal as a payload a reader can decide on without parsing text.

    ``isError`` and ``code`` are the contract (``committed`` says whether the
    tracker may hold the write); the human sentence rides along as ``error``.
    A caller that only wants the text still gets it; a caller that must not
    guess reads the flag instead of matching an emoji.
    """
    payload = {
        "isError": True,
        "code": code,
        "error": message,
        "committed": committed,
        "retryable": retryable,
        **fields,
    }
    return json.dumps(payload, ensure_ascii=False)


def _failure_text(res: Dict[str, Any]) -> str:
    """What to say when bd did not succeed: its own words, or the honest gap."""
    reason = (res.get("error") or res.get("output") or "").strip()
    if reason:
        return reason
    if res.get("timed_out"):
        return "bd was stopped on timeout without answering"
    if res.get("missing_executable"):
        return "bd could not be started"
    return "bd did not answer"


def _inconclusive(res: Dict[str, Any]) -> bool:
    """Whether the write may have reached the tracker before the answer was lost.

    Three cases are *not* unknown: bd succeeded; the gateway refused before
    running anything (a busy database, a scope outside the tracker); or bd
    answered with an exit code of its own and some text. A timeout, a killed
    process and an empty answer are unknown, and saying otherwise would be a
    guess about a database nobody read.
    """
    if res.get("success"):
        return False
    if res.get("failed_before_run"):
        return False
    if res.get("timed_out") or res.get("missing_executable"):
        return True
    return not (res.get("error") or res.get("output") or "").strip()


def _actor_executor(context: Any) -> tuple:
    """Who the write acts for and which run asked, from the host's own context.

    Neither comes from a tool argument: the model may not name the actor of a
    tracker write, and a path it chooses is not an identity. The user is the
    run's, the executor is the run/agent/container the host put in the context.
    """
    raw = getattr(context, "context", None)
    factory = _factory_of(context)
    user = getattr(raw, "user_id", None) if raw is not None else None
    if not user:
        user = getattr(factory, "user_id", None)
    actor = f"grid:{user}" if user else "grid"

    parts = []
    for name in ("agent_id", "context_id"):
        value = getattr(raw, name, None) if raw is not None else None
        if value:
            parts.append(str(value))
    container = (getattr(raw, "container_id", None) if raw is not None else None) or getattr(
        factory, "container_id", None
    )
    if container:
        parts.append(f"container:{str(container)[:12]}")
    return actor, "|".join(parts) or None


def _bd_env(context: Any) -> Optional[Dict[str, str]]:
    """The environment bd runs with: the actor it records on what it writes."""
    actor, _ = _actor_executor(context)
    return {"BEADS_ACTOR": actor} if actor else None


def _run_id(context: Any) -> Optional[str]:
    """The run that makes this write, when the host gave it its own identity.

    It is what the task journal binds a claim to (core.tracker_lifecycle):
    a concrete executor run, never the user - one person may run several.
    """
    raw = getattr(context, "context", None)
    value = getattr(raw, "run_id", None) if raw is not None else None
    return str(value) if value else None


def _raw_context(context: Any) -> Any:
    return getattr(context, "context", None)


def _refusal(code: str, message: str, *, retryable: bool = False) -> Dict[str, Any]:
    """A write the host refused before bd ran: no tracker change happened."""
    return {
        "success": False,
        "failed_before_run": True,
        "output": "",
        "error": message,
        "code": code,
        "retryable": retryable,
    }


async def _reserve_claim(
    context: Any, directory: str, bead_id: str, run_id: Optional[str], actor: str
) -> Optional[Dict[str, Any]]:
    """Bind the claim to this run *before* bd sees it.

    Two runs racing for one task settle here, not in the tracker: the loser is
    refused with ``tracker_claimed_elsewhere`` and makes no write. A stale
    binding (its lease expired) is taken over and the takeover is recorded.
    Without a run identity the write is not bound: the journal has no owner to
    name, and pretending the user owns it is what a stale run could abuse.
    """
    if not run_id:
        return None
    raw = _raw_context(context)
    try:
        await asyncio.to_thread(
            tracker_lifecycle.bind,
            directory,
            bead_id,
            run_id=run_id,
            actor=actor,
            agent=getattr(raw, "agent_id", None),
            context_id=getattr(raw, "context_id", None),
            parent_run_id=getattr(raw, "parent_run_id", None),
            note="claim reserved before the tracker write",
        )
    except tracker_lifecycle.LifecycleError as exc:
        return _refusal(exc.code, f"{exc}. Nothing was written.", retryable=exc.retryable)
    tracker_lifecycle.remember(_raw_context(context), directory, bead_id)
    return None


async def _release_unconfirmed(
    context: Any, directory: str, bead_id: str, run_id: Optional[str], res: Dict[str, Any]
) -> None:
    """After a claim bd did not confirm: forget the reservation or keep it honestly.

    A definite refusal (bd ran, said no) leaves no claim, so the reservation is
    removed. An inconclusive answer may have been a claim whose reply was lost:
    then the binding stays ours, with a checkpoint that says so.
    """
    if not run_id:
        return
    try:
        if _inconclusive(res):
            await asyncio.to_thread(
                tracker_lifecycle.checkpoint,
                directory,
                bead_id,
                run_id,
                "claim_unknown",
                _failure_text(res),
            )
            tracker_lifecycle.remember(_raw_context(context), directory, bead_id)
            return
        await asyncio.to_thread(
            tracker_lifecycle.release, directory, bead_id, run_id,
            reason=_failure_text(res),
        )
    except Exception:  # noqa: BLE001 - the tracker answer decides the write
        logger.warning("the claim reservation of %s could not be settled", bead_id, exc_info=True)


async def _note_confirmed_claim(context: Any, directory: str, bead_id: str, answer: str) -> None:
    """The claim reached the tracker: remember it on the run that its finally settles."""
    run_id = _run_id(context)
    if not run_id:
        return
    try:
        data = json.loads(answer) if isinstance(answer, str) and answer.lstrip().startswith(("{", "[")) else None
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("isError"):
        return
    tracker_lifecycle.remember(_raw_context(context), directory, bead_id)
    try:
        await asyncio.to_thread(
            tracker_lifecycle.checkpoint, directory, bead_id, run_id, "claimed",
            "the tracker confirmed the claim",
        )
    except Exception:  # noqa: BLE001 - a checkpoint is not the claim
        logger.warning("the claim of %s could not be checkpointed", bead_id, exc_info=True)


def _answer_payload(answer: Any) -> Optional[Dict[str, Any]]:
    """A tool's JSON answer as a payload, or None when it is not one."""
    if isinstance(answer, dict):
        return answer
    if isinstance(answer, str) and answer.lstrip().startswith("{"):
        try:
            data = json.loads(answer)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _claim_of_run(answer: Any, run_id: Optional[str]) -> bool:
    """Whether an answer says *this* run's claim is the one the tracker holds.

    A claim's ``grid_run`` metadata is written in the same bd transaction as
    the claim, so it is the only reading that tells two runs of one user
    apart. A repeat that finds another run's claim in the answer returns the
    result but keeps the ownership where it is.
    """
    if not run_id:
        return False
    data = _answer_payload(answer)
    return bool(data) and not data.get("isError") and data.get("run_id") == run_id


async def _guard_owner(
    context: Any, directory: str, bead_id: str
) -> Optional[Dict[str, Any]]:
    """Refuse a write on a task a *live* other run holds (core.tracker_lifecycle).

    bd guards the tracker's own assignee (``grid:<user>``): it refuses a close
    by another actor and will not let a second actor claim a held task. Two runs
    of one user share that assignee, so this journal is what tells them apart:
    a task owned by a live run cannot be updated or closed by another one, and
    the refusal says so instead of overwriting the owner's work. A stale
    binding (its lease expired) does not block: the next run may carry the task
    on, and a confirmed close records the takeover.
    """
    run_id = _run_id(context)
    try:
        view = await asyncio.to_thread(tracker_lifecycle.lookup, directory, bead_id)
    except tracker_lifecycle.LifecycleError as exc:
        return _refusal(exc.code, f"{exc} Nothing was written.")
    if view is None or view["owner_run_id"] == run_id:
        return None
    if not tracker_lifecycle.is_live(view):
        if not run_id:
            # A run without an identity cannot own the task it is about to
            # change, so it has no right to take it from the run whose lease
            # ran out: only a concrete run can be recorded as the new owner.
            return None
        # Take the task over *before* bd is awaited, in the journal's own
        # transaction: bind is a compare-and-set on the owner, so two runs
        # racing for a stale task settle here - the loser is refused, not
        # both-written. This is the fencing the previous guard lacked: the
        # stale predecessor can no longer finish the task, and this run owns
        # what it is about to write.
        actor, _ = _actor_executor(context)
        raw = _raw_context(context)
        try:
            await asyncio.to_thread(
                tracker_lifecycle.bind,
                directory,
                bead_id,
                run_id=run_id,
                actor=actor,
                agent=getattr(raw, "agent_id", None),
                context_id=getattr(raw, "context_id", None),
                parent_run_id=getattr(raw, "parent_run_id", None),
                note=f"taken over from {view['owner_run_id']} after its lease expired",
            )
        except tracker_lifecycle.LifecycleError as exc:
            return _refusal(exc.code, f"{exc}. Nothing was written.", retryable=exc.retryable)
        tracker_lifecycle.remember(raw, directory, bead_id)
        return None
    return _refusal(
        "tracker_not_owner",
        f"{bead_id} is held by another run ({view['owner_run_id']}, "
        f"state {view['state']} until {view['lease_expires_at']:.0f}); this run "
        f"({run_id or 'unbound'}) must not change it. Continue that run, or "
        f"take the task over once its lease expires (which happens on its own "
        f"when its run is gone). Nothing was written.",
        retryable=True,
    )


async def _settle_after_write(
    context: Any, directory: str, bead_id: str, outcome: str, detail: str = ""
) -> None:
    """Finish the binding after a confirmed write that ends the task's work."""
    run_id = _run_id(context)
    if not run_id:
        return
    try:
        await asyncio.to_thread(
            tracker_lifecycle.finish, directory, bead_id, run_id,
            outcome=outcome, detail=detail, steal_if_stale=True,
        )
    except tracker_lifecycle.LifecycleError as exc:
        # A live owner that is not this run: the write itself was confirmed by
        # bd (which guards its own assignee), so the binding is reported, not hidden.
        logger.warning("the binding of %s was not settled: %s", bead_id, exc)
    except Exception:  # noqa: BLE001 - the tracker already holds the write
        logger.warning("the binding of %s could not be settled", bead_id, exc_info=True)


async def _begin_operation(
    context: Any,
    directory: str,
    kind: str,
    request_id: Optional[str],
    detail: Optional[Dict[str, Any]] = None,
) -> "tracker_operations.Operation":
    """Journal the start of a write before bd is asked to make it."""
    actor, executor = _actor_executor(context)
    return await asyncio.to_thread(
        tracker_operations.begin,
        directory,
        kind,
        request_id=request_id,
        actor=actor,
        executor=executor,
        detail=detail or {},
    )


async def _conclude(operation: "tracker_operations.Operation", state: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Record how a write ended; a journal that cannot be written is reported, not raised."""
    writer = {
        tracker_operations.COMMITTED: tracker_operations.commit,
        tracker_operations.FAILED: tracker_operations.fail,
        tracker_operations.UNKNOWN: tracker_operations.unknown,
    }[state]
    try:
        return await asyncio.to_thread(writer, operation, **kwargs)
    except Exception:  # noqa: BLE001 - the tracker already holds the truth
        logger.warning("The tracker operation %s could not be journaled", operation.operation_id, exc_info=True)
        return None


def _settled_answer(operation: "tracker_operations.Operation", *, dump: bool) -> Optional[str]:
    """The answer a finished operation already gave; None when it has none.

    A caller that repeats a request sees what the first call concluded instead
    of a second write: the same ids for a plan, the same refusal for one bd
    rejected, and a plain "this is still running / outcome unknown" otherwise.
    """
    record = operation.record or {}
    if operation.state == tracker_operations.COMMITTED:
        payload = dict(record.get("data") or {})
        payload.update({"operation_id": operation.operation_id, "committed": True, "duplicate": True})
        return json.dumps(payload, ensure_ascii=False, indent=2) if dump else str(
            (record.get("data") or {}).get("output") or json.dumps(payload, ensure_ascii=False)
        )
    if operation.state == tracker_operations.FAILED:
        return json.dumps(
            {
                "isError": True,
                "error": record.get("error") or "the write was refused",
                "code": record.get("code") or "tracker_rejected",
                "committed": False,
                "operation_id": operation.operation_id,
                "duplicate": True,
                "retryable": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    in_flight = operation.state == tracker_operations.STARTED
    return json.dumps(
        {
            "isError": True,
            "error": (
                "This exact request is still being applied by another call."
                if in_flight
                else "An earlier attempt with this request ended without a recorded outcome."
            ) + " Nothing was repeated.",
            "code": "tracker_operation_in_flight" if in_flight else "tracker_operation_unknown",
            "committed": None,
            "operation_id": operation.operation_id,
            "duplicate": True,
            "retryable": bool(in_flight),
            "hint": "Call again with the same request_id to reconcile against the tracker; do not create it by hand.",
        },
        ensure_ascii=False,
        indent=2,
    )


async def _durable_write(
    context: Any,
    directory: str,
    container_id: Optional[str],
    *,
    kind: str,
    request_id: Optional[str],
    detail: Optional[Dict[str, Any]],
    run: Any,
    success_text: Any,
    reconcile: Any = None,
    reconciled_text: Any = None,
    journal_data: Any = None,
    outcome: Optional[Dict[str, Any]] = None,
) -> str:
    """One write under the operation protocol, from the first record to the answer.

    ``run`` performs the bd call, ``success_text`` renders its success answer,
    ``reconcile`` looks the tracker up for the marker this write stamps and
    returns proof (or None when it cannot tell), ``reconciled_text`` renders
    that proof. Both ``run`` and ``reconcile`` get the operation, because the
    marker they write and look up is the operation's own id. The journal is
    concluded in every path, including the one where the answer was lost and
    only the marker can tell.

    A repeat of an operation the journal holds as ``unknown`` is not a dead
    end: its marker is looked up once more, and a proven commit upgrades the
    record. A repeat of a ``committed`` or ``failed`` operation answers from
    the journal and never touches bd again.

    ``outcome``, when given, is filled with what this *call* did: whether it
    performed the write (``performed``) or only replayed/reconciled one an
    earlier call made. The distinction is what keeps a repeated request from
    granting its new caller the ownership of the original write.
    """
    try:
        operation = await _begin_operation(context, directory, kind, request_id, detail)
    except tracker_operations.OperationError as exc:
        return _typed_error(exc.code, f"{exc} Nothing was written.", retryable=exc.retryable)
    except Exception as exc:  # noqa: BLE001 - a journal that cannot be written stops the write
        logger.warning("The tracker operation could not be started", exc_info=True)
        return _typed_error(
            "tracker_operation", f"The operation could not be recorded ({exc}). Nothing was written."
        )

    if outcome is not None:
        outcome.update(
            {
                "operation_id": operation.operation_id,
                "request_id": operation.request_id,
                "state": operation.state,
                "duplicate": not operation.is_new,
                "performed": False,
                "reconciled": False,
            }
        )

    if not operation.is_new:
        settled = _settled_answer(operation, dump=False)
        if operation.state in (tracker_operations.COMMITTED, tracker_operations.FAILED):
            return settled
        if reconcile is not None:
            proof = await reconcile(operation)
            if proof:
                await _conclude(operation, tracker_operations.COMMITTED, data=proof, code="reconciled")
                if outcome is not None:
                    outcome.update({"state": tracker_operations.COMMITTED, "reconciled": True})
                return reconciled_text(proof) if reconciled_text else settled
        return settled

    if outcome is not None:
        outcome["performed"] = True
    res = await run(operation)
    if res.get("success"):
        data = journal_data(res) if journal_data else {"output": res.get("output") or ""}
        await _conclude(operation, tracker_operations.COMMITTED, data=data, code="ok")
        if outcome is not None:
            outcome["state"] = tracker_operations.COMMITTED
        return success_text(res, operation)

    reason = _failure_text(res)
    if reconcile is not None:
        proof = await reconcile(operation)
        if proof:
            await _conclude(operation, tracker_operations.COMMITTED, data=proof, code="reconciled")
            if outcome is not None:
                outcome.update({"state": tracker_operations.COMMITTED, "reconciled": True})
            if reconciled_text:
                return reconciled_text(proof)
    if _inconclusive(res):
        await _conclude(
            operation,
            tracker_operations.UNKNOWN,
            error=reason,
            code="tracker_unknown",
        )
        if outcome is not None:
            outcome["state"] = tracker_operations.UNKNOWN
        return _typed_error(
            "tracker_unknown",
            f"{reason}. The write may or may not have been applied (operation {operation.operation_id}); "
            f"nothing was repeated. Check the tracker before repeating this call.",
            committed=None,
            operation_id=operation.operation_id,
        )
    await _conclude(operation, tracker_operations.FAILED, error=reason, code=res.get("code") or "tracker_failed")
    if outcome is not None:
        outcome["state"] = tracker_operations.FAILED
    return _typed_error(
        res.get("code") or "tracker_failed",
        f"{reason} (operation {operation.operation_id})",
        operation_id=operation.operation_id,
    )


def _run_bd_via_docker_run(args: List[str], cwd: str, context: Any) -> Dict[str, Any]:
    """
    Run bd inside a short-lived container (no Docker SDK required).
    Mounts the provided cwd at the container's workspace.

    The container is named by the gateway, so a timeout stops that one
    container and nothing else; when this server runs as root the container
    writes as the project's owner, so the tracker stays the user's. The
    physical database, not the container, is the serialization key.
    """
    image = _get_isolation_image(context)
    host_cwd = os.path.abspath(cwd) if cwd else os.path.abspath(".")
    argv = _with_json(args)
    try:
        logger.debug("Running beads via docker image %s: %s", image, argv)
        result = tracker_gateway.execute(
            argv,
            directory=host_cwd,
            backend=tracker_gateway.Backend.docker(image),
            container_workdir=CONTAINER_WORKDIR,
            timeout=BD_TIMEOUT_SECONDS,
        )
    except tracker_gateway.TrackerError as exc:
        return _bd_failure(exc, context)
    except Exception as exc:  # the tools answer, they do not raise
        logger.warning("Running bd in a container failed", exc_info=True)
        return _bd_failure(exc, context)

    # Friendly error when isolation image isn't built locally.
    if not result.success:
        err_low = (result.error or "").lower()
        if ("unable to find image" in err_low) or ("no such image" in err_low) or ("pull access denied" in err_low):
            build_hint = (
                f"Docker image '{image}' not found locally.\n"
                f"Build it in repo root:\n"
                f"  docker build -t {image} -f Dockerfile .\n"
                f"Or set GRID_ISOLATION_IMAGE to an existing image name."
            )
            result.error = build_hint + (f"\n\nDocker error: {result.error}" if result.error else "")
    return _bd_answer(result, argv, context)


#: How long one bd command may run.
BD_TIMEOUT_SECONDS = tracker_gateway.DEFAULT_TIMEOUT_SECONDS


async def _bd(args: List[str], **kwargs: Any) -> Dict[str, Any]:
    """:func:`_run_bd_command` in a worker thread: the tools are coroutines on
    the server's event loop, which must not wait for a process."""
    return await asyncio.to_thread(_run_bd_command, args, **kwargs)


async def run_bd(args: List[str], cwd: Optional[str] = None) -> Dict[str, Any]:
    """One bd command on the host, for callers outside an agent run.

    The web UI's tracker page (web_chat.tasks_api) uses this: the same command
    shaping, JSON parsing and physical-database gate as the agent tools,
    without a run's context or container - the host's bd against the
    directory it is pointed at.
    """
    return await asyncio.to_thread(_run_bd_command, args, cwd=cwd)


def _run_bd_command(
    args: List[str],
    cwd: Optional[str] = None,
    container_id: Optional[str] = None,
    context: Any = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Run a 'bd' command through the tracker gateway and return the result.
    Always adds --json for machine-readable output when possible.

    This is the one place bd is started, for the agent tools and the web UI
    alike: the gateway serializes every command on the physical database,
    checks the backend/version and stops a timed-out command by id.
    """
    argv = _with_json(args)
    directory = cwd or os.path.abspath(".")

    workdir = None
    if container_id:
        try:
            workdir = _map_path_to_container(directory, context)
        except ValueError as exc:
            return _bd_failure(f"The tracker is outside this run's workspace: {exc}", context)
        backend = tracker_gateway.Backend.container(container_id)
    else:
        backend = tracker_gateway.Backend.host(_find_bd())

    try:
        logger.debug("Running beads command: %s in %s (%s)", argv, directory, backend.label)
        result = tracker_gateway.execute(
            argv,
            directory=directory,
            backend=backend,
            container_workdir=workdir,
            env=env,
            timeout=BD_TIMEOUT_SECONDS,
        )
    except tracker_gateway.TrackerError as exc:
        return _bd_failure(exc, context)
    except Exception as exc:  # the tools answer, they do not raise
        logger.warning("The tracker gateway failed for %s", argv[0] if argv else "?", exc_info=True)
        return _bd_failure(exc, context)

    if result.missing_executable and not container_id and _docker_fallback_enabled():
        # bd is not installed on the host: run it in a short-lived container
        # mounted at the same directory, so the same physical database stays
        # the serialization key.
        try:
            return _run_bd_via_docker_run(args, cwd=directory, context=context)
        except Exception as fallback_error:
            return _bd_failure(
                f"bd is not installed ({result.error}) and running it in Docker failed: {fallback_error}",
                context,
            )
    return _bd_answer(result, argv, context)


def _resolve_directory(context: RunContextWrapper, directory: str) -> str:
    """Host path of the project directory, relative to the agent working directory.

    Same rule as the file and git tools: the agent's paths are relative to its
    working directory and never escape it (ValueError). In a container the host
    path is mapped to the container path when the command runs.

    A run bound to a tracker (a chat space's project) keeps that tracker. The
    default stays the bound one, as it always did; a path the caller spells
    out is resolved in the run's workspace as before, but it may not name a
    *different* tracker: a path that is itself a tracker (it has a ``.beads``
    of its own) is refused rather than quietly opening another database, and a
    subdirectory of the bound tracker is that same tracker, so bd runs at its
    root.
    """
    factory = _factory_of(context)
    tracker = getattr(factory, "tracker_directory", None)
    if directory in ("", ".", None):
        # A chat with a space of its own keeps its plan in the tracker it shares
        # with the user's other chats (web_chat.space), not in a new one per chat.
        if tracker:
            return str(tracker)
        return resolve_agent_path(".", factory)

    resolved = resolve_agent_path(directory, factory)
    if not tracker:
        return resolved
    pinned = os.path.realpath(str(tracker))
    here = os.path.realpath(resolved)
    if here == os.path.realpath(resolve_agent_path(".", factory)):
        # A spelling that lands back on the run's working directory ("other/..")
        # is the default path, and the default is the bound tracker.
        return str(tracker)
    if here == pinned or _inside(here, pinned):
        # A subdirectory of the bound tracker is the same tracker: bd runs at
        # its root, where the database is.
        return pinned
    if _inside(pinned, here):
        # A parent of the bound tracker is not the bound tracker either; bd
        # would find this run's tracker by walking up only by accident.
        return resolved
    if (Path(here) / ".beads").exists():
        raise ValueError(
            f"this run's tracker is {pinned}, and {here} is a tracker of its own; "
            f"a bead path may not switch the run to another database. Use directory='.'."
        )
    return resolved


def _inside(path: str, root: str) -> bool:
    """Whether *path* is under *root* (the root itself is not "inside")."""
    try:
        return os.path.commonpath([path, root]) == root and path != root
    except ValueError:  # different drives on Windows
        return False


# ============================================================================
# Shared append-only logs (for games / chat-like streams)
# ============================================================================

_LOG_ROOT_DIRNAME = "shared_log"
_LOG_CURSOR_FILENAME = "cursors.json"


def _safe_filename(name: str) -> str:
    """Conservatively map an arbitrary name to a safe filename stem."""
    if not name:
        return "default"
    out = []
    for ch in str(name):
        if ch.isalnum() or ch in ("-", "_", ".", "@"):
            out.append(ch)
        else:
            out.append("_")
    stem = "".join(out).strip("._")
    return stem or "default"


def _log_root(directory: str) -> Path:
    """Return path to the shared log root inside .beads/."""
    base = Path(directory)
    beads_dir = base / ".beads"
    beads_dir.mkdir(parents=True, exist_ok=True)
    root = beads_dir / _LOG_ROOT_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _log_path(directory: str, channel: str) -> Path:
    """Path to the append-only JSONL log file for a channel."""
    root = _log_root(directory)
    return root / f"{_safe_filename(channel)}.jsonl"


def _cursor_path(directory: str) -> Path:
    """Path to the cursor storage JSON file."""
    root = _log_root(directory)
    return root / _LOG_CURSOR_FILENAME


def _load_cursors(path: Path) -> Dict[str, Dict[str, int]]:
    """Load cursor map: {channel: {reader: byte_offset}}."""
    try:
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            # normalize to int offsets
            out: Dict[str, Dict[str, int]] = {}
            for ch, readers in data.items():
                if not isinstance(readers, dict):
                    continue
                out[str(ch)] = {}
                for r, off in readers.items():
                    try:
                        out[str(ch)][str(r)] = int(off)
                    except (TypeError, ValueError):
                        continue
            return out
    except (OSError, ValueError):
        logger.warning("Log cursors in %s are unreadable; reading from the start", path, exc_info=True)
    return {}


def _save_cursors(path: Path, cursors: Dict[str, Dict[str, int]]) -> None:
    path.write_text(json.dumps(cursors, ensure_ascii=False, indent=2), encoding="utf-8")


@function_tool
async def beads_log_append(
    context: RunContextWrapper,
    channel: str,
    author: str,
    text: str,
    directory: str = ".",
) -> str:
    """
    Append a single entry to a shared, append-only JSONL log (inside .beads/shared_log/).
    Intended for chat-like streams (e.g., game public log) to avoid rewriting previous text.

    Args:
        channel: Logical channel name (e.g., "GAME", "MAFIA", "public")
        author: Author identifier (e.g., "GM", "Agent_A")
        text: Message body (one entry)
        directory: Working directory (defaults to auto-resolved ".")
    """
    try:
        directory = _resolve_directory(context, directory)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    path = _log_path(directory, channel)

    entry = {
        "ts": time.time(),
        "author": str(author),
        "text": str(text),
    }

    try:
        # Ensure parent exists
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line)
            f.write("\n")
            f.flush()
        return json.dumps(
            {"success": True, "channel": channel, "bytes_appended": len(line) + 1},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@function_tool
async def beads_log_read(
    context: RunContextWrapper,
    channel: str,
    reader: str,
    max_bytes: int = 6000,
    from_start: bool = False,
    reset_to_end: bool = False,
    directory: str = ".",
) -> str:
    """
    Read ONLY new log data for (channel, reader) since the last read.
    Tracks per-reader cursor (byte offset) in .beads/shared_log/cursors.json.

    Args:
        channel: Logical channel name
        reader: Reader identifier (e.g., "GM", "Agent_A")
        max_bytes: Maximum bytes to return (soft cap; helps keep outputs bounded)
        from_start: If True, ignore stored cursor and read from beginning
        reset_to_end: If True, set cursor to end of file and return empty (useful to "mark as read")
        directory: Working directory (defaults to auto-resolved ".")
    """
    try:
        directory = _resolve_directory(context, directory)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    log_path = _log_path(directory, channel)
    cur_path = _cursor_path(directory)

    # Clamp max_bytes to keep tool output safe.
    try:
        max_bytes = int(max_bytes)
    except Exception:
        max_bytes = 6000
    if max_bytes < 256:
        max_bytes = 256
    if max_bytes > 20000:
        max_bytes = 20000

    if not log_path.exists():
        return json.dumps(
            {
                "success": True,
                "channel": channel,
                "reader": reader,
                "new_data": "",
                "truncated": False,
                "cursor_before": 0,
                "cursor_after": 0,
                "log_exists": False,
            },
            ensure_ascii=False,
        )

    cursors = _load_cursors(cur_path)
    ch_key = str(channel)
    rd_key = str(reader)
    stored = cursors.get(ch_key, {}).get(rd_key, 0)

    cursor_before = 0 if from_start else int(stored or 0)

    try:
        file_size = log_path.stat().st_size
    except Exception:
        file_size = None

    if reset_to_end:
        end_pos = file_size if isinstance(file_size, int) else 0
        cursors.setdefault(ch_key, {})[rd_key] = int(end_pos)
        try:
            _save_cursors(cur_path, cursors)
        except OSError:
            # The next read repeats what this one returned.
            logger.warning("Could not save log cursor %s", cur_path, exc_info=True)
        return json.dumps(
            {
                "success": True,
                "channel": channel,
                "reader": reader,
                "new_data": "",
                "truncated": False,
                "cursor_before": cursor_before,
                "cursor_after": int(end_pos),
                "reset_to_end": True,
            },
            ensure_ascii=False,
        )

    try:
        with log_path.open("rb") as f:
            # Guard against cursor beyond EOF (e.g., log rotated/cleared)
            if isinstance(file_size, int) and cursor_before > file_size:
                cursor_before = 0
            f.seek(cursor_before)
            data = f.read(max_bytes + 1)  # +1 to detect truncation
            truncated = len(data) > max_bytes
            if truncated:
                data = data[:max_bytes]
            cursor_after = cursor_before + len(data)

        new_text = data.decode("utf-8", errors="replace")

        # Persist cursor.
        cursors.setdefault(ch_key, {})[rd_key] = int(cursor_after)
        try:
            _save_cursors(cur_path, cursors)
        except OSError:
            # The next read repeats what this one returned.
            logger.warning("Could not save log cursor %s", cur_path, exc_info=True)

        return json.dumps(
            {
                "success": True,
                "channel": channel,
                "reader": reader,
                "new_data": new_text,
                "truncated": bool(truncated),
                "cursor_before": int(cursor_before),
                "cursor_after": int(cursor_after),
                "log_exists": True,
                "log_size": int(file_size) if isinstance(file_size, int) else None,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


def _init_args(directory: str) -> List[str]:
    """`bd init` that adds the tracker and nothing else to the user's project.

    By default bd also writes AGENTS.md, CLAUDE.md and Claude/Codex/Cursor
    settings and hooks, installs git hooks and stages its files in the user's
    git index; the agents reach the tracker through these tools and need none
    of that. --setup-exclude keeps .beads/ out of git through .git/info/exclude.
    The issue prefix is the project's own name: in a container bd would take
    it from the mount point ("workspace").
    """
    args = ["init", "--init-if-missing", "--non-interactive",
            "--skip-agents", "--skip-hooks", "--setup-exclude"]
    name = Path(directory).name
    if name:
        args += ["--prefix", name]  # bd normalizes it into a valid prefix
    return args


@function_tool
async def beads_init(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Initialize beads (bd) in the given directory. Idempotent: safe to call if already initialized.
    Creates .beads/ and its database so beads_ready, beads_create, etc. work in this directory.

    Args:
        directory: Path to the project directory (default: ".")
    """
    try:
        directory = _resolve_directory(context, directory)
    except ValueError as exc:
        return _error_text("tracker_scope", str(exc))
    container_id = _get_container_id(context)

    res = await _bd(_init_args(directory), cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        # "already a beads database" or similar is often success for idempotent init
        if "already" in (res.get("error") or "").lower() or "already" in (res.get("output") or "").lower():
            return "✅ Beads already initialized in this directory."
        return _error_text(res.get("code") or "tracker_failed", f"initializing the tracker: {_failure_text(res)}")
    return res["output"] or "✅ Beads initialized."


@function_tool
async def beads_ready(context: RunContextWrapper, directory: str = ".") -> str:
    """
    List beads tasks that are ready to be worked on (no open blockers).
    
    Args:
        directory: Path to the project directory (default: ".")
    """
    try:
        directory = _resolve_directory(context, directory)
    except ValueError as exc:
        return _error_text("tracker_scope", str(exc))
    container_id = _get_container_id(context)
    
    res = await _bd(["ready"], cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return _error_text(res.get("code") or "tracker_failed", f"ready: {_failure_text(res)}")
    return res["output"]

async def _reconcile_created(
    directory: str,
    operation_id: str,
    expected: Dict[str, Any],
    container_id: Optional[str],
    context: Any,
) -> Optional[Dict[str, Any]]:
    """The task this operation already created, found by its marker; None when none.

    ``None`` means "the tracker could not be read" only when bd failed to
    answer at all; an empty list is the tracker saying it holds no such task,
    which is returned as ``{}`` so the caller can tell "checked" from "unknown".

    The marker alone is not proof: any task carrying our reference could hold
    a different title, priority, type or description. What is adopted is only
    the task that is *this request's* result - the marker written by the same
    commit as those exact fields.
    """
    ref = tracker_operations.marker(operation_id)
    res = await _bd(
        ["list", "--external-ref", ref, "--json", "--limit", "0", "--all"],
        cwd=directory,
        container_id=container_id,
        context=context,
    )
    data = res.get("data")
    if not res.get("success") or not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict) or item.get("external_ref") != ref:
            continue
        if not isinstance(item.get("id"), str) or not _created_fields_match(item, expected):
            continue
        return {"id": item["id"], "title": item.get("title") or ""}
    return {}


def _as_text(value: Any) -> str:
    """A field as the tracker stores it: absent and empty are the same text."""
    return "" if value is None else str(value)


def _created_fields_match(item: Dict[str, Any], expected: Dict[str, Any]) -> bool:
    """Whether a marked task holds exactly what this create intended."""
    if _as_text(item.get("title")) != _as_text(expected.get("title")):
        return False
    if _as_text(item.get("description")) != _as_text(expected.get("description")):
        return False
    try:
        if int(item.get("priority")) != int(expected.get("priority")):
            return False
    except (TypeError, ValueError):
        return False
    if _as_text(item.get("issue_type")) != _as_text(expected.get("type")):
        return False
    if "acceptance" in expected:
        # A create that asked for acceptance is proven only by the same text:
        # a marker under a task with another brief is not this request's.
        if _as_text(item.get("acceptance_criteria")) != _as_text(expected.get("acceptance")):
            return False
    if "assignee" in expected and expected.get("assignee"):
        if _as_text(item.get("assignee")) != _as_text(expected.get("assignee")):
            return False
    if "depends_on" in expected:
        # The edges ride the same bd command, so they are part of the proof too.
        held = {dep["id"] for dep in _dependency_rows(item)}
        if held != set(expected.get("depends_on") or []):
            return False
    return True


def _dependency_rows(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The blocking edges a bd issue carries, by id (a relates_to link blocks nothing)."""
    rows: List[Dict[str, Any]] = []
    for dep in item.get("dependencies") or []:
        if not isinstance(dep, dict) or dep.get("type") not in (None, "blocks", "depends_on", "depends-on"):
            continue
        dep_id = dep.get("depends_on_id") or dep.get("id")
        if isinstance(dep_id, str):
            rows.append({"id": dep_id})
    return rows


async def _create_bead(
    context: RunContextWrapper,
    directory: str,
    container_id: Optional[str],
    args: List[str],
    request_id: str,
    detail: Optional[Dict[str, Any]] = None,
) -> str:
    """One bead, journaled like a plan: a marker proves a lost answer's commit."""
    title = args[-1]
    expected = dict(detail or {"title": title})
    expected.setdefault("title", title)
    try:
        operation = await _begin_operation(
            context, directory, "create", request_id, expected
        )
    except tracker_operations.OperationError as exc:
        return _typed_error(exc.code, f"{exc} Nothing was created.", retryable=exc.retryable)
    except Exception as exc:  # noqa: BLE001
        logger.warning("The create operation could not be started", exc_info=True)
        return _typed_error(
            "tracker_operation", f"The create could not be recorded ({exc}). Nothing was created."
        )

    if not operation.is_new:
        if operation.state in (tracker_operations.COMMITTED, tracker_operations.FAILED):
            return _settled_answer(operation, dump=True) or ""
        proof = await _reconcile_created(
            directory, operation.operation_id, expected, container_id, context
        )
        if proof:
            await _conclude(operation, tracker_operations.COMMITTED, data=proof, code="reconciled")
            return json.dumps({**proof, "operation_id": operation.operation_id, "committed": True, "duplicate": True}, ensure_ascii=False)
        return _settled_answer(operation, dump=True) or ""

    # args ends with ("--", title): the marker is a flag of bd's, so it goes
    # before the "--" that makes a title text.
    argv = list(args[:-2]) + ["--external-ref", tracker_operations.marker(operation.operation_id), "--", title]
    res = await _bd(argv, cwd=directory, container_id=container_id, context=context, env=_bd_env(context))

    data = res.get("data")
    if res.get("success") and isinstance(data, dict) and isinstance(data.get("id"), str):
        recorded = {"id": data["id"], "title": data.get("title") or title}
        await _conclude(operation, tracker_operations.COMMITTED, data=recorded, code="ok")
        # The tool's own answer stays bd's JSON, with the operation named so a
        # caller can repeat by it.
        answer = dict(data)
        answer["operation_id"] = operation.operation_id
        # A positive receipt is typed, and it comes from the journal record
        # that was just written, not from the fact that nobody objected.
        answer["committed"] = True
        return json.dumps(answer, ensure_ascii=False)

    proof = await _reconcile_created(
        directory, operation.operation_id, expected, container_id, context
    )
    if proof:
        await _conclude(operation, tracker_operations.COMMITTED, data=proof, code="reconciled")
        return json.dumps({**proof, "operation_id": operation.operation_id, "committed": True, "reconciled": True}, ensure_ascii=False)

    reason = _failure_text(res)
    if _inconclusive(res) or (proof is None and not res.get("failed_before_run")):
        await _conclude(operation, tracker_operations.UNKNOWN, error=reason, code="tracker_unknown")
        return _typed_error(
            "tracker_unknown",
            f"{reason}. The bead may or may not have been created (operation {operation.operation_id}); "
            f"nothing was repeated. Check the tracker before creating it again.",
            committed=None,
            operation_id=operation.operation_id,
        )
    await _conclude(operation, tracker_operations.FAILED, error=reason, code=res.get("code") or "tracker_failed")
    return _typed_error(
        res.get("code") or "tracker_failed",
        f"{reason}. Nothing was created (operation {operation.operation_id}).",
        operation_id=operation.operation_id,
    )


async def create_bead(
    context: RunContextWrapper, 
    title: str, 
    priority: int = 1, 
    type: str = "task",
    description: str = "",
    acceptance: str = "",
    assignee: str = "",
    depends_on: Optional[List[str]] = None,
    directory: str = ".",
    request_id: str = "",
) -> str:
    """
    Create a new bead (task, epic, message, etc.).
    
    Args:
        title: Short title of the bead
        priority: Priority (0=P0, 1=P1, 2=P2, 3=P3)
        type: Type of bead (task, epic, message, etc.)
        description: Detailed description
        acceptance: How to tell the task is done
        assignee: Who the task is for
        depends_on: Ids of existing tasks that must be done first
        directory: Path to the project directory
        request_id: Optional key of your own; repeating it answers with the
            first call's bead instead of creating a second one.
    """
    try:
        directory = _resolve_directory(context, directory)
    except ValueError as exc:
        return _typed_error("tracker_scope", str(exc))
    container_id = _get_container_id(context)
    try:
        request_id = tracker_operations.check_request_id(request_id)
    except tracker_operations.OperationError as exc:
        return _typed_error(exc.code, f"{exc} Nothing was created.")
    
    # Values ride inside their flags and the title follows "--": a title or a
    # description starting with "-" is text, never an option of bd's.
    args = ["create", "--json", bd_flag("priority", priority), bd_flag("type", type)]
    if description:
        args.append(bd_flag("description", description))
    if acceptance:
        args.append(bd_flag("acceptance", acceptance))
    if assignee:
        args.append(bd_flag("assignee", assignee))
    deps = [dep for dep in (depends_on or []) if isinstance(dep, str) and BEAD_ID.match(dep)]
    if deps:
        args.append(bd_flag("deps", ",".join(deps)))
    args += ["--", title]
    detail = {
        "title": title, "priority": priority, "type": type, "description": description,
        "acceptance": acceptance, "assignee": assignee, "depends_on": deps,
    }
    return await _create_bead(context, directory, container_id, args, request_id, detail)


@function_tool
async def beads_create(
    context: RunContextWrapper,
    title: str,
    priority: int = 1,
    type: str = "task",
    description: str = "",
    acceptance: str = "",
    assignee: str = "",
    depends_on: Optional[List[str]] = None,
    directory: str = ".",
    request_id: str = "",
) -> str:
    """
    Create a new bead (task, epic, message, etc.).
    
    Args:
        title: Short title of the bead
        priority: Priority (0=P0, 1=P1, 2=P2, 3=P3)
        type: Type of bead (task, epic, message, etc.)
        description: Detailed description
        acceptance: How to tell the task is done
        assignee: Who the task is for
        depends_on: Ids of existing tasks that must be done first
        directory: Path to the project directory
        request_id: Optional key of your own; repeating it answers with the
            first call's bead instead of creating a second one.
    """
    return await create_bead(
        context, title, priority, type, description, acceptance, assignee, depends_on,
        directory, request_id,
    )

@function_tool
async def beads_show(context: RunContextWrapper, bead_id: str, directory: str = ".") -> str:
    """
    Show detailed information about a specific bead.
    
    Args:
        bead_id: The ID of the bead (e.g., "bd-a1b2")
        directory: Path to the project directory
    """
    try:
        directory = _resolve_directory(context, directory)
    except ValueError as exc:
        return _error_text("tracker_scope", str(exc))
    container_id = _get_container_id(context)
    if problem := _bad_ids(bead_id):
        return problem

    # --json keeps the task's own fields (dependencies, notes, acceptance)
    # readable by the caller; bd's plain output is prose meant for a person.
    res = await _bd(["show", bead_id, "--json"], cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return _error_text(res.get("code") or "tracker_failed", f"showing bead {bead_id}: {_failure_text(res)}")
    return res["output"]

#: How a close carries the operation (and the run) that made it, inside the
#: reason text bd stores: ``<reason> [grid-op:<operation>[ run:<run>]]``.
CLOSE_STAMP = re.compile(r"^(?P<reason>.*) \[grid-op:(?P<op>[0-9A-Za-z]+)(?: run:(?P<run>[^\]\s]+))?\]$", re.DOTALL)


def _close_stamp(operation_id: str, run_id: Optional[str] = None) -> str:
    """The stamp a close writes: this operation, and the run that made it."""
    return f"[grid-op:{operation_id}" + (f" run:{run_id}]" if run_id else "]")


def _parse_close_reason(text: str) -> Optional[tuple]:
    """The reason, operation and run a close stamp holds; None when unstamped."""
    match = CLOSE_STAMP.match(text or "")
    if match is None:
        return None
    return match.group("reason"), match.group("op"), match.group("run")


def _shown_issue(res: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The one issue a ``bd show --json`` answer holds, whichever shape it came in."""
    data = res.get("data")
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else None
    return data if isinstance(data, dict) else None


#: How an update's intended fields read in a bd issue: one that is not the
#: given text is not this operation's write, whatever the marker says.
_UPDATE_FIELDS = {
    "status": "status",
    "assignee": "assignee",
    "title": "title",
    "description": "description",
    "notes": "notes",
    "acceptance": "acceptance_criteria",
    "priority": "priority",
    "type": "issue_type",
}


async def _reconcile_update(
    directory: str,
    bead_id: str,
    operation_id: str,
    expected: Dict[str, Any],
    container_id: Optional[str],
    context: Any,
) -> Optional[Dict[str, Any]]:
    """The task as the tracker holds it, when it *is* this operation's result.

    A status or an assignee that happens to match is not proof: the task could
    have been in that state before the lost answer, or another actor could
    have put it there. The update writes ``grid_op=<operation id>`` into the
    issue's metadata in the same bd transaction as the change, so only that
    marker makes the change this operation's commit. Every field the call
    intended is then checked as well: a marker with a different title, notes
    or status is somebody else's write under our name, not this commit.

    The claim's ``grid_run`` metadata rides back in the answer (``run_id``), so
    a repeat can tell whether the write was *its* run's or another run's.
    """
    res = await _bd(["show", bead_id, "--json"], cwd=directory, container_id=container_id, context=context)
    issue = _shown_issue(res) if res.get("success") else None
    if issue is None:
        return None
    metadata = issue.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("grid_op") != operation_id:
        return None
    for field, key in _UPDATE_FIELDS.items():
        wanted = expected.get(field)
        if wanted is not None and _as_text(issue.get(key)) != _as_text(wanted):
            return None
    run_id = metadata.get("grid_run")
    if expected.get("run_id") is not None and _as_text(run_id) != _as_text(expected["run_id"]):
        return None
    return {
        "id": bead_id,
        "status": issue.get("status"),
        "assignee": issue.get("assignee"),
        "run_id": run_id,
        "output": res.get("output") or "",
    }


async def update_bead(
    context: RunContextWrapper,
    bead_id: str,
    status: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    notes: Optional[str] = None,
    claim: bool = False,
    directory: str = ".",
    request_id: str = "",
    acceptance: Optional[str] = None,
    priority: Optional[int] = None,
    type: Optional[str] = None,
    assignee: Optional[str] = None,
) -> str:
    """
    Update an existing bead.
    
    Args:
        bead_id: The ID of the bead
        status: New status (todo, in_progress, blocked, closed)
        title: New title
        description: New description
        notes: Add notes to the bead
        claim: If True, atomically sets assignee to current user and status to in_progress
        directory: Path to the project directory
        request_id: Optional key of your own; repeating it answers with the
            first call's result instead of writing twice.
        acceptance: How to tell the task is done
        priority: New priority (0=P0 .. 4=P4)
        type: New issue type
        assignee: Who the task is for (a claim sets it to the run's own actor)
    """
    try:
        directory = _resolve_directory(context, directory)
        request_id = tracker_operations.check_request_id(request_id)
    except tracker_operations.OperationError as exc:
        return _typed_error(exc.code, f"{exc} Nothing was written.")
    except ValueError as exc:
        return _typed_error("tracker_scope", str(exc))
    container_id = _get_container_id(context)
    
    if problem := _bad_ids(bead_id):
        return _typed_error("tracker_args", f"not a bead id: {bead_id!r}")

    actor, _ = _actor_executor(context)
    run_id = _run_id(context)
    # A claim sets the assignee to the run's own actor and records the run in
    # the issue's own metadata: that is what bd stores, and what a lost answer
    # is later checked against - including which run it belonged to.
    expected = {
        "status": "in_progress" if claim else status,
        "assignee": actor if claim else assignee,
        "title": title,
        "description": description,
        "notes": notes,
        "acceptance": acceptance,
        "priority": priority,
        "type": type,
        "run_id": run_id if claim else None,
    }

    async def run(operation: "tracker_operations.Operation") -> Dict[str, Any]:
        # A claimed task belongs to this run: the journal is checked (and, for
        # a claim, reserved) before bd sees the write, so a run cannot take or
        # change a task another *live* run holds. bd still guards its own
        # assignee; this is what tells two runs of one user apart.
        refusal = None if claim else await _guard_owner(context, directory, bead_id)
        if refusal is not None:
            return refusal
        if claim:
            refusal = await _reserve_claim(context, directory, bead_id, run_id, actor)
            if refusal is not None:
                return refusal
        # The marker is part of the same bd command as the change, so the
        # tracker records both in one transaction: reading it back is proof
        # that *this* update committed, not that the task happens to look right.
        args = ["update", bead_id, "--json", bd_flag("set-metadata", f"grid_op={operation.operation_id}")]
        if claim:
            args.append("--claim")
            # The run that holds the claim, visible in the tracker itself; bd
            # keeps its own status and assignee untouched by this metadata.
            args.append(bd_flag("set-metadata", f"grid_run={run_id}"))
        if status:
            args.append(bd_flag("status", status))
        if title:
            args.append(bd_flag("title", title))
        if description:
            args.append(bd_flag("description", description))
        if acceptance:
            args.append(bd_flag("acceptance", acceptance))
        if priority is not None:
            args.append(bd_flag("priority", priority))
        if type:
            args.append(bd_flag("type", type))
        if assignee and not claim:
            args.append(bd_flag("assignee", assignee))
        if notes:
            args.append(bd_flag("notes", notes))
        res = await _bd(args, cwd=directory, container_id=container_id, context=context, env=_bd_env(context))
        if claim and not res.get("success"):
            await _release_unconfirmed(context, directory, bead_id, run_id, res)
        return res

    async def reconcile(operation: "tracker_operations.Operation") -> Optional[Dict[str, Any]]:
        return await _reconcile_update(
            directory, bead_id, operation.operation_id, expected, container_id, context
        )

    def reconciled_text(data: Dict[str, Any]) -> str:
        return json.dumps(
            {
                "id": data["id"],
                "status": data.get("status"),
                "assignee": data.get("assignee"),
                "run_id": data.get("run_id"),
                "committed": True,
                "reconciled": True,
            },
            ensure_ascii=False,
        )

    outcome: Dict[str, Any] = {}
    answer = await _durable_write(
        context,
        directory,
        container_id,
        kind="update",
        request_id=request_id,
        detail={
            "bead_id": bead_id,
            "claim": bool(claim),
            "status": status,
            "title": title,
            "description": description,
            "notes": notes,
            "acceptance": acceptance,
            "priority": priority,
            "type": type,
            "assignee": None if claim else assignee,
        },
        run=run,
        success_text=lambda res, operation: json.dumps(
            {
                "id": bead_id,
                "status": expected.get("status") or (_shown_issue(res) or {}).get("status"),
                "assignee": expected.get("assignee") or (_shown_issue(res) or {}).get("assignee"),
                "committed": True,
                "operation_id": operation.operation_id,
            },
            ensure_ascii=False,
        ),
        reconcile=reconcile,
        reconciled_text=reconciled_text,
        journal_data=lambda res: {"output": res.get("output") or "", "bead_id": bead_id},
        outcome=outcome,
    )
    performed = bool(outcome.get("performed"))
    if claim:
        # A confirmed claim is the run's, for its finally - but only for the
        # run that made it. A repeat that merely replays another run's claim
        # does not inherit its ownership: idempotency returns the answer, it
        # does not move the task to the caller.
        if performed or _claim_of_run(answer, run_id):
            await _note_confirmed_claim(context, directory, bead_id, answer)
    elif status == "closed":
        # Closing through an update is a close: the binding ends with it, and
        # only the call that actually closed it may settle that binding.
        if performed:
            await _settle_after_write(context, directory, bead_id, "closed", notes or "")
    elif run_id and performed:
        try:
            await asyncio.to_thread(
                tracker_lifecycle.checkpoint, directory, bead_id, run_id, "update",
                notes or (f"status={status}" if status else ""),
            )
        except Exception:  # noqa: BLE001 - a checkpoint is not the write
            logger.warning("the update of %s could not be checkpointed", bead_id, exc_info=True)
    return answer


@function_tool
async def beads_update(
    context: RunContextWrapper,
    bead_id: str,
    status: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    notes: Optional[str] = None,
    claim: bool = False,
    directory: str = ".",
    request_id: str = "",
    acceptance: Optional[str] = None,
    priority: Optional[int] = None,
    type: Optional[str] = None,
    assignee: Optional[str] = None,
) -> str:
    """
    Update an existing bead.

    Args:
        bead_id: The ID of the bead
        status: New status (todo, in_progress, blocked, closed)
        title: New title
        description: New description
        notes: Add notes to the bead
        claim: If True, atomically sets assignee to current user and status to in_progress
        directory: Path to the project directory
        request_id: Optional key of your own; repeating it answers with the
            first call's result instead of writing twice.
        acceptance: How to tell the task is done
        priority: New priority (0=P0 .. 4=P4)
        type: New issue type
        assignee: Who the task is for (a claim sets it to the run's own actor)
    """
    return await update_bead(
        context, bead_id, status, title, description, notes, claim, directory, request_id,
        acceptance, priority, type, assignee,
    )

async def close_bead(
    context: RunContextWrapper,
    bead_id: str,
    reason: str = "Completed",
    directory: str = ".",
    request_id: str = "",
) -> str:
    """
    Close a bead.
    
    Args:
        bead_id: The ID of the bead
        reason: Reason for closing
        directory: Path to the project directory
        request_id: Optional key of your own; repeating it answers with the
            first call's result instead of closing twice.
    """
    try:
        directory = _resolve_directory(context, directory)
        request_id = tracker_operations.check_request_id(request_id)
    except tracker_operations.OperationError as exc:
        return _typed_error(exc.code, f"{exc} Nothing was written.")
    except ValueError as exc:
        return _typed_error("tracker_scope", str(exc))
    container_id = _get_container_id(context)
    if problem := _bad_ids(bead_id):
        return _typed_error("tracker_args", f"not a bead id: {bead_id!r}")

    run_id = _run_id(context)

    async def run(operation: "tracker_operations.Operation") -> Dict[str, Any]:
        # A task a live other run holds is not this run's to close: bd refuses
        # a close by another assignee, and the journal refuses one by another
        # run of the same user (core.tracker_lifecycle). A stale binding does
        # not block - its lease is over and the close records the takeover.
        refusal = await _guard_owner(context, directory, bead_id)
        if refusal is not None:
            return refusal
        # The close reason carries the operation's marker in the same bd
        # transaction as the close: a lost answer is proven by reading back a
        # reason this operation wrote, not by finding the task closed, which
        # any other actor could have made true. The run that closed it rides
        # along, so a repeat can tell whose close it reads.
        marked = f"{reason} {_close_stamp(operation.operation_id, run_id)}"
        res = await _bd(
            ["close", bead_id, bd_flag("reason", marked), "--json"],
            cwd=directory,
            container_id=container_id,
            context=context,
            env=_bd_env(context),
        )
        if res.get("success"):
            await _settle_after_write(context, directory, bead_id, "closed", reason)
        return res

    async def reconcile(operation: "tracker_operations.Operation") -> Optional[Dict[str, Any]]:
        res = await _bd(["show", bead_id, "--json"], cwd=directory, container_id=container_id, context=context)
        issue = _shown_issue(res) if res.get("success") else None
        if issue is None or issue.get("status") != "closed":
            return None
        parsed = _parse_close_reason(issue.get("close_reason") or "")
        if parsed is None:
            return None
        stamped_reason, stamped_op, stamped_run = parsed
        # The reason is checked in full, not for a substring: a close with our
        # marker but somebody else's words is not this request's close.
        if stamped_op != operation.operation_id or stamped_reason != reason:
            return None
        return {
            "id": bead_id,
            "status": "closed",
            "close_reason": reason,
            "run_id": stamped_run,
            "output": res.get("output") or "",
        }

    outcome: Dict[str, Any] = {}
    answer = await _durable_write(
        context,
        directory,
        container_id,
        kind="close",
        request_id=request_id,
        detail={"bead_id": bead_id, "reason": reason},
        run=run,
        success_text=lambda res, operation: json.dumps(
            {
                "id": bead_id,
                "status": "closed",
                "close_reason": reason,
                "committed": True,
                "operation_id": operation.operation_id,
            },
            ensure_ascii=False,
        ),
        reconcile=reconcile,
        reconciled_text=lambda data: json.dumps(
            {**data, "committed": True, "reconciled": True}, ensure_ascii=False
        ),
        journal_data=lambda res: {"output": res.get("output") or "", "bead_id": bead_id, "close_reason": reason},
        outcome=outcome,
    )
    # A close reconciled from a lost answer is a confirmed close too - but
    # only the call that closed it (or a repeat proven to be the same run's)
    # settles the binding: replaying does not make the caller the owner.
    settled = _answer_payload(answer)
    if isinstance(settled, dict) and settled.get("status") == "closed":
        if outcome.get("performed") or _claim_of_run(settled, run_id):
            await _settle_after_write(context, directory, bead_id, "closed", reason)
    return answer


@function_tool
async def beads_close(
    context: RunContextWrapper,
    bead_id: str,
    reason: str = "Completed",
    directory: str = ".",
    request_id: str = "",
) -> str:
    """
    Close a bead.

    Args:
        bead_id: The ID of the bead
        reason: Reason for closing
        directory: Path to the project directory
        request_id: Optional key of your own; repeating it answers with the
            first call's result instead of closing twice.
    """
    return await close_bead(context, bead_id, reason, directory, request_id)

@function_tool
async def beads_sync(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Sync beads with the remote repository (pull, export, commit, push).
    Should be called at the end of a session or after significant changes.
    
    Args:
        directory: Path to the project directory
    """
    try:
        directory = _resolve_directory(context, directory)
    except ValueError as exc:
        return _error_text("tracker_scope", str(exc))
    container_id = _get_container_id(context)
    
    res = await _bd(["sync"], cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return _error_text(res.get("code") or "tracker_failed", f"syncing beads: {_failure_text(res)}")
    return res["output"]

async def dep_bead(
    context: RunContextWrapper,
    action: str,
    child_id: str,
    parent_id: str,
    directory: str = ".",
    request_id: str = "",
) -> str:
    """
    The plain coroutine behind ``beads_dep``: core.tracker_service hands it to
    callers that are not tools (web_chat.tasks_api), so an edge the page adds
    carries the same journal record, request id and actor as an agent's.

    Manage dependencies between beads.

    
    Args:
        action: "add" or "remove"
        child_id: The ID of the child/blocked bead
        parent_id: The ID of the parent/blocker bead
        directory: Path to the project directory
        request_id: Optional key of your own; repeating it answers with the
            first call's result instead of changing the graph twice.
    """
    try:
        directory = _resolve_directory(context, directory)
        request_id = tracker_operations.check_request_id(request_id)
    except tracker_operations.OperationError as exc:
        return _typed_error(exc.code, f"{exc} Nothing was written.")
    except ValueError as exc:
        return _typed_error("tracker_scope", str(exc))
    container_id = _get_container_id(context)
    
    if action not in ["add", "remove"]:
        return _typed_error("tracker_args", "action must be 'add' or 'remove'")
    if problem := _bad_ids(child_id, parent_id):
        return _typed_error("tracker_args", f"not a bead id: {child_id!r} or {parent_id!r}")

    async def run(operation: "tracker_operations.Operation") -> Dict[str, Any]:
        return await _bd(
            ["dep", action, child_id, parent_id],
            cwd=directory,
            container_id=container_id,
            context=context,
            env=_bd_env(context),
        )

    async def reconcile(operation: "tracker_operations.Operation") -> Optional[Dict[str, Any]]:
        # A dependency is idempotent: the intended state holding means the
        # request's effect is present, whether or not this exact call wrote it.
        res = await _bd(["show", child_id, "--json"], cwd=directory, container_id=container_id, context=context)
        issue = _shown_issue(res) if res.get("success") else None
        if issue is None:
            return None
        deps = {
            dep.get("depends_on_id") or dep.get("id")
            for dep in (issue.get("dependencies") or [])
            if isinstance(dep, dict)
        }
        if action == "add" and parent_id not in deps:
            return None
        if action == "remove" and parent_id in deps:
            return None
        return {"id": child_id, "action": action, "parent_id": parent_id, "output": res.get("output") or ""}

    return await _durable_write(
        context,
        directory,
        container_id,
        kind="dep",
        request_id=request_id,
        detail={"action": action, "child": child_id, "parent": parent_id},
        run=run,
        success_text=lambda res, operation: res.get("output") or "",
        reconcile=reconcile,
        reconciled_text=lambda data: json.dumps(data, ensure_ascii=False),
        journal_data=lambda res: {"output": res.get("output") or ""},
    )

@function_tool
async def beads_dep(
    context: RunContextWrapper,
    action: str,
    child_id: str,
    parent_id: str,
    directory: str = ".",
    request_id: str = "",
) -> str:
    """
    Manage dependencies between beads.

    Args:
        action: "add" or "remove"
        child_id: The ID of the child/blocked bead
        parent_id: The ID of the parent/blocker bead
        directory: Path to the project directory
        request_id: Optional key of your own; repeating it answers with the
            first call's result instead of changing the graph twice.
    """
    return await dep_bead(context, action, child_id, parent_id, directory, request_id)


class PlanTask(BaseModel):
    """One task of a plan, named by a key that the plan's dependencies use."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Short name for this task in the plan, e.g. 'research-api'.")
    title: str
    description: str = Field(description="What to do: the brief the agent will get.")
    acceptance: str = Field(default="", description="How to tell it is done.")
    depends_on: List[str] = Field(
        default_factory=list,
        description="Keys of tasks of this plan, or ids of existing tasks, that must be done first.",
    )
    priority: int = Field(default=2, ge=0, le=4)


def _graph_plan(tasks: List[PlanTask]) -> Dict[str, Any]:
    """The plan as `bd create --graph` reads it; ValueError naming what is wrong.

    bd checks the graph too (cycles above all); checking keys here first
    names the mistake in the plan's own terms.
    """
    if not tasks:
        raise ValueError("Give at least one task.")
    keys = [task.key.strip() for task in tasks]
    if any(not key for key in keys):
        raise ValueError("Every task needs a key.")
    too_long = sorted({key for key in keys if len(key) > 60})
    if too_long:
        raise ValueError(f"A key is at most 60 characters: {', '.join(too_long[:3])}.")
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"Keys must be unique: {', '.join(duplicates)}.")
    nodes, edges = [], []
    for key, task in zip(keys, tasks):
        node = {"key": key, "title": task.title, "description": task.description, "priority": task.priority}
        if task.acceptance:
            node["acceptance_criteria"] = task.acceptance
        nodes.append(node)
        for dependency in task.depends_on:
            dependency = dependency.strip()
            if dependency == key:
                raise ValueError(f"Task '{key}' cannot depend on itself.")
            # A key of this plan, else an existing task's id.
            edges.append({"from_key": key, "to_key": dependency} if dependency in keys
                         else {"from_key": key, "to_id": dependency})
    return {"nodes": nodes, "edges": edges}


def _bd_error(res: Dict[str, Any]) -> str:
    """bd's own message: its --json error, else what it printed."""
    data = res.get("data")
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    output = res.get("output") or ""
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict) and parsed.get("error"):
            return str(parsed["error"])
    except ValueError:
        pass
    return res.get("error") or output or "bd failed"


def _plan_error(code: str, message: str) -> str:
    """The plan's answer when it did not start: the same JSON as every other one."""
    return json.dumps(
        {"isError": True, "error": message, "code": code, "committed": False, "retryable": False},
        ensure_ascii=False,
    )


def _dependency_ids(item: Dict[str, Any]) -> set:
    """The ids a tracker issue was read as depending on, in either shape."""
    found = set()
    dependencies = item.get("dependencies")
    if not isinstance(dependencies, list):
        return found
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        value = dependency.get("depends_on_id") or dependency.get("id")
        if isinstance(value, str) and value:
            found.add(value)
    return found


def _plan_problems(plan: Dict[str, Any], ids: Dict[str, str], items: Dict[str, Dict[str, Any]]) -> List[str]:
    """What the tracker's marked tasks do not match of the plan asked for.

    A marker proves *a* task was written under this operation's name; it does
    not prove it was written with this title, priority, acceptance or these
    edges. Every intended field and every edge is checked, so a task that
    carries our marker but different content is never reported as committed.
    """
    problems: List[str] = []
    expected_edges: Dict[str, set] = {node["key"]: set() for node in plan["nodes"]}
    for node in plan["nodes"]:
        key = node["key"]
        item = items.get(key)
        if item is None:
            problems.append(f"no task for key '{key}'")
            continue
        for field in ("title", "description", "priority"):
            if field in node and _as_text(item.get(field)) != _as_text(node[field]):
                problems.append(f"task '{key}' has a different {field}")
        if _as_text(item.get("acceptance_criteria")) != _as_text(node.get("acceptance_criteria")):
            problems.append(f"task '{key}' has a different acceptance_criteria")
    for edge in plan["edges"]:
        from_key = edge["from_key"]
        target = ids.get(edge["to_key"]) if "to_key" in edge else edge.get("to_id")
        if from_key not in expected_edges:
            problems.append(f"edge from unknown key '{from_key}'")
            continue
        expected_edges[from_key].add(target)
    for key, wanted in expected_edges.items():
        item = items.get(key)
        if item is None:
            continue
        actual = _dependency_ids(item)
        wanted = {value for value in wanted if value}
        if actual != wanted:
            problems.append(f"task '{key}' has different dependencies")
    return problems


async def _reconcile_plan(
    directory: str,
    operation_id: str,
    plan: Dict[str, Any],
    container_id: Optional[str],
    context: Any,
) -> Optional[tuple]:
    """What the tracker already holds for this operation, by key.

    The marker rides inside the same bd transaction as the tasks, so finding
    every marker is proof the graph was *written* even when its answer was
    lost. It is not proof of *what* was written: the nodes' fields and the
    plan's edges are compared with what this request asked for. Returns
    ``(complete, ids_by_key, problems)``; markers for only some keys mean bd
    failed part-way, which is a partial plan whose real ids are kept, and
    markers whose content differs are a mismatch, not a commit. ``None``
    means the tracker could not be read, so nothing is known.
    """
    prefix = tracker_operations.marker_prefix(operation_id)
    res = await _bd(
        ["list", "--external-contains", prefix, "--json", "--limit", "0", "--all"],
        cwd=directory,
        container_id=container_id,
        context=context,
    )
    data = res.get("data")
    if not res.get("success") or not isinstance(data, list):
        return None
    found: Dict[str, Dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        ref, bead_id = item.get("external_ref"), item.get("id")
        if not isinstance(ref, str) or not isinstance(bead_id, str) or not ref.startswith(prefix):
            continue
        key = ref[len(prefix):].lstrip(":")
        if key:
            found.setdefault(key, item)
    keys = [node["key"] for node in plan["nodes"]]
    ids = {key: found[key]["id"] for key in keys if key in found}
    problems = _plan_problems(plan, ids, found)
    complete = len(ids) == len(keys) and not problems
    return (complete, ids, problems)


async def _plan_waves(
    tasks: List[PlanTask],
    ids: Dict[str, str],
    directory: str,
    container_id: Optional[str],
    context: Any,
) -> Dict[str, Any]:
    """The plan's first wave and what each other task waits for.

    A ``ready`` that fails does not undo the plan: the tasks exist, so the
    answer keeps every id and says only that the first wave is unknown rather
    than inventing an empty one.
    """
    ready = await _bd(
        ["ready", "--json", "--brief", "--limit", "0"],
        cwd=directory,
        container_id=container_id,
        context=context,
    )
    data = ready.get("data")
    known = ready.get("success") and isinstance(data, list)
    ready_ids = {item.get("id") for item in data if isinstance(item, dict)} if known else set()
    ordered = {task.key.strip(): ids.get(task.key.strip()) for task in tasks}
    waves: Dict[str, Any] = {"ready": [], "waiting": []}
    for task in tasks:
        key = task.key.strip()
        if known and ordered[key] in ready_ids:
            waves["ready"].append({"key": key, "id": ordered[key], "title": task.title})
        else:
            waves["waiting"].append({"key": key, "id": ordered[key], "waits_for": task.depends_on})
    if not known:
        # The ids are real; which of them may start now was not answered.
        waves["ready_unknown"] = True
        waves["ready_error"] = _failure_text(ready)
    return {"ids": ordered, **waves}


async def _finish_plan(
    operation: "tracker_operations.Operation",
    tasks: List[PlanTask],
    keys: List[str],
    ids: Dict[str, str],
    directory: str,
    container_id: Optional[str],
    context: Any,
    *,
    reconciled: bool,
) -> str:
    """Complete a created plan: its waves, then the journal's committed record."""
    payload = await _plan_waves(tasks, ids, directory, container_id, context)
    payload["operation_id"] = operation.operation_id
    payload["committed"] = True
    if reconciled:
        payload["reconciled"] = True
    code = "ready_unknown" if payload.get("ready_unknown") else "ok"
    concluded = await _conclude(operation, tracker_operations.COMMITTED, data=payload, code=code)
    if concluded is None:
        payload["warning"] = (
            "The plan is in the tracker but this operation could not be journaled, "
            "so repeating the same request_id cannot be answered from the journal."
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def plan(
    context: RunContextWrapper,
    tasks: List[PlanTask],
    directory: str = ".",
    request_id: str = "",
) -> str:
    """
    Create a whole plan in the tracker in one call: its tasks and the dependencies between them.

    Each task has a key of your choosing; depends_on names the keys (or existing
    task ids) that must be done before it. The plan is checked as a whole - unique
    keys, known references, no cycles - and nothing is created when it is wrong.

    Returns the id of every task by key, the tasks ready to start now (the first
    wave) and, for the rest, which keys each waits for, plus an operation_id.
    A call whose answer was lost reports ``committed: null`` instead of guessing;
    repeating it with the same request_id reconciles against the tracker and
    never creates a second plan.

    Args:
        tasks: The plan's tasks
        directory: Path to the project directory
        request_id: Optional key of your own for this request; repeating it
            returns the first call's result instead of creating another plan.
    """
    try:
        directory = _resolve_directory(context, directory)
        plan = _graph_plan(tasks)
        request_id = tracker_operations.check_request_id(request_id)
    except tracker_operations.OperationError as exc:
        return _plan_error(exc.code, f"{exc} Nothing was created.")
    except ValueError as exc:
        return _plan_error("tracker_plan_invalid", f"{exc} Nothing was created.")
    container_id = _get_container_id(context)
    keys = [task.key.strip() for task in tasks]

    if not (Path(directory) / ".beads").is_dir():
        return _plan_error("tracker_missing", "No tracker here: call beads_init first. Nothing was created.")

    # Keep the graph input beside the actual tracker. A redirect alias is not
    # mounted in the temporary Docker backend; its target workspace is.
    try:
        directory = str(tracker_gateway.storage_identity(directory).beads.parent)
    except tracker_gateway.TrackerError as exc:
        return _plan_error(exc.code, f"{exc} Nothing was created.")

    try:
        # The fingerprint covers the whole plan, not just its keys: the same
        # request id with different briefs, priorities or edges is a conflict.
        operation = await _begin_operation(
            context,
            directory,
            "plan",
            request_id,
            {"keys": keys, "nodes": plan["nodes"], "edges": plan["edges"]},
        )
    except tracker_operations.OperationError as exc:
        return _plan_error(exc.code, f"{exc} Nothing was created.")
    except Exception as exc:  # noqa: BLE001 - an unrecorded write is not attempted
        logger.warning("The plan operation could not be started", exc_info=True)
        return _plan_error("tracker_operation", f"The plan could not be recorded ({exc}). Nothing was created.")

    if not operation.is_new:
        return await _replay_plan(operation, tasks, keys, plan, directory, container_id, context)

    # The marker must be inside the graph bd writes: it is the only evidence
    # that survives a lost answer, and it rides the same commit as the tasks.
    for node in plan["nodes"]:
        node["external_ref"] = tracker_operations.marker(operation.operation_id, node["key"])

    # bd reads a plan from a file; it lives beside the tracker, which the
    # container sees too, for as long as the command runs.
    beads_dir = Path(directory) / ".beads"
    plan_file = beads_dir / f"plan-{uuid.uuid4().hex[:8]}.json"
    try:
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        await _conclude(operation, tracker_operations.FAILED,
                        error=str(exc), code="tracker_plan_input")
        return _plan_error(
            "tracker_plan_input",
            f"The graph input could not be written ({exc}). Nothing was created.",
        )
    try:
        res = await _bd(
            ["create", "--graph", f".beads/{plan_file.name}", "--json"],
            cwd=directory,
            container_id=container_id,
            context=context,
            env=_bd_env(context),
        )
    except Exception as exc:  # noqa: BLE001 - the answer was lost, not the plan
        logger.warning("The plan command did not answer", exc_info=True)
        res = {"success": False, "output": "", "error": str(exc), "data": None, "exit_code": -1}
    finally:
        try:
            plan_file.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove tracker graph input %s", plan_file.name, exc_info=True)

    data = res.get("data")
    ids = data.get("ids") if res.get("success") and isinstance(data, dict) else None
    if isinstance(ids, dict) and all(key in ids for key in keys):
        return await _finish_plan(operation, tasks, keys, ids, directory, container_id, context, reconciled=False)

    # No usable answer: ask the tracker itself, by the marker this call wrote.
    proof = await _reconcile_plan(directory, operation.operation_id, plan, container_id, context)
    if proof is not None:
        complete, found, problems = proof
        if complete:
            return await _finish_plan(operation, tasks, keys, found, directory, container_id, context, reconciled=True)
        if found and len(found) == len(keys):
            # Every key is in the tracker, but not with what this plan asked
            # for: the markers are ours, the content is not. That is not a
            # commit of this plan, and it must not be reported as one.
            await _conclude(
                operation,
                tracker_operations.UNKNOWN,
                data={"ids": found},
                error="the tracker holds different content under this operation's markers",
                code="tracker_plan_mismatch",
            )
            return json.dumps(
                {
                    "isError": True,
                    "error": "The tracker holds tasks under this operation's markers, but their "
                             "content or dependencies differ from this plan. Nothing was repeated.",
                    "code": "tracker_plan_mismatch",
                    "committed": None,
                    "ids": found,
                    "problems": problems,
                    "operation_id": operation.operation_id,
                    "retryable": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        if found:
            # bd failed part-way: some tasks exist. Their ids are real and the
            # caller keeps them; the plan is not whole and is not repeated.
            await _conclude(
                operation,
                tracker_operations.UNKNOWN,
                data={"ids": found},
                error="only part of the plan reached the tracker",
                code="tracker_plan_partial",
            )
            return json.dumps(
                {
                    "isError": True,
                    "error": f"Only part of the plan was created ({_failure_text(res)}). Created ids are below; "
                             f"nothing was repeated.",
                    "code": "tracker_plan_partial",
                    "committed": None,
                    "ids": found,
                    "operation_id": operation.operation_id,
                    "retryable": False,
                },
                ensure_ascii=False,
                indent=2,
            )

    if _inconclusive(res) or (proof is None and not res.get("failed_before_run")):
        await _conclude(
            operation,
            tracker_operations.UNKNOWN,
            error=_failure_text(res),
            code="tracker_unknown",
        )
        return json.dumps(
            {
                "isError": True,
                "error": f"{_failure_text(res)}. The plan may or may not have been created "
                         f"(operation {operation.operation_id}); nothing was repeated.",
                "code": "tracker_unknown",
                "committed": None,
                "operation_id": operation.operation_id,
                "retryable": False,
                "hint": "Call beads_plan again with the same request_id to reconcile; do not create the plan by hand.",
            },
            ensure_ascii=False,
            indent=2,
        )

    # bd refused and the tracker holds no marker of this operation: nothing was
    # created, and that is now a fact rather than a hope about a timeout.
    reason = _bd_error(res)
    await _conclude(operation, tracker_operations.FAILED, error=reason, code=res.get("code") or "tracker_rejected")
    return json.dumps(
        {
            "isError": True,
            "error": f"{reason}. Nothing was created.",
            "code": "tracker_rejected",
            "committed": False,
            "operation_id": operation.operation_id,
            "retryable": False,
        },
        ensure_ascii=False,
    )


@function_tool
async def beads_plan(
    context: RunContextWrapper,
    tasks: List[PlanTask],
    directory: str = ".",
    request_id: str = "",
) -> str:
    """
    Create a whole plan in the tracker in one call: its tasks and the dependencies between them.

    Each task has a key of your choosing; depends_on names the keys (or existing
    task ids) that must be done before it. The plan is checked as a whole - unique
    keys, known references, no cycles - and nothing is created when it is wrong.

    Returns the id of every task by key, the tasks ready to start now (the first
    wave) and, for the rest, which keys each waits for, plus an operation_id.
    A call whose answer was lost reports ``committed: null`` instead of guessing;
    repeating it with the same request_id reconciles against the tracker and
    never creates a second plan.

    Args:
        tasks: The plan's tasks
        directory: Path to the project directory
        request_id: Optional key of your own for this request; repeating it
            returns the first call's result instead of creating another plan.
    """
    return await plan(context, tasks, directory, request_id)

async def _replay_plan(
    operation: "tracker_operations.Operation",
    tasks: List[PlanTask],
    keys: List[str],
    plan: Dict[str, Any],
    directory: str,
    container_id: Optional[str],
    context: Any,
) -> str:
    """A repeated request: the first call's answer, or the tracker's own truth.

    A finished operation answers for its key. An unfinished one - a call still
    running elsewhere, or one whose process died - is never repeated: its
    markers are looked up, and a complete plan found there is adopted and
    finished (its ``ready`` was never answered). An operation recorded as
    ``unknown`` is looked up the same way: a later answer from the tracker may
    still prove it, and upgrading the journal is not a repeat.
    """
    if operation.state in (tracker_operations.COMMITTED, tracker_operations.FAILED):
        return _settled_answer(operation, dump=True) or ""

    proof = await _reconcile_plan(directory, operation.operation_id, plan, container_id, context)
    if proof is not None and proof[0]:
        return await _finish_plan(operation, tasks, keys, proof[1], directory, container_id, context, reconciled=True)
    if proof is not None and proof[1] and len(proof[1]) == len(keys):
        await _conclude(
            operation,
            tracker_operations.UNKNOWN,
            data={"ids": proof[1]},
            error="the tracker holds different content under this operation's markers",
            code="tracker_plan_mismatch",
        )
        return json.dumps(
            {
                "isError": True,
                "error": "The tracker holds tasks under this operation's markers, but their content "
                         "or dependencies differ from this plan. Nothing was repeated.",
                "code": "tracker_plan_mismatch",
                "committed": None,
                "ids": proof[1],
                "problems": proof[2],
                "operation_id": operation.operation_id,
                "duplicate": True,
                "retryable": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    if proof is not None and proof[1]:
        await _conclude(
            operation,
            tracker_operations.UNKNOWN,
            data={"ids": proof[1]},
            error="only part of the plan reached the tracker",
            code="tracker_plan_partial",
        )
        return json.dumps(
            {
                "isError": True,
                "error": "Only part of this plan reached the tracker; its ids are below. Nothing was repeated.",
                "code": "tracker_plan_partial",
                "committed": None,
                "ids": proof[1],
                "operation_id": operation.operation_id,
                "duplicate": True,
                "retryable": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    return _settled_answer(operation, dump=True) or ""


@function_tool
async def beads_list(
    context: RunContextWrapper,
    all: bool = False,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 15,
    directory: str = ".",
) -> str:
    """
    List beads tasks with optional filters and simple pagination.

    Important:
    - By default returns at most 15 items to keep tool output bounded.
    - Use `page=2` (and so on) to fetch subsequent pages.
    
    Args:
        all: If True, show all issues including closed ones.
        status: Filter by status (open, in_progress, blocked, deferred, closed).
        page: 1-based page number.
        page_size: Items per page (clamped to 1..15).
        directory: Path to the project directory (default: ".")
    """
    try:
        directory = _resolve_directory(context, directory)
    except ValueError as exc:
        return _error_text("tracker_scope", str(exc))
    container_id = _get_container_id(context)

    # Safety clamps to avoid huge tool outputs.
    try:
        page = int(page)
    except Exception:
        page = 1
    if page < 1:
        page = 1

    try:
        page_size = int(page_size)
    except Exception:
        page_size = 15
    if page_size < 1:
        page_size = 1
    if page_size > 15:
        page_size = 15

    # Fetch full list (JSON) then paginate locally to preserve total count and navigation hints.
    # Sorting newest-first tends to be the most useful for agents.
    args = ["list", "--limit", "0", "--sort", "updated", "--reverse"]
    if all:
        args.append("--all")
    if status:
        args.append(bd_flag("status", status))

    res = await _bd(args, cwd=directory, container_id=container_id, context=context)
    if not res["success"]:
        return _error_text(res.get("code") or "tracker_failed", f"listing beads: {_failure_text(res)}")

    issues = res.get("data")
    if issues is None:
        # Fallback: try to parse JSON from stdout (e.g., if warnings preceded JSON).
        try:
            issues = json.loads(res.get("output") or "[]")
        except Exception:
            issues = []

    if not isinstance(issues, list):
        return "❌ Error listing beads: unexpected output format"

    total = len(issues)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    start = (page - 1) * page_size
    end = start + page_size
    items = issues[start:end] if start < total else []

    payload = {
        "page": page,
        "page_size": page_size,
        "returned": len(items),
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1 and total_pages > 0,
        "has_next": total_pages > 0 and page < total_pages,
        "prev_page": (page - 1) if page > 1 else None,
        "next_page": (page + 1) if total_pages > 0 and page < total_pages else None,
        "items": items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

# Registry for easy integration
def _docker_fallback_enabled() -> bool:
    return os.environ.get("BEADS_DISABLE_DOCKER_FALLBACK", "0") not in ("1", "true", "yes")


def _bd_problem() -> Optional[str]:
    """Why bd cannot run on this host, following the same lookup as _find_bd."""
    bd = _find_bd()
    if os.path.isfile(bd) or shutil.which(bd):
        return None
    if _docker_fallback_enabled() and shutil.which("docker"):
        return None  # bd runs inside the isolation image instead
    if _docker_fallback_enabled():
        return "bd is not installed (BEADS_BD_PATH, ~/.local/bin/bd, PATH) and docker for the fallback is missing"
    return "bd is not installed (BEADS_BD_PATH, ~/.local/bin/bd, PATH); docker fallback is disabled"


_BD = Requires(check=_bd_problem, hint="Install beads (bd) or set BEADS_BD_PATH to its executable")
# beads_log_append/beads_log_read only touch files under .beads/ and need nothing.
TOOL_REQUIREMENTS = {name: _BD for name in (
    "beads_init", "beads_ready", "beads_list", "beads_create", "beads_show",
    "beads_update", "beads_close", "beads_sync", "beads_dep", "beads_plan",
)}


BEADS_TOOLS = {
    "beads_init": beads_init,
    "beads_ready": beads_ready,
    "beads_list": beads_list,
    "beads_create": beads_create,
    "beads_show": beads_show,
    "beads_update": beads_update,
    "beads_close": beads_close,
    "beads_sync": beads_sync,
    "beads_dep": beads_dep,
    "beads_plan": beads_plan,
    # Shared append-only log helpers (useful for games / chat streams)
    "beads_log_append": beads_log_append,
    "beads_log_read": beads_log_read,
}


# Where these tools act (utils.tool_isolation): bd runs in the run's container when there is one
from utils.tool_isolation import CONTAINER as _CONTAINER  # noqa: E402

TOOL_ISOLATION = {
    "beads_close": _CONTAINER,
    "beads_create": _CONTAINER,
    "beads_dep": _CONTAINER,
    "beads_init": _CONTAINER,
    "beads_list": _CONTAINER,
    "beads_log_append": _CONTAINER,
    "beads_log_read": _CONTAINER,
    "beads_plan": _CONTAINER,
    "beads_ready": _CONTAINER,
    "beads_show": _CONTAINER,
    "beads_sync": _CONTAINER,
    "beads_update": _CONTAINER,
}

# What these tools do (utils.tool_effects): the tracker is the workspace's own
# state; sync pulls and pushes the remote repository.
from utils import tool_effects as _effects  # noqa: E402

TOOL_EFFECTS = {
    "beads_close": _effects.write(),
    "beads_create": _effects.write(),
    "beads_dep": _effects.write(),
    "beads_init": _effects.write(),
    "beads_list": _effects.read(),
    "beads_log_append": _effects.write(),
    "beads_log_read": _effects.read(),
    "beads_plan": _effects.write(),
    "beads_ready": _effects.read(),
    "beads_show": _effects.read(),
    "beads_sync": _effects.EXTERNAL_ANY,
    "beads_update": _effects.write(),
}
