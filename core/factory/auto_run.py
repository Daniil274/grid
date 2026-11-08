"""Tools the operator has an agent run before its model is asked.

``auto_run_tools`` of an agent's config: one-time ones once per user and
agent, per-message ones on every request; their output becomes part of the
agent's input. They need not be among the agent's own tools: the operator
runs them, so a one-time setup step does not have to be offered to the model.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agents import Agent

from core.action_policy import is_policy_block
from core.factory.preparation import (
    BLOCKED,
    DONE,
    FAILED,
    Step,
    StepOutcome,
    is_concurrent,
    plan_steps,
    run_steps,
    timings,
)
from core.factory.run_context import AutoRunToolContext, GridRunContext
from schemas import AgentConfig
from utils.path_utils import CONTAINER_WORKDIR
from utils.exceptions import ConfigError
from utils.logger import Logger

logger = logging.getLogger("grid.agent_factory")


#: Statuses a tool's own return value reports as a failure, regardless of how
#: it is worded. The unified structured contract the tracker tools will adopt
#: (``ok``/``success``/``status``/``error``) is read here already; ``❌`` is the
#: legacy text contract of the beads tools until then.
_FAILED_STATUSES = frozenset({"error", "failed", "failure", "denied", "refused"})
_SUCCESS_FLAGS = ("success", "ok")
_LEGACY_ERROR_MARK = "❌"


class PreparationFailed(RuntimeError):
    """A required batch of steps did not finish: the agent must not start.

    Raised instead of starting a model on incomplete context. ``result`` keeps
    the per-step outcome so a caller can report which step failed and why.
    """

    def __init__(self, result: "PreparationResult", subject: str = "preparation"):
        self.result = result
        self.subject = subject
        super().__init__(result.failure_report(subject))


class StepDidNotSucceed(RuntimeError):
    """A tool returned but its own result says the call failed.

    Raised inside a batch so the step is recorded ``FAILED`` with the tool's
    reason as its detail, exactly as a raised call is: a returned failure is
    not a successful preparation.
    """


@dataclass(frozen=True)
class StepFailure:
    """One step that did not finish: its name, status and the reason."""

    name: str
    status: str
    detail: str = ""

    def describe(self) -> str:
        reason = self.detail or "no reason reported"
        return f"{self.name} ({self.status}): {reason}"


class PreparationResult(str):
    """A batch's injected text, with the typed outcome of every step.

    The string value is what is prepended to the agent's instructions, so a
    caller that only needs the text uses it as before. The attributes carry the
    verdict, and that is what a caller must decide on: ``ok`` is True only when
    every step finished ``DONE``. A step that raised, was missing, returned a
    failure or was refused by the action policy makes ``ok`` False; it is never
    reported as a successful preparation.
    """

    def __new__(
        cls,
        text: str,
        outcomes: List[StepOutcome],
        *,
        phase: str,
        required: bool = True,
    ) -> "PreparationResult":
        result = super().__new__(cls, text or "")
        result.outcomes = list(outcomes)
        result.phase = phase
        result.required = bool(required)
        return result

    @property
    def ok(self) -> bool:
        """True only when every step finished; an empty batch is a success."""
        return all(outcome.status == DONE for outcome in self.outcomes)

    @property
    def failures(self) -> List[StepFailure]:
        """The steps that did not finish, in the batch's order."""
        return [
            StepFailure(
                name=outcome.step.name,
                status=outcome.status,
                detail=outcome.detail,
            )
            for outcome in self.outcomes
            if outcome.status != DONE
        ]

    def failure_report(self, subject: str = "preparation") -> str:
        """A line a caller can show: what did not happen and why."""
        failures = self.failures
        if not failures:
            return f"{subject} succeeded"
        reasons = "; ".join(failure.describe() for failure in failures)
        return (
            f"{subject}: {len(failures)} of {len(self.outcomes)} step(s) did not "
            f"finish: {reasons}"
        )


def failure_reason(value: Any) -> Optional[str]:
    """Why a tool's return value says the call did not succeed, or None.

    The structured fields are authoritative: ``success``/``ok`` false, a
    failure ``status``, or an ``error`` without a success flag. A text answer
    is read through the documented legacy contract of the beads tools (a
    leading ``❌``); a plain text without it is not judged on its wording.
    """
    data: Optional[Mapping[str, Any]] = None
    if isinstance(value, Mapping):
        data = value
    elif isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, Mapping):
                data = parsed
        if data is None:
            if stripped.startswith(_LEGACY_ERROR_MARK):
                return stripped.splitlines()[0].lstrip(_LEGACY_ERROR_MARK).strip() or "failed"
            return None

    if data is None:
        return None

    for flag in _SUCCESS_FLAGS:
        if data.get(flag) is False:
            detail = data.get("error") or data.get("message") or data.get("detail")
            return str(detail) if detail else f"{flag} is false"
    status = data.get("status")
    if isinstance(status, str) and status.lower() in _FAILED_STATUSES:
        detail = data.get("error") or data.get("reason") or data.get("message")
        return str(detail) if detail else f"status {status!r}"
    error = data.get("error")
    if error and not any(data.get(flag) is True for flag in _SUCCESS_FLAGS):
        return str(error)
    return None


class AutoRunTools:
    """Runs the operator's auto_run_tools of an agent.

    Mixed into AgentFactory (core.agent_factory); relies on self.config,
    self.container_id, self._stream_observer, self._initialized_agents.
    """

    def _substitute_tool_params(
        self, tool_params: dict, working_dir: str, user_message: str = ""
    ) -> dict:
        """Substitute template variables in tool parameters."""
        result = {}
        for k, v in tool_params.items():
            if v == "${working_directory}":
                result[k] = working_dir
            elif v == "${user_message}":
                result[k] = user_message
            else:
                result[k] = v
        return result

    async def _execute_auto_run_tools(
        self,
        agent_key: str,
        agent_config: AgentConfig,
        tools: list,
        working_dir: str,
        run_context: GridRunContext,
        every_run: bool = False,
        user_message: str = "",
    ) -> Tuple[str, bool]:
        """Run auto_run_tools with given working_dir.

        Args:
            every_run: If True, only run tools marked every_run=True.
                       If False, only run one-time tools (every_run not set or False).

        Returns:
            The combined result string to inject, and whether every tool ran:
            a call the action policy blocked is neither injected nor counted
            as done, so one-time tools are tried again on the next run.
        """
        specs = [
            spec
            for spec in agent_config.auto_run_tools or []
            if bool(spec.get("every_run", False)) == every_run
        ]
        steps = plan_steps(specs)
        observer = getattr(run_context, "stream_observer", None) or self._stream_observer

        async def run_one(step: Step) -> Tuple[str, str]:
            auto_tool = step.spec
            tool_name = auto_tool.get("name")
            tool_params = self._substitute_tool_params(
                dict(auto_tool.get("parameters", {})), working_dir, user_message
            )
            target_tool = next(
                (t for t in tools if getattr(t, "name", "") == tool_name), None
            ) or self._unlisted_auto_run_tool(tool_name)
            if target_tool and hasattr(target_tool, "on_invoke_tool"):
                logger.info(
                    f"Auto-running tool '{tool_name}' for agent '{agent_key}' (cwd={working_dir}, every_run={every_run})"
                )
                try:
                    if hasattr(observer, "tool_call_started"):
                        observer.tool_call_started(
                            agent_key, tool_name, tool_params
                        )
                    tool_ctx_wrapper = AutoRunToolContext(
                        run_context, tool_name=tool_name, operator_configured=True
                    )
                    tool_result = await target_tool.on_invoke_tool(
                        tool_ctx_wrapper, json.dumps(tool_params)
                    )
                    if hasattr(observer, "tool_call_finished"):
                        observer.tool_call_finished(
                            agent_key, tool_name, tool_result
                        )
                    # Log the full result of the auto-run tool call in verbose mode
                    Logger("agent_factory").log_verbose(
                        f"AUTO-RUN TOOL RESULT: {tool_name}",
                        (
                            tool_result
                            if isinstance(tool_result, str)
                            else str(tool_result)
                        ),
                    )
                    if is_policy_block(tool_result):
                        logger.warning(
                            f"⚠️ Auto-run tool '{tool_name}' blocked by action policy"
                        )
                        return "", BLOCKED
                    reason = failure_reason(tool_result)
                    if reason:
                        # A setup step that reports its own failure is not
                        # context: injected, it reads as a successful result.
                        logger.warning(
                            f"⚠️ Auto-run tool '{tool_name}' did not succeed: {reason}"
                        )
                        raise StepDidNotSucceed(reason)
                    logger.debug(
                        f"Injected auto-run result of '{tool_name}' into instructions"
                    )
                    return (
                        f"\n\n=== AUTO-RUN TOOL RESULT '{tool_name}' ===\n{tool_result}\n",
                        DONE,
                    )
                except StepDidNotSucceed:
                    return "", FAILED
                except Exception as tool_err:
                    logger.warning(
                        f"⚠️ Error in auto-run tool '{tool_name}': {tool_err}"
                    )
                    if hasattr(observer, "tool_call_finished"):
                        observer.tool_call_finished(
                            agent_key, tool_name, f"❌ {type(tool_err).__name__}: {tool_err}"
                        )
                    return "", FAILED
            logger.warning(
                f"⚠️ Auto-run tool '{tool_name}' of agent '{agent_key}' is not a function tool of this system"
            )
            if hasattr(observer, "tool_call_finished"):
                observer.tool_call_finished(
                    agent_key, tool_name,
                    f"❌ Auto-run tool '{tool_name}' is not a function tool of this system",
                )
            return "", FAILED

        started = time.perf_counter()
        outcomes = await run_steps(steps, run_one)
        self._log_preparation(
            f"auto_run_tools(every_run={every_run})", outcomes,
            time.perf_counter() - started, run_context,
        )
        result_parts = [outcome.value for outcome in outcomes if outcome.status == DONE]
        # Legacy meaning: a call the action policy blocked did not run, so a
        # one-time tool is tried again on the next request. A tool that raised
        # or is unknown counts as run, as it always did.
        complete = all(outcome.status != BLOCKED for outcome in outcomes)
        return "".join(result_parts), complete

    def _unlisted_auto_run_tool(self, tool_name: str) -> Any:
        """A function tool of the config that the agent does not list, built as
        its listed tools are (isolation, output limit); None for any other name."""
        try:
            if self.config.get_tool(tool_name).type != "function":
                return None
        except (ConfigError, AttributeError):  # an unknown name; a factory without a config
            return None
        return next(iter(self._resolve_function_tools([tool_name])), None)

    async def _resolve_init_tool_pool(
        self,
        init_tools: List[Dict[str, Any]],
        tools: list,
        *,
        agent_key: Optional[str] = None,
    ) -> list:
        """The tools explicit init_tools may call: only what the target was given.

        A dynamic agent is issued its tools by the caller, so its init_tools may
        use exactly those. A configured agent (``agent_key``) may also use the
        ``auto_run_tools`` its own config declares: those are its promised
        preparation, not an arbitrary addition.

        Nothing is resolved from the registry just because an init_tool names
        it. Doing so let a caller reach a tool the target was never issued
        (a reviewer could run a preview it does not have), so a name outside
        the pool is refused by :meth:`_execute_init_tools`, not added here.
        """
        pool = list(tools)
        if agent_key is None:
            return pool
        try:
            agent_config = self.config.get_agent(agent_key)
        except Exception:  # an unconfigured key is not a config agent
            return pool
        declared = [
            spec.get("name")
            for spec in (getattr(agent_config, "auto_run_tools", None) or [])
            if isinstance(spec, Mapping) and spec.get("name")
        ]
        known = {getattr(tool, "name", "") for tool in pool}
        extra_names = [name for name in dict.fromkeys(declared) if name not in known]
        if extra_names:
            extra_tools, _, _ = await self._resolve_tools_for_names(extra_names)
            pool.extend(extra_tools)
        return pool

    async def _execute_init_tools(
        self,
        init_tools: List[Dict[str, Any]],
        tools: list,
        run_context: "GridRunContext",
    ) -> PreparationResult:
        """
        Run a list of raw tool-spec dicts and return concatenated results.

        Mirrors _execute_auto_run_tools() but accepts plain dicts instead of
        an AgentConfig, making it suitable for dynamic (orchestrated) agents.

        The result is the injected text *and* its typed verdict (PreparationResult):
        explicit init_tools are required, so a caller starts the model only when
        ``result.ok`` is true and otherwise reports ``result.failure_report()``.

        Args:
            init_tools: List of {"name": "tool_name", "parameters": {...}}.
            tools:      Resolved tool objects the agent has access to.
            run_context: GridRunContext used for tool invocation.

        Returns:
            PreparationResult: text for the instructions, plus per-step status.
        """
        observer = getattr(run_context, "stream_observer", None) or self._stream_observer
        # A container's workspace is CONTAINER_WORKDIR, not "/": an init tool
        # told its directory is the filesystem root indexes the whole container
        # instead of the workspace the agent was given.
        working_dir = (
            CONTAINER_WORKDIR
            if getattr(self, "container_id", None)
            else self.config.get_working_directory()
        )
        # Named once, before the loop: a tool that is not found reports under
        # it too (it used to be unbound there and failed every init tool).
        agent_key = (
            getattr(run_context, "agent_key", None)
            or getattr(run_context, "agent_name", None)
            or getattr(run_context, "agent_id", None)
            or getattr(getattr(run_context, "context", None), "agent_key", None)
            or "dynamic-agent"
        )
        steps = plan_steps(init_tools)

        async def run_one(step: Step) -> Tuple[str, str]:
            spec = step.spec
            tool_name = spec.get("name")
            tool_params = self._substitute_tool_params(
                dict(spec.get("parameters", {})), working_dir, ""
            )
            target_tool = next(
                (t for t in tools if getattr(t, "name", "") == tool_name), None
            )
            if target_tool and hasattr(target_tool, "on_invoke_tool"):
                logger.info(f"init_tool: running '{tool_name}' for dynamic agent")
                try:
                    if hasattr(observer, "tool_call_started"):
                        observer.tool_call_started(
                            agent_key, tool_name, tool_params
                        )
                    wrapper = AutoRunToolContext(run_context, tool_name=tool_name)
                    result = await target_tool.on_invoke_tool(
                        wrapper, json.dumps(tool_params)
                    )
                    if hasattr(observer, "tool_call_finished"):
                        observer.tool_call_finished(
                            agent_key, tool_name, result
                        )
                    # Log the full result of the init tool call in verbose mode
                    Logger("agent_factory").log_verbose(
                        f"INIT TOOL RESULT: {tool_name}",
                        result if isinstance(result, str) else str(result),
                    )
                    if is_policy_block(result):
                        # A refusal is not context: injected into the
                        # instructions it only misleads the agent.
                        logger.warning(
                            f"⚠️ init_tool '{tool_name}' blocked by action policy"
                        )
                        return "", BLOCKED
                    reason = failure_reason(result)
                    if reason:
                        logger.warning(
                            f"⚠️ init_tool '{tool_name}' did not succeed: {reason}"
                        )
                        raise StepDidNotSucceed(reason)
                    return f"\n\n=== CONTEXT [{tool_name}] ===\n{result}\n", DONE
                except StepDidNotSucceed:
                    raise
                except Exception as e:
                    # Shown to the run's view and kept for the caller: the
                    # batch records the real error, not an empty "failed".
                    logger.warning(f"⚠️ init_tool '{tool_name}' error: {e}")
                    if hasattr(observer, "tool_call_finished"):
                        observer.tool_call_finished(
                            agent_key, tool_name, f"❌ {type(e).__name__}: {e}"
                        )
                    raise
            logger.warning(
                f"⚠️ init_tool '{tool_name}' not found in agent's tool list"
            )
            if hasattr(observer, "tool_call_finished"):
                observer.tool_call_finished(
                    agent_key, tool_name,
                    f"❌ init_tool '{tool_name}' not found in the agent's tool list",
                )
            raise StepDidNotSucceed(
                f"tool '{tool_name}' is not available to this agent"
            )

        started = time.perf_counter()
        outcomes = await run_steps(steps, run_one)
        self._log_preparation(
            "init_tools", outcomes, time.perf_counter() - started, run_context
        )
        return PreparationResult(
            "".join(outcome.value for outcome in outcomes if outcome.status == DONE),
            outcomes,
            phase="init_tools",
            required=True,
        )

    def _log_preparation(
        self,
        phase: str,
        outcomes: List[StepOutcome],
        wall_seconds: float,
        run_context: Any,
    ) -> None:
        """Record what the preparation cost: per step, and the batch's total.

        The serial sum beside the wall time is what dependency-aware
        concurrency saved; the record also goes to the run's metadata, where
        a caller (and the tests) can read it.
        """
        if not outcomes:
            return
        record = timings(outcomes, wall_seconds)
        mode = "parallel" if is_concurrent([o.step for o in outcomes]) else "sequential"
        logger.info(
            "preparation: %s %s, %d step(s) in %.0f ms (serial %.0f ms): %s",
            phase,
            mode,
            len(outcomes),
            record["wall_ms"],
            record["serial_ms"],
            ", ".join(
                f"{entry['name']}={entry['status']} {entry['seconds'] * 1000:.0f}ms"
                + (f" ({entry['detail']})" if entry["detail"] else "")
                for entry in record["steps"]
            ),
        )
        metadata = getattr(run_context, "metadata", None)
        if isinstance(metadata, dict):
            metadata.setdefault("preparation_timings", []).append(
                {"phase": phase, "mode": mode, **record}
            )

    async def _auto_run_preamble(
        self,
        agent_key: str,
        agent_config: AgentConfig,
        agent: Agent,
        run_ctx: "GridRunContext",
        user_message: str,
    ) -> str:
        """Run the agent's auto_run_tools and return their output for its input.

        One-time tools run once per user and agent; per-message tools on every
        request. A failing tool is logged and left out; the request still runs.
        """
        if not agent_config.auto_run_tools:
            return ""
        # A container's workspace is CONTAINER_WORKDIR, not "/": an agent told
        # its directory is the filesystem root indexes and lists the whole
        # container instead of the workspace it is meant to work in.
        working_dir = CONTAINER_WORKDIR if self.container_id else self.config.get_working_directory()
        init_key = f"{agent_key}:{run_ctx.user_id or 'default'}"
        parts: List[str] = []
        phases = [(False, init_key not in self._initialized_agents), (True, True)]
        for every_run, due in phases:
            if not due:
                continue
            try:
                info, complete = await self._execute_auto_run_tools(
                    agent_key,
                    agent_config,
                    list(agent.tools or []),
                    working_dir,
                    run_ctx,
                    every_run=every_run,
                    user_message=user_message,
                )
            except Exception:
                logger.warning(
                    "auto_run_tools of %s failed (every_run=%s)", agent_key, every_run, exc_info=True
                )
                continue
            if info:
                parts.append(info)
            if not every_run and complete:
                self._initialized_agents.add(init_key)
        # Per-message output first: it is the most recent state.
        return "\n\n".join(reversed(parts))
