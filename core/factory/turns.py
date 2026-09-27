"""A turn of a conversation: its runs, retries, stops, resumption and compaction.

One request and its answer (run_agent, continue_agent): the request is
stored, the agent runs with retries on passing failures, a graceful Stop or a
failure leaves an interruption the next turn resumes, messages sent meanwhile
are steered in, the session is compacted when it outgrows the window, and a
conversation can fork before an edited message. Relies on the whole factory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from agents import (
    Agent,
    RunConfig,
    RunItemStreamEvent,
    SQLiteSession,
)
from agents.exceptions import (
    MaxTurnsExceeded,
    ModelBehaviorError,
)
from agents.exceptions import (
    UserError as AgentsUserError,
)
from agents.run import CallModelData, ModelInputData

from core.action_policy import ActionRunState
from core.agent_input import AgentInput, context_content, parse_agent_input
from core.compact import (
    compact_conversation,
    get_auto_compact_threshold,
    is_prompt_too_long_error,
)
from core.context_budget import (
    context_budget_filter,
    request_tokens,
    session_transcript,
)
from core.factory.failures import (
    is_retriable,
    retry_backoff_seconds,
    writes_tool_calls_as_text,
)
from core.factory.journal import safe_preview
from core.factory.run_context import (
    GridRunContext,
    _get_runner,
    _RunProgress,
    _TurnStopped,
    with_images,
)
from core.generated_images import ImageCollector, collecting, generated_so_far
from core.image_window import limit_images
from core.interruption import (
    CONTINUATION_TYPE,
    CONTINUE_TEXT,
    Interruption,
    RunControl,
    StopReason,
    completed_names,
)
from core.run_stream import (
    append_action_reasoning,
    interrupted_run_report,
    last_message_text,
    run_output_text,
    tool_event_info,
)
from core.steering import Steering, SteerMessage
from schemas import AgentExecution
from utils.exceptions import AgentError, ContextError
from utils.logger import Logger
from utils.path_utils import reset_current_factory, set_current_factory

logger = logging.getLogger("grid.agent_factory")


# Sent after an answer that wrote tool calls as text instead of calling tools.
TOOL_CALL_CORRECTION = """Your last answer wrote tool calls as text, for example:
<tool_call><function=function_name><parameter=parameter_name>value</parameter></function></tool_call>

Text like that runs nothing. Call the tools themselves, then answer.
Repeat the last step that way."""

# Input of an attempt rerun after an overflowing session was summarized: the
# summary already holds the request and what was done for it.
OVERFLOW_RETRY_NOTE = (
    "[The conversation was compacted into the summary above to fit the context "
    "window.] Continue the current request from where you stopped; do not redo "
    "steps that already succeeded."
)


class TurnRunner:
    """A turn of a conversation: its runs, retries, stops, resumption and compaction.

    One request and its answer (run_agent, continue_agent): the request is
    stored, the agent runs with retries on passing failures, a graceful Stop or a
    failure leaves an interruption the next turn resumes, messages sent meanwhile
    are steered in, the session is compacted when it outgrows the window, and a
    conversation can fork before an edited message. Relies on the whole factory.
    """

    MALFORMED_TOOL_CALL_RETRIES = 2

    async def _consume_stream(
        self,
        result: Any,
        *,
        observer: Any,
        agent_key: str,
        action_state: Any,
        on_event: Optional[Callable[[Any], None]] = None,
    ) -> List[str]:
        """Feed a streamed run to its observer; return the text fragments it rendered.

        Rendering and bookkeeping never stop a run: their failures are logged.
        """
        fragments: List[str] = []
        async for event in result.stream_events():
            append_action_reasoning(action_state, event)
            try:
                if on_event is not None:
                    on_event(event)
                fragment = observer.handle_event(event, agent_key=agent_key)
            except Exception:
                logger.exception("Stream observer failed for %s", agent_key)
                continue
            if fragment:
                fragments.append(fragment)
        return fragments

    def _run_config(
        self,
        agent_key: Optional[str] = None,
        *,
        steering: Optional[Steering] = None,
        session: Optional[SQLiteSession] = None,
    ) -> RunConfig:
        """What every model call of a run of *agent_key* passes through.

        First the messages the user sent while the turn runs (core.steering;
        only for the top-level run of a turn, which passes its ``steering`` and
        ``session``), then the image budget (core.image_window), then the
        context budget (core.context_budget): old compactable tool outputs are
        cleared from the request once it would pass the auto-compact threshold.
        """
        max_images = self.config.config.settings.image_processing.max_images_per_request
        clear_outputs = context_budget_filter(
            self._context_window(agent_key), self.compact_config
        )

        def budget(items: List[Any], instructions: Optional[str]) -> ModelInputData:
            items = limit_images(items, max_images)
            if clear_outputs is not None:
                items = clear_outputs(items, instructions)
            return ModelInputData(input=items, instructions=instructions)

        if steering is None:
            def apply(data: CallModelData) -> ModelInputData:
                return budget(data.model_data.input, data.model_data.instructions)
        else:
            async def apply(data: CallModelData) -> ModelInputData:
                items = await steering.apply(data.model_data.input, session)
                return budget(items, data.model_data.instructions)

        return RunConfig(call_model_input_filter=apply)

    @staticmethod
    def _final_text(result: Any, fragments: List[str]) -> str:
        """The answer of a finished run: its final output, else what it streamed.

        An answer that is only generated images has no text, and says nothing
        about a missing report.
        """
        final = getattr(result, "final_output", None)
        if final is not None and str(final).strip():
            return str(final)
        streamed = "".join(fragments).strip()
        if streamed or generated_so_far():
            return streamed
        return str(run_output_text(result))

    async def run_agent_object_simple(
        self,
        agent: Any,
        input_message: str,
        context_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        stream_observer: Optional[Any] = None,
        action_state: Optional[Any] = None,
        action_depth: int = 0,
    ) -> str:
        """Run an Agent instance in its own session and return its answer text.

        Used for dynamic and background agents. The run has its own SDK session
        (agent name + context id) and never reads or changes the factory's
        conversation history.

        ``stream_observer`` is the view the run reports into - the caller's, so a
        dynamic agent shows up in the trace of the turn that launched it rather
        than on the server console. Defaults to the factory's own observer.

        ``action_state`` is the policy state of a run started by another agent
        (see ``core.action_policy.delegated_state``). Without it the run is its own
        task: the input message becomes the trusted instruction, which is right
        only for callers that speak for the user (background workers, the CLI).
        """
        observer = stream_observer or self._stream_observer
        agent_label = getattr(agent, "name", None) or "dynamic-agent"
        context_id = context_id or self.context_manager.get_current_context_id()
        session = self._get_agent_session(agent_label, context_id)
        if action_state is None:
            action_state = self._action_state(self._policy_task(input_message, context_id))
            if action_state is not None and hasattr(observer, "handle_policy_event"):
                action_state.policy_event = observer.handle_policy_event
        run_ctx = GridRunContext(
            factory=self,
            context_id=context_id,
            session=session,
            pipeline_id=pipeline_id,
            action_state=action_state,
            action_depth=action_depth,
            # Agents this one delegates to report into the same view.
            stream_observer=stream_observer,
        )

        attempt = 0
        set_current_factory(self)
        try:
            while True:
                result = _get_runner().run_streamed(
                    starting_agent=agent,
                    input=input_message,
                    context=run_ctx,
                    session=session,
                    max_turns=self.config.get_max_turns(),
                    run_config=self._run_config(None),
                )
                try:
                    fragments = await self._consume_stream(
                        result,
                        observer=observer,
                        agent_key=agent_label,
                        action_state=action_state,
                    )
                except (MaxTurnsExceeded, ModelBehaviorError, AgentsUserError) as exc:
                    # Not transient: retrying would repeat the work. The caller
                    # gets what the agent did so far.
                    logger.warning("Agent %s stopped: %s: %s", agent_label, type(exc).__name__, exc)
                    return interrupted_run_report(result, f"Agent {agent_label}", exc)
                except Exception as exc:
                    if not is_retriable(exc):
                        raise
                    attempt += 1
                    delay = retry_backoff_seconds(attempt)
                    logger.warning(
                        "Retriable failure of agent %s (attempt %d, retry in %.1fs): %s",
                        agent_label,
                        attempt,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                return self._final_text(result, fragments)
        finally:
            reset_current_factory()

    def _open_context(self, context_id: Optional[str], use_active_context: bool) -> str:
        """The conversation this request belongs to: named, active, or new."""
        try:
            if context_id:
                return self.context_manager.activate_context(context_id)
            if use_active_context and self.context_manager.get_current_context_id():
                return self.context_manager.get_current_context_id()
            return self.context_manager.start_new_context()
        except ContextError as exc:
            raise AgentError("Failed to prepare conversation context") from exc

    def _record_invocation(
        self, agent_key: str, context_id: str, user_id: Optional[str], message: str
    ) -> Optional[Any]:
        """Note the invocation; with agent logging on, start this turn's session
        log and return its token for Logger.deactivate_session_log."""
        self.context_manager.set_metadata("context_id", context_id)
        self.context_manager.set_metadata(
            "last_invocation", {"agent": agent_key, "timestamp": time.time()}
        )
        if user_id:
            self.context_manager.set_metadata("user_id", user_id)
        agent_logging = self.config.config.settings.agent_logging
        if agent_logging is None or not agent_logging.enabled:
            return None
        # This factory's own settings travel with the turn: another turn -
        # another user's, on a web server - logs to its own directory at once.
        token = Logger.activate_session_log(
            context_id,
            log_dir=str(self._logs_directory_path()),
            level=agent_logging.level,
            enabled=True,
        )
        if agent_logging.save_conversations and message:
            Logger("agent_factory").log_verbose(f"USER INPUT: {agent_key}", message)
        return token

    def _prepare_instructions(
        self,
        agent_key: str,
        context_path: Optional[str],
        include_transcript: bool,
    ) -> str:
        """Assemble the agent's instructions and record exactly what was sent."""
        assembly = self.instructions_builder.assemble_model_context(
            agent_key,
            context_path,
            include_conversation_context=include_transcript,
            include_path_context=True,
        )
        instructions = assembly.instructions
        self.context_manager.set_metadata("last_context_assembly", assembly.to_debug_payload())
        if not self.context_manager.get_metadata("agent_instructions"):
            # Shown by the context inspector.
            self.context_manager.set_metadata("agent_instructions", instructions)
        if agent_key not in self._logged_agents:
            Logger("agent_factory").log_verbose(f"FULL PROMPT STARTUP: {agent_key}", instructions)
            self._logged_agents.add(agent_key)
        return instructions

    def _add_user_message(
        self,
        message: str,
        agent_input: AgentInput,
        agent_key: str,
        context_id: str,
        turn_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        content = (
            message if agent_input.is_text else context_content(agent_input.items[0])
        )
        self.context_manager.append_message_to(
            context_id,
            "user",
            content,
            metadata={
                **(extra or {}),
                "context_id": context_id,
                "agent": agent_key,
                "type": "user_input",
                "turn_id": turn_id,
            },
        )

    def _session_epoch(self, context_id: str, agent_key: str) -> int:
        """How many times *agent_key*'s session in *context_id* was replaced by a summary."""
        epochs = self.context_manager.get_context_metadata(context_id).get("session_epochs") or {}
        return int(epochs.get(agent_key, 0))

    def _bump_session_epoch(self, context_id: str, agent_key: str) -> None:
        epochs = dict(self.context_manager.get_context_metadata(context_id).get("session_epochs") or {})
        epochs[agent_key] = int(epochs.get(agent_key, 0)) + 1
        self.context_manager.update_context_metadata(context_id, {"session_epochs": epochs})

    async def fork_conversation(self, context_id: str, message_id: str) -> str:
        """Branch *context_id* at user message *message_id*, for an edited version of it.

        The branch holds the messages before it (ContextManager.fork_context)
        and, for each agent that worked in the conversation, its session up to
        the point where the turn of that message started - so the agent in the
        branch remembers every step before the edit, tool calls included, and
        nothing after. The cut is the turn's ``session_mark``. When the session
        was summarized since that mark, no exact cut exists: that agent's
        session in the branch starts empty and it reads the copied messages as a
        transcript instead. Returns the branch id.
        """
        branch_id, _ = self.context_manager.fork_context(context_id, message_id)
        messages = self.context_manager.conversation_view(context_id)["messages"]
        index = next(i for i, m in enumerate(messages) if (m.metadata or {}).get("message_id") == message_id)
        epochs = self.context_manager.get_context_metadata(context_id).get("session_epochs") or {}
        branch_epochs = dict(epochs)
        agents = {(m.metadata or {}).get("agent") for m in messages} - {None}
        for agent in sorted(agents):
            items = await self._get_agent_session(agent, context_id).get_items()
            if not items:
                continue
            # The first turn of this agent at or after the edit point.
            mark = next(
                (
                    (m.metadata or {})["session_mark"]
                    for m in messages[index:]
                    if ((m.metadata or {}).get("session_mark") or {}).get("agent") == agent
                ),
                None,
            )
            if mark is None:
                cut = len(items)  # the agent did nothing after the edit point
            elif int(mark.get("epoch", 0)) == int(epochs.get(agent, 0)) and mark["items"] <= len(items):
                cut = mark["items"]
            else:
                # Summarized since: the copied marks of this agent no longer apply.
                branch_epochs[agent] = int(epochs.get(agent, 0)) + 1
                logger.info("Branch %s: %s starts from the transcript (session was compacted)", branch_id, agent)
                continue
            if cut:
                await self._get_agent_session(agent, branch_id).add_items(items[:cut])
        self.context_manager.update_context_metadata(branch_id, {"session_epochs": branch_epochs})
        return branch_id

    async def context_usage(self, agent_key: str, context_id: Optional[str] = None) -> Dict[str, int]:
        """How full *agent_key*'s context is in a conversation: tokens, window, threshold.

        Measured on the agent's SDK session - what the model reads - not on the
        stored chat, which holds only the visible text.
        """
        context_id = context_id or self.get_active_context_id()
        items = await self._get_agent_session(agent_key, context_id).get_items()
        window = self._context_window(agent_key)
        return {
            "tokens": request_tokens(items),
            "window": window,
            "threshold": get_auto_compact_threshold(window, self.compact_config),
        }

    async def compact_session(
        self, agent_key: str, context_id: str, *, force: bool = False
    ) -> Optional[Dict[str, int]]:
        """Summarize *agent_key*'s session in a conversation into one message.

        Without ``force`` only when the session is past the auto-compact
        threshold (core.context_budget) and compaction has not failed
        ``compact.auto.max_consecutive_failures`` times in a row. The compaction
        model reads the session - messages, tool calls and their results - and
        the session is replaced by its summary. The stored chat users see is not
        touched. Returns ``{"tokens_before", "tokens_after"}``, or None when
        nothing was compacted; a failure is logged, never raised.
        """
        compact_cfg = self.compact_config
        if not compact_cfg.enabled or not (force or compact_cfg.auto.enabled):
            return None
        session = self._get_agent_session(agent_key, context_id)
        items = await session.get_items()
        if not items:
            return None
        tokens_before = request_tokens(items)
        window = self._context_window(agent_key)
        if not force:
            if tokens_before <= get_auto_compact_threshold(window, compact_cfg):
                return None
            if self._compact_tracking.consecutive_failures >= compact_cfg.auto.max_consecutive_failures:
                logger.warning(
                    "Auto-compact of %s skipped after %d failures in a row",
                    agent_key,
                    self._compact_tracking.consecutive_failures,
                )
                return None
        logger.info("Compacting the session of %s: ~%d/%d tokens", agent_key, tokens_before, window)
        try:
            client, model = self._get_compact_client_and_model(agent_key)
            result = await compact_conversation(
                messages=session_transcript(items),
                llm_client=client,
                model=model,
                suppress_followup_questions=True,
                is_auto_compact=not force,
                max_output_tokens=compact_cfg.summary_max_output_tokens,
                compact_cfg=compact_cfg,
            )
        except Exception:
            self._compact_tracking.consecutive_failures += 1
            logger.warning("Compacting the session of %s failed", agent_key, exc_info=True)
            return None
        summary = [
            {"role": message.role, "content": message.get_text()}
            for message in result.summary_messages
            if message.get_text().strip()
        ]
        if not result.success() or not summary:
            self._compact_tracking.consecutive_failures += 1
            logger.warning(
                "Compacting the session of %s produced no summary: %s",
                agent_key,
                result.user_display_message or result.error_message or result.status.value,
            )
            return None
        self._compact_tracking.consecutive_failures = 0
        await session.clear_session()
        await session.add_items(summary)
        # Turn marks taken before this point no longer count items of this session.
        self._bump_session_epoch(context_id, agent_key)
        tokens_after = request_tokens(summary)
        logger.info("Compacted the session of %s: ~%d -> ~%d tokens", agent_key, tokens_before, tokens_after)
        return {"tokens_before": tokens_before, "tokens_after": tokens_after}

    async def _run_attempt(
        self,
        agent: Agent,
        agent_key: str,
        run_input: Union[str, List[Any]],
        run_ctx: "GridRunContext",
        session: Optional[SQLiteSession],
        *,
        stream: bool,
        observer: Any,
        progress: "_RunProgress",
    ) -> Tuple[str, Any]:
        """One run of the agent. Returns its answer text and the SDK result.

        A run that ends without an answer and without failing raises
        _TurnStopped - settings.agent_timeout, max_turns, a model-side error
        that a retry would repeat, the user's graceful Stop. Other errors
        propagate for _run_with_retries to judge.
        """
        timeout = self.config.get_agent_timeout()
        max_turns = self.config.get_max_turns()
        control = run_ctx.run_control
        fragments: List[str] = []
        result: Any = None

        def record_tool_event(event: Any) -> None:
            if not isinstance(event, RunItemStreamEvent):
                return
            if event.name not in ("tool_called", "tool_output"):
                return
            info = tool_event_info(event.item)
            tool_name = info.get("tool_name") or "tool"
            if event.name == "tool_called":
                progress.ledger.called(info.get("call_id"), tool_name, info.get("arguments"))
            else:
                progress.ledger.returned(info.get("call_id"), tool_name)
            self.journal.record_event(
                event_type=event.name,
                tool_name=tool_name,
                arguments=info.get("arguments") if event.name == "tool_called" else None,
                output=info.get("output") if event.name == "tool_output" else None,
                extra={"retry_count": progress.attempt, "call_id": info.get("call_id")},
                persist=event.name == "tool_called",
            )

        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                if stream:
                    if hasattr(observer, "reasoning_text"):
                        observer.reasoning_text = ""
                        observer._reasoning_buf = []
                    steering = control.steering if control is not None else None
                    if steering is not None:
                        steering.new_run()
                    result = progress.result = _get_runner().run_streamed(
                        agent,
                        run_input,
                        context=run_ctx,
                        max_turns=max_turns,
                        session=session,
                        run_config=self._run_config(agent_key, steering=steering, session=session),
                    )
                    if control is not None:
                        control.attach(result)
                    try:
                        fragments = await self._consume_stream(
                            result,
                            observer=observer,
                            agent_key=agent_key,
                            action_state=run_ctx.action_state,
                            on_event=record_tool_event,
                        )
                    finally:
                        if control is not None:
                            control.detach(result)
                else:
                    result = progress.result = await _get_runner().run(
                        agent,
                        run_input,
                        context=run_ctx,
                        max_turns=max_turns,
                        session=session,
                        run_config=self._run_config(agent_key),
                    )
        except TimeoutError:
            if not deadline.expired():
                raise
            logger.warning("Agent %s stopped after %s s", agent_key, timeout)
            raise _TurnStopped(
                StopReason.TIMEOUT, f"no answer within settings.agent_timeout ({timeout} s)"
            ) from None
        except MaxTurnsExceeded:
            logger.warning("Agent %s reached max_turns (%s)", agent_key, max_turns)
            raise _TurnStopped(
                StopReason.MAX_TURNS, f"settings.max_turns is {max_turns}"
            ) from None
        except (ModelBehaviorError, AgentsUserError) as exc:
            logger.warning("Agent %s stopped: %s: %s", agent_key, type(exc).__name__, exc)
            raise _TurnStopped(StopReason.ERROR, f"{type(exc).__name__}: {exc}") from None
        # A graceful Stop ends the stream after a step. If that step was the
        # final answer, the turn is simply done.
        if control is not None and control.stop_requested and result.final_output is None:
            raise _TurnStopped(StopReason.USER_STOP)
        return self._final_text(result, fragments), result

    async def _run_with_retries(
        self,
        agent: Agent,
        agent_key: str,
        run_input: Union[str, List[Any]],
        run_ctx: "GridRunContext",
        session: Optional[SQLiteSession],
        *,
        stream: bool,
        observer: Any,
        progress: "_RunProgress",
    ) -> Tuple[str, Any]:
        """Run until an answer: transient provider errors are retried with backoff.

        A context overflow is answered once by trimming the oldest history.

        A rerun never sends the request twice. The SDK stores the input in the
        session when a run starts, and each finished step after it; so once the
        session has moved, the rerun's input is a note to continue from there
        (Interruption.resume_input) naming the calls the failed attempt left in
        flight. When the session did not move, the request is sent again.
        """
        control = run_ctx.run_control
        trimmed = False
        set_current_factory(self)
        try:
            while True:
                if control is not None and control.stop_requested:
                    raise _TurnStopped(StopReason.USER_STOP)
                self.journal.update_pending_run(
                    agent_key=agent_key,
                    active_context_id=run_ctx.context_id,
                    input_preview=progress.input_preview,
                    status="running",
                    retry_count=progress.attempt,
                )
                self.journal.record_event(
                    event_type="attempt_started", extra={"retry_count": progress.attempt}
                )
                session_mark = await self._session_mark(session)
                try:
                    return await self._run_attempt(
                        agent,
                        agent_key,
                        run_input,
                        run_ctx,
                        session,
                        stream=stream,
                        observer=observer,
                        progress=progress,
                    )
                except _TurnStopped:
                    raise
                except Exception as exc:
                    if not trimmed and is_prompt_too_long_error(exc) and session is not None:
                        trimmed = True
                        # Whether the failed attempt stored the request: then
                        # the summary holds it, else it is sent again.
                        stored_request = await self._session_mark(session) != session_mark
                        if await self.compact_session(agent_key, run_ctx.context_id, force=True):
                            progress.ledger.forget_open()
                            if stored_request:
                                run_input = OVERFLOW_RETRY_NOTE
                            continue
                    retriable = is_retriable(exc)
                    error_text = safe_preview(str(exc), max_length=700)
                    self.journal.update_pending_run(
                        agent_key=agent_key,
                        active_context_id=run_ctx.context_id,
                        input_preview=progress.input_preview,
                        status="retrying" if retriable else "failed",
                        retry_count=progress.attempt,
                        last_error=error_text,
                    )
                    self.journal.record_event(
                        event_type="attempt_error",
                        output=error_text,
                        extra={
                            "retry_count": progress.attempt,
                            "retriable": retriable,
                            "exception_type": type(exc).__name__,
                        },
                    )
                    if not retriable:
                        progress.recorded_failure = True
                        raise AgentError(f"Agent execution failed: {exc}") from exc
                    if await self._session_mark(session) != session_mark:
                        run_input = Interruption(
                            reason=StopReason.ERROR,
                            task="",
                            agent=agent_key,
                            detail=f"{type(exc).__name__}: {error_text}",
                            in_flight=progress.ledger.in_flight,
                        ).resume_input()
                    progress.ledger.forget_open()
                    progress.attempt += 1
                    delay = retry_backoff_seconds(progress.attempt)
                    logger.warning(
                        "Retriable failure of agent %s (attempt %d, retry in %.1fs): %s",
                        agent_key,
                        progress.attempt,
                        delay,
                        exc,
                        exc_info=exc,
                    )
                    # Stop ends the wait for the provider, too.
                    if control is not None:
                        if await control.wait(delay):
                            raise _TurnStopped(StopReason.USER_STOP)
                    else:
                        await asyncio.sleep(delay)
        finally:
            reset_current_factory()

    @staticmethod
    async def _session_mark(session: Optional[SQLiteSession]) -> Any:
        """The newest item of *session*: it changes whenever the SDK stores one."""
        if session is None:
            return None
        items = await session.get_items(limit=1)
        return items[-1] if items else None

    async def run_agent(
        self,
        agent_key: str,
        message: str,
        context_path: Optional[str] = None,
        context_id: Optional[str] = None,
        *,
        stream: bool = False,
        use_active_context: bool = False,
        user_id: Optional[str] = None,
        stream_observer: Optional[Any] = None,
        turn_id: Optional[str] = None,
        edit_of: Optional[str] = None,
    ) -> str:
        """Run an agent on a message within a conversation and return its answer.

        Args:
            agent_key: Agent to run
            message: The user's message: text, or a JSON SDK message with images
            context_path: Optional context path
            context_id: Conversation to continue; a new one when omitted
            stream: Stream events to the observer while the agent runs
            use_active_context: Continue the active conversation when no id is given
            user_id: User the run acts for (workspace isolation)
            stream_observer: View the run reports into; the factory's by default
            turn_id: Caller's id for this turn, stored on its messages
            edit_of: The slot of the message this one is a new version of, in a
                branch made by fork_conversation (ContextManager.message_versions)

        The answer ends with the line ``Context ID: <id>``. An answer written as
        text tool calls (``<tool_call><function=...>``) is retried with a
        correction, up to MALFORMED_TOOL_CALL_RETRIES times.

        When the conversation ends with an interrupted turn, this message resumes
        it (see core.interruption). A turn that stops early answers with the
        summary of its interruption - or, for an error or a cancelled task,
        raises after recording it.
        """
        return await self._run_turn_with_corrections(
            agent_key,
            message,
            context_path,
            context_id,
            stream=stream,
            use_active_context=use_active_context,
            user_id=user_id,
            stream_observer=stream_observer,
            turn_id=turn_id,
            edit_of=edit_of,
        )

    async def continue_agent(
        self,
        agent_key: str,
        context_id: str,
        *,
        context_path: Optional[str] = None,
        stream: bool = False,
        user_id: Optional[str] = None,
        stream_observer: Optional[Any] = None,
        turn_id: Optional[str] = None,
    ) -> str:
        """Continue the interrupted turn *context_id* ends with - the Continue button.

        The agent resumes the same request from its session, told why it
        stopped and which tool calls were in flight. Raises AgentError when the
        conversation has no interruption left to continue.
        """
        return await self._run_turn_with_corrections(
            agent_key,
            None,
            context_path,
            context_id,
            stream=stream,
            user_id=user_id,
            stream_observer=stream_observer,
            turn_id=turn_id,
        )

    def steer(self, context_id: str, message: SteerMessage) -> bool:
        """Give *message* to the turn running in *context_id*, for its next step.

        The agent reads it before its next model call without stopping
        (core.steering), and it is stored in the conversation as the user's.
        Returns False when no streamed turn of this conversation runs here.
        If the turn ends before a next call, ``message.on_undelivered`` gets it
        back.
        """
        control = self._run_controls.get(context_id)
        if control is None:
            return False
        reported = message.on_delivered

        def delivered(steer: SteerMessage) -> None:
            # The user's words widen the task the policy gate judges against.
            state = control.action_state
            if isinstance(state, ActionRunState):
                state.task = f"{state.task}\n\nUser instruction added during the task:\n{steer.text}"
            self.context_manager.append_message_to(
                context_id,
                "user",
                steer.text,
                metadata={"context_id": context_id, "type": "steer", "steer_id": steer.message_id},
            )
            if reported is not None:
                reported(steer)

        message.on_delivered = delivered
        control.steering.add(message)
        return True

    def request_stop(self, context_id: str) -> bool:
        """Ask the turn running in *context_id* to stop after its current step.

        The step finishes - the model's response and the tool calls it made -
        and is saved; then the turn ends with a Stop interruption that Continue
        picks up. Returns False when no streamed turn of this conversation is
        running here; the caller can only cancel it then.
        """
        control = self._run_controls.get(context_id)
        if control is None:
            return False
        control.request_stop()
        return True

    async def _run_turn_with_corrections(
        self,
        agent_key: str,
        message: Optional[str],
        context_path: Optional[str],
        context_id: Optional[str],
        **options: Any,
    ) -> str:
        output, context_id, answered = await self._run_turn(
            agent_key, message, context_path, context_id, **options
        )
        options.pop("use_active_context", None)
        options.pop("edit_of", None)  # a correction is not another version
        for retry in range(1, self.MALFORMED_TOOL_CALL_RETRIES + 1):
            if not answered or not writes_tool_calls_as_text(output):
                break
            logger.warning(
                "Agent %s wrote tool calls as text; retrying with a correction (%d/%d)",
                agent_key,
                retry,
                self.MALFORMED_TOOL_CALL_RETRIES,
            )
            output, context_id, answered = await self._run_turn(
                agent_key, TOOL_CALL_CORRECTION, context_path, context_id, **options
            )
        return output

    def _turn_task(self, message: Optional[str], interrupted: Optional[Interruption]) -> str:
        """The user's request this turn works on, as the interruption record keeps it."""
        if message is None:
            return interrupted.task
        text = self._policy_message_text(message).strip()
        if interrupted is None:
            return text
        return f"{interrupted.task}\n\nThen the user wrote:\n{text}"

    @staticmethod
    def _resumed_input(
        agent_input: AgentInput, resumed: Interruption, message: Optional[str]
    ) -> Union[str, List[Any]]:
        """The model's input for a turn that resumes *resumed*."""
        if message is None:
            return resumed.resume_input()
        if agent_input.is_text:
            return resumed.resume_input(agent_input.items)
        # A message with images: the note goes in front of its own parts.
        first = dict(agent_input.items[0])
        note = {"type": "input_text", "text": resumed.resume_input("")}
        first["content"] = [note, *(first.get("content") or [])]
        return [first, *agent_input.items[1:]]

    def _record_interruption(
        self,
        *,
        context_id: str,
        agent_key: str,
        task: str,
        reason: StopReason,
        detail: str,
        progress: "_RunProgress",
        turn_id: Optional[str],
        images: Optional[List[str]] = None,
    ) -> Interruption:
        """Store why the turn stopped and what was in progress; return the record.

        Runs on every early end, including a cancelled task, so it never awaits
        and never raises: a failure to store is logged, and the caller's own
        outcome - an answer, an error, a cancellation - goes on.
        """
        items = list(getattr(progress.result, "new_items", None) or [])
        interruption = Interruption(
            reason=reason,
            task=task,
            agent=agent_key,
            detail=detail,
            in_flight=progress.ledger.in_flight,
            completed=progress.ledger.completed or completed_names(items),
            last_text=last_message_text(items),
        )
        try:
            self.context_manager.append_message_to(
                context_id,
                "assistant",
                with_images(interruption.summary(), images or []),
                interruption.message_metadata(context_id=context_id, turn_id=turn_id),
            )
            self.journal.update_pending_run(
                agent_key=agent_key,
                active_context_id=context_id,
                input_preview=progress.input_preview,
                status="interrupted",
                retry_count=progress.attempt,
                last_error=detail or None,
            )
            progress.recorded_failure = True
        except Exception:
            logger.exception("Could not record the interruption of %s in %s", agent_key, context_id)
        logger.warning(
            "Turn of %s in %s interrupted: %s %s", agent_key, context_id, reason.value, detail
        )
        return interruption

    async def _run_turn(
        self,
        agent_key: str,
        message: Optional[str],
        context_path: Optional[str],
        context_id: Optional[str],
        *,
        stream: bool,
        user_id: Optional[str],
        stream_observer: Optional[Any],
        use_active_context: bool = False,
        turn_id: Optional[str] = None,
        edit_of: Optional[str] = None,
    ) -> Tuple[str, str, bool]:
        """One request and answer of the conversation.

        Returns (answer, context id, answered): ``answered`` is False when the
        text is the summary of an interruption rather than the agent's answer.

        ``message`` None is Continue. Either way, when the conversation ends
        with an interruption this turn resumes it (core.interruption).

        Once the turn's request is stored, the conversation always says how the
        turn ended: its answer, or an interruption record - timeout, turn limit,
        model error, provider failure, Stop (graceful or a cancelled task).
        """
        observer = stream_observer or self._stream_observer
        shown_message = message if message is not None else CONTINUE_TEXT
        execution = AgentExecution(
            agent_name=agent_key, start_time=time.time(), input_message=shown_message
        )
        progress = _RunProgress(input_preview=safe_preview(shown_message, max_length=700))
        control = RunControl()
        # Images an image model generates during the turn: shown as they come,
        # stored with the answer (core.generated_images).
        generated = ImageCollector(on_image=getattr(observer, "handle_generated_image", None))
        active_context_id: Optional[str] = None
        task = ""
        stored = False  # the turn's request is in the conversation
        session_log = None
        try:
            active_context_id = self._open_context(context_id, use_active_context)
            execution.context_id = active_context_id
            continuing = context_id is not None or use_active_context
            # Turns of a conversation start only here, one at a time, so a run
            # record that still says "running" was left by a process that ended.
            self.context_manager.recover_abandoned_turn(active_context_id)
            interrupted = self.context_manager.pending_interruption(active_context_id)
            if interrupted is not None and interrupted.agent and interrupted.agent != agent_key:
                # Only its own agent can resume it: the finished steps are in
                # that agent's session. Another agent's turn leaves it behind.
                interrupted = None
            if message is None and interrupted is None:
                raise AgentError("This conversation has no interrupted turn of this agent to continue.")
            task = self._turn_task(message, interrupted)
            # The trusted task includes the bounded user-authored conversation,
            # not only a context-free follow-up such as "commit" or "continue".
            # Continue is judged against the request it continues.
            action_state = self._action_state(
                self._policy_task(message if message is not None else interrupted.task, active_context_id)
            )
            if action_state is not None and hasattr(observer, "handle_policy_event"):
                action_state.policy_event = observer.handle_policy_event
            session_log = self._record_invocation(agent_key, active_context_id, user_id, shown_message)
            user_id = user_id or self.context_manager.get_metadata("user_id")

            agent = await self.create_agent(agent_key, context_path)
            agent_config = self.config.get_agent(agent_key)
            agent_input = (
                parse_agent_input(message) if message is not None
                else AgentInput(CONTINUE_TEXT, multimodal=False)
            )

            run_ctx = GridRunContext(
                factory=self,
                context_id=active_context_id,
                user_id=user_id,
                agent_id=agent_key,
                container_id=self.container_id,
                action_state=action_state,
                stream_observer=observer,
                run_control=control,
            )
            control.action_state = action_state
            preamble = await self._auto_run_preamble(
                agent_key,
                agent_config,
                agent,
                run_ctx,
                user_message=message if message is not None and agent_input.is_text else "",
            )

            # History reaches the model through the agent's SDK session: every
            # message, tool call and tool result, images included.
            session = self._get_agent_session(agent_key, active_context_id)
            # Past the threshold, the session is summarized before the turn adds to it.
            await self.compact_session(agent_key, active_context_id)
            # Where this turn starts in the agent's session: a branch forked at
            # this turn's message copies the session up to here (fork_conversation).
            session_mark = {
                "agent": agent_key,
                "items": len(await session.get_items()),
                "epoch": self._session_epoch(active_context_id, agent_key),
            }
            # A transcript in the prompt only for an agent that joins a
            # conversation its session has not seen (e.g. after routing).
            include_transcript = continuing and not await session.get_items(limit=1)
            agent.instructions = self._prepare_instructions(
                agent_key, context_path, include_transcript
            )

            # Claimed right before the request is stored, after the last await,
            # so an interruption is continued by exactly one turn.
            resumed = (
                self.context_manager.take_interruption(active_context_id)
                if interrupted is not None
                else None
            )
            if message is None and resumed is None:
                raise AgentError("The interrupted turn was already continued.")
            run_input: Union[str, List[Any]] = (
                self._resumed_input(agent_input, resumed, message)
                if resumed is not None
                else agent_input.items
            )
            if preamble and isinstance(run_input, str):
                run_input = f"{preamble}\n\n[Current user request]\n\n{run_input}"
            if message is None:
                self.context_manager.append_message_to(
                    active_context_id,
                    "user",
                    CONTINUE_TEXT,
                    metadata={
                        "context_id": active_context_id,
                        "agent": agent_key,
                        "type": CONTINUATION_TYPE,
                        "turn_id": turn_id,
                        "session_mark": session_mark,
                    },
                )
            else:
                extra = {"session_mark": session_mark}
                if edit_of:
                    extra["edit_of"] = edit_of
                self._add_user_message(message, agent_input, agent_key, active_context_id, turn_id, extra)
            stored = True

            run_ctx.session = session
            run_ctx.metadata = self.context_manager.get_all_metadata()
            progress.input_preview = safe_preview(run_input, max_length=700)
            self.journal.update_pending_run(
                agent_key=agent_key,
                active_context_id=active_context_id,
                input_preview=progress.input_preview,
                status="running",
                clear_tool_events=True,
                task=task,
                turn_id=turn_id,
            )
            # Graceful Stop needs a streamed run: it ends the stream after a step.
            if stream:
                self._run_controls[active_context_id] = control
            answered = True
            try:
                with collecting(generated):
                    output, result = await self._run_with_retries(
                        agent,
                        agent_key,
                        run_input,
                        run_ctx,
                        session,
                        stream=stream,
                        observer=observer,
                        progress=progress,
                    )
            except _TurnStopped as stop:
                interruption = self._record_interruption(
                    context_id=active_context_id,
                    agent_key=agent_key,
                    task=task,
                    reason=stop.reason,
                    detail=stop.detail,
                    progress=progress,
                    turn_id=turn_id,
                    images=generated.images,
                )
                output, result, answered = interruption.summary(), progress.result, False

            marker = f"Context ID: {active_context_id}"
            if marker not in output:
                output = f"{output.rstrip()}\n\n{marker}"
            if answered:
                self.context_manager.append_message_to(
                    active_context_id,
                    "assistant",
                    with_images(output, generated.images),
                    metadata={
                        "context_id": active_context_id,
                        "agent": agent_key,
                        "type": "agent_response",
                        "turn_id": turn_id,
                    },
                )
            execution.end_time = time.time()
            execution.output = output
            execution.tools_used = [
                name
                for item in getattr(result, "new_items", None) or []
                if getattr(item, "type", "") == "tool_call_item"
                and (name := tool_event_info(item).get("tool_name"))
            ]
            self.context_manager.add_execution(execution)
            self.journal.record_event(
                event_type="attempt_completed",
                output=output,
                extra={"retry_count": progress.attempt},
            )
            if answered:
                self.journal.update_pending_run(
                    agent_key=agent_key,
                    active_context_id=active_context_id,
                    input_preview=progress.input_preview,
                    status="completed",
                    retry_count=progress.attempt,
                )
            Logger("agent_factory").log_verbose(f"FULL RESPONSE: {agent_key}", output)
            return output, active_context_id, answered
        except asyncio.CancelledError:
            # The hard Stop, or the process shutting down: the task is cancelled
            # wherever it was. Record it and let the cancellation go on.
            if stored:
                self._record_interruption(
                    context_id=active_context_id,
                    agent_key=agent_key,
                    task=task,
                    reason=StopReason.USER_STOP,
                    detail="",
                    progress=progress,
                    turn_id=turn_id,
                    images=generated.images,
                )
            raise
        except Exception as exc:
            execution.end_time = time.time()
            execution.error = str(exc)
            if stored:
                self._record_interruption(
                    context_id=active_context_id,
                    agent_key=agent_key,
                    task=task,
                    reason=StopReason.ERROR,
                    detail=str(exc),
                    progress=progress,
                    turn_id=turn_id,
                    images=generated.images,
                )
            if not progress.recorded_failure:
                try:
                    self.journal.update_pending_run(
                        agent_key=agent_key,
                        active_context_id=active_context_id,
                        input_preview=progress.input_preview,
                        status="failed",
                        retry_count=progress.attempt,
                        last_error=safe_preview(str(exc), max_length=700),
                    )
                    self.journal.record_event(
                        event_type="execution_failed",
                        output=str(exc),
                        extra={"exception_type": type(exc).__name__},
                    )
                except Exception:
                    logger.debug("Failed to persist pending run failure state", exc_info=True)
            self.context_manager.add_execution(execution)
            raise
        finally:
            if active_context_id is not None and self._run_controls.get(active_context_id) is control:
                del self._run_controls[active_context_id]
            # Messages sent during the turn that no model call read go back.
            control.steering.hand_back()
            Logger.deactivate_session_log(session_log)
