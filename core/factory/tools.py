"""The tools an agent gets: function tools, sub-agents and their limits.

Function tools resolve through the config's own project loader (and are
withheld in an isolated space unless confined, utils.tool_isolation); agent
tools run sub-agents that share the turn's context, policy chain and stop;
every output is bounded.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
import uuid
from typing import Any, List, Optional

from agents import Agent, RunContextWrapper, function_tool
from agents.exceptions import (
    MaxTurnsExceeded,
    ModelBehaviorError,
    UserError as AgentsUserError,
)

from core.action_policy import delegated_state
from core.factory.run_context import GridRunContext, get_runner, runner_max_turns
from core.interruption import StopRequested
from core.run_stream import interrupted_run_report, run_output_text
from schemas import AgentConfig, AgentExecution
from tools import resolve_tool, tool_isolation
from utils.exceptions import ConfigError
from utils.logger import Logger
from utils.path_utils import reset_current_factory, set_current_factory
from utils.tool_isolation import is_confined

logger = logging.getLogger("grid.agent_factory")


CONTEXT_ID_REGEX = re.compile(r"ctx-[0-9a-fA-F]{8,}")


class ToolAssembly:
    """Builds the tools of the factory's agents.

    Mixed into AgentFactory (core.agent_factory); relies on self.config,
    self.context_manager, self.container_id, the caches, self.models, PolicyWiring
    and TurnRunner.
    """

    async def _resolve_tools_for_names(
        self, tool_names: List[str]
    ) -> tuple[List[Any], List[str]]:
        """
        Resolve a mixed list of tool keys (function/agent/mcp from config) into:
        - tools: SDK tool instances (function tools + agent tools)
        - mcp_server_names: MCP tool keys (servers) to attach to agent
        """
        function_tools: List[str] = []
        agent_tools: List[str] = []
        mcp_tools: List[str] = []

        for tool_key in tool_names:
            try:
                tool_cfg = self.config.get_tool(tool_key)
                if tool_cfg.type == "function":
                    function_tools.append(tool_key)
                elif tool_cfg.type == "agent":
                    agent_tools.append(tool_key)
                elif tool_cfg.type == "mcp":
                    mcp_tools.append(tool_key)
            except ConfigError:
                # Tool lists of dynamic agents are written by models: an unknown
                # name is dropped, not fatal, but it must be visible.
                logger.warning("Unknown tool '%s' requested for a dynamic agent; skipped", tool_key)
                continue

        resolved: List[Any] = self._resolve_function_tools(function_tools)

        if agent_tools:
            try:
                resolved.extend(
                    self._wrap_tool_with_policy(t, getattr(t, "name", "agent"), "agent")
                    for t in await self._create_agent_tools(agent_tools)
                )
            except Exception as exc:
                logger.debug("Failed to resolve agent tools: %s", exc, exc_info=exc)

        return resolved, mcp_tools

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimates the number of tokens in a string (~4 chars = 1 token)."""
        return max(1, len(text) // 4)

    def _truncate_tool_output(self, output: Any, tool_name: str = "") -> Any:
        """Check tool output for limit violations.

        2. max_tool_output — truncates the string with a warning when exceeded.
        """
        settings = self.config.config.settings
        output_str = str(output) if not isinstance(output, str) else output

        # Token check — return error without truncation
        max_tokens = getattr(settings, "max_tool_output_tokens", None)
        if max_tokens is not None and isinstance(output, str):
            estimated = self._estimate_tokens(output_str)
            if estimated > max_tokens:
                logger.warning(
                    "Tool output rejected (token limit): %s (~%d tokens > %d limit)",
                    tool_name,
                    estimated,
                    max_tokens,
                )
                return (
                    f"ERROR: Tool output is too large (~{estimated} tokens, limit {max_tokens} tokens). "
                    f"The result of '{tool_name}' was not passed to avoid context overflow. "
                    f"Use more specific parameters or split the request into smaller parts."
                )

        # Character check — truncate with warning
        max_chars = getattr(settings, "max_tool_output", None)
        if (
            max_chars is not None
            and isinstance(output, str)
            and len(output) > max_chars
        ):
            original_length = len(output)
            truncated = output[:max_chars]
            truncated += (
                f"\n\n⚠️ Tool output truncated "
                f"(showing {max_chars} of {original_length} characters)"
            )
            logger.info(
                "Tool output truncated: %s (%d -> %d chars)",
                tool_name,
                original_length,
                max_chars,
            )
            return truncated

        return output

    async def _ensure_pipeline_for_run_context(
        self,
        run_context_obj: Any,
        orchestrator_name: str,
    ) -> tuple[Any, str, str]:
        """Attach the current execution tree to a shared serial pipeline."""
        from core.tracing.pipeline_registry import PipelineRegistry

        raw_ctx = getattr(run_context_obj, "context", run_context_obj)
        if raw_ctx is None:
            raise ValueError("Run context is missing")

        active_context_id = (
            getattr(raw_ctx, "context_id", None)
            or self.get_active_context_id()
            or self.context_manager.start_new_context()
        )
        user_id = getattr(raw_ctx, "user_id", None)
        pipeline_id = getattr(raw_ctx, "pipeline_id", None)

        registry = PipelineRegistry()
        if not pipeline_id:
            pipeline_id = await registry.get_or_create_pipeline(
                orchestrator_name=orchestrator_name,
                context_id=active_context_id,
                user_id=user_id,
            )

        if (
            hasattr(raw_ctx, "context_id")
            and getattr(raw_ctx, "context_id", None) is None
        ):
            raw_ctx.context_id = active_context_id
        if hasattr(raw_ctx, "pipeline_id"):
            raw_ctx.pipeline_id = pipeline_id
        if hasattr(raw_ctx, "step_id"):
            raw_ctx.step_id = registry.get_current_step_id(pipeline_id)
        if (
            hasattr(raw_ctx, "execution_mode")
            and getattr(raw_ctx, "execution_mode", None) is None
        ):
            raw_ctx.execution_mode = "serial_subtree"

        return registry, pipeline_id, active_context_id

    def _wrap_tool_with_output_limit(
        self, tool: Any, tool_key: Optional[str] = None
    ) -> Any:
        """Wraps FunctionTool with pipeline serialization and output limits."""
        settings = self.config.config.settings
        max_tokens = getattr(settings, "max_tool_output_tokens", None)
        max_chars = getattr(settings, "max_tool_output", None)
        if not hasattr(tool, "on_invoke_tool"):
            return tool

        original_invoke = tool.on_invoke_tool
        factory_ref = self
        tool_name = (
            tool_key or getattr(tool, "name", "") or getattr(tool, "__name__", "tool")
        )

        async def invoke_original(ctx, args):
            result = await original_invoke(ctx, args)
            if max_tokens is None and max_chars is None:
                return result
            return factory_ref._truncate_tool_output(
                result, getattr(tool, "name", "") or tool_name
            )

        async def limited_invoke(ctx, args):
            registry, pipeline_id, active_context_id = (
                await factory_ref._ensure_pipeline_for_run_context(
                    ctx,
                    f"tool:{tool_name}",
                )
            )
            raw_ctx = getattr(ctx, "context", None)
            if raw_ctx is not None:
                raw_ctx.context_id = active_context_id
                raw_ctx.pipeline_id = pipeline_id
                raw_ctx.execution_mode = "serial_subtree"

            async def execute_step():
                return await invoke_original(ctx, args)

            return await registry.run_serialized_step(
                pipeline_id=pipeline_id,
                agent_name=f"tool:{tool_name}",
                step_coro_factory=execute_step,
                metadata={
                    "kind": "function_tool",
                    "tool_name": tool_name,
                },
            )

        tool.on_invoke_tool = limited_invoke
        return self._wrap_tool_with_policy(tool, tool_name, "function")

    def _resolve_function_tools(self, tool_keys: List[str]) -> List[Any]:
        """Each function tool by key, from this factory's own config: its project
        tools first, then the system's. An unknown key is logged and left out;
        each tool keeps its own key for its output limit.

        The registries hand every factory the same tool objects, and wrapping
        replaces a tool's on_invoke_tool: each factory wraps a copy of its own.
        Wrapping the shared object would stack every factory's wrappers on it,
        and one user's calls would run through another's factory - its
        pipeline, its conversations, its limits."""
        loader = self.config.project_tools_loader
        tools: List[Any] = []
        for tool_key in tool_keys:
            if self.confine_tools and not is_confined(tool_isolation(tool_key, loader)):
                logger.warning(
                    "Tool '%s' acts on the host, not in the user's container or workspace; "
                    "withheld from agents of an isolated space",
                    tool_key,
                )
                continue
            tool = resolve_tool(tool_key, loader)
            if tool is None:
                logger.warning("Tool '%s' not found in project or system tools; skipped", tool_key)
                continue
            tools.append(self._wrap_tool_with_output_limit(copy.copy(tool), tool_key))
        return tools

    async def _get_agent_tools(
        self, agent_config: AgentConfig, agent_key: Optional[str] = None
    ) -> List[Any]:
        """Get all tools for agent with caching."""
        cache_key = f"{agent_config.name}:{hash(tuple(agent_config.tools))}"

        if cache_key in self._tool_cache:
            return self._tool_cache[cache_key]

        tools = []

        # Categorize tools
        function_tools = []
        mcp_tools = []
        agent_tools = []

        for tool_name in agent_config.tools:
            try:
                tool_config = self.config.get_tool(tool_name)

                if tool_config.type == "function":
                    function_tools.append(tool_name)
                elif tool_config.type == "mcp":
                    mcp_tools.append(tool_name)
                elif tool_config.type == "agent":
                    agent_tools.append(tool_name)

            except ConfigError as exc:
                logger.warning(
                    "Tool configuration missing for agent",
                    extra={"agent": agent_config.name, "tool": tool_name},
                    exc_info=exc,
                )

        tools.extend(self._resolve_function_tools(function_tools))

        # Add agent tools
        if agent_tools:
            try:
                agent_tool_instances = await self._create_agent_tools(
                    agent_tools, current_agent_key=agent_key
                )
                tools.extend(
                    self._wrap_tool_with_policy(t, getattr(t, "name", "agent"), "agent")
                    for t in agent_tool_instances
                )

            except Exception as e:
                logger.error(
                    "Failed to create agent tools %s: %s", agent_tools, e, exc_info=e
                )

        # Cache tools
        self._tool_cache[cache_key] = tools

        return tools

    async def _create_agent_tools(
        self, agent_keys: List[str], current_agent_key: Optional[str] = None
    ) -> List[Any]:
        """Create agent tools with proper logging and context sharing."""
        tools = []

        for agent_tool_key in agent_keys:
            try:
                # Get tool configuration
                tool_config = self.config.get_tool(agent_tool_key)
                target_agent_key = (
                    getattr(tool_config, "target_agent", None) or agent_tool_key
                )
                target_agent_config = self.config.get_agent(target_agent_key)
                tool_name = tool_config.name or f"call_{target_agent_key}"

                # Self-referential coordinator tools must be lazy. Creating the
                # target eagerly while the parent agent is still being assembled
                # recurses through _get_agent_tools indefinitely.
                sub_agent = (
                    None
                    if target_agent_key == current_agent_key
                    else await self.create_agent(target_agent_key)
                )
                target_agent_name = (
                    getattr(sub_agent, "name", None) or target_agent_config.name
                )
                tool_description = (
                    tool_config.description or f"Calls {target_agent_name}"
                )

                # Get context sharing parameters from tool config
                context_strategy = getattr(
                    tool_config, "context_strategy", "conversation"
                )
                context_depth = getattr(tool_config, "context_depth", 5)
                include_tool_history = getattr(
                    tool_config, "include_tool_history", True
                )

                # Create context-aware tool (primary name)
                main_tool = self._create_context_aware_agent_tool(
                    agent_key=target_agent_key,
                    sub_agent=sub_agent,
                    tool_name=tool_name,
                    tool_description=tool_description,
                    context_strategy=context_strategy,
                    context_depth=context_depth,
                    include_tool_history=include_tool_history,
                )

                # Wrap for logging
                # Channel suffixes the model glues onto the name
                # ("<name>_commentary") are resolved by sdk_patches.resolve_model_tool_name.
                wrapped_main = self._wrap_agent_tool(main_tool, target_agent_name)
                tools.append(wrapped_main)

            except Exception as e:
                logger.error(
                    "Failed to configure agent tool",
                    extra={"agent_tool": agent_tool_key},
                    exc_info=e,
                )

        return tools

    def _wrap_agent_tool(self, agent_tool: Any, agent_name: str) -> Any:
        """Wrap agent tool for proper logging and execution tracking."""
        if not hasattr(agent_tool, "on_invoke_tool"):
            return agent_tool

        original_invoke = agent_tool.on_invoke_tool

        async def wrapped_invoke_tool(tool_context, tool_call_arguments):
            start_time = time.time()
            # Normalize and log tool arguments
            # The SDK passes the model's arguments as a JSON string; map every
            # allowed alias onto the single required 'input' field.
            arguments = tool_call_arguments
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments) if arguments.strip() else {}
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    arguments = parsed
            preferred_text: Optional[str] = None
            if isinstance(arguments, dict):
                # Prioritize text aliases over input to avoid losing the task
                for alias in ("task", "message", "prompt", "input"):
                    value = arguments.get(alias)
                    if isinstance(value, str) and value.strip():
                        preferred_text = value.strip()
                        break
                # If null/None or empty strings — replace with empty string
                if not isinstance(preferred_text, str):
                    preferred_text = ""
            else:
                # Not a JSON object (plain text or malformed JSON): the text is the task
                preferred_text = str(arguments) if arguments is not None else ""
            normalized_args = {"input": preferred_text}

            # Safely convert arguments to string for logging
            requested_context_id = self._extract_context_id_from_text(preferred_text)
            sub_context_id = requested_context_id or f"ctx-{uuid.uuid4().hex[:8]}"

            # Safely convert arguments to string for logging
            input_data = str(normalized_args)

            execution = AgentExecution(
                agent_name=agent_name, start_time=start_time, input_message=input_data
            )
            execution.context_id = sub_context_id

            try:
                # Log tool call with a nice name
                tool_display_name = getattr(agent_tool, "name", agent_name)
                # Add prefix for agent-tools
                formatted_tool_name = f"Agent-Tool: {tool_display_name}"

                Logger("agent_factory").log_verbose(
                    f"TOOL CALL: {tool_display_name}", normalized_args
                )

                registry, pipeline_id, active_context_id = (
                    await self._ensure_pipeline_for_run_context(
                        tool_context,
                        formatted_tool_name,
                    )
                )
                raw_ctx = getattr(tool_context, "context", None)
                if raw_ctx is not None:
                    raw_ctx.context_id = active_context_id
                    raw_ctx.pipeline_id = pipeline_id
                    raw_ctx.execution_mode = "serial_subtree"

                async def execute_original():
                    result = original_invoke(
                        tool_context, json.dumps(normalized_args, ensure_ascii=False)
                    )
                    if hasattr(result, "__await__"):
                        return await result
                    return result

                result = await registry.run_serialized_step(
                    pipeline_id=pipeline_id,
                    agent_name=formatted_tool_name,
                    step_coro_factory=execute_original,
                    metadata={
                        "kind": "agent_tool",
                        "tool_name": tool_display_name,
                        "target_agent": agent_name,
                    },
                )

                # ✅ CONTEXT ISOLATION: DO NOT inject multimodal output into the global context!
                # Each agent works with its own isolated context.
                # The parent agent should only see the text result (final_output).
                # Multimodal data (images) stays inside the sub-agent and does NOT leak upward.

                execution.end_time = time.time()

                # Prepare text representation for logs/history
                if isinstance(result, str):
                    result_text = result
                elif isinstance(result, list) and result:
                    # For object list (multimodal output), create a summary
                    result_text = f"[Multimodal Tool Output: {len(result)} items]"
                    # If all elements have a text representation, use it
                    if all(isinstance(x, str) for x in result):
                        result_text = str(result)
                else:
                    result_text = str(result)

                # The sub-agent reports the session it actually ran in.
                reported = re.findall(r"Context ID: (ctx-[0-9a-fA-F]{8,})", result_text)
                if reported:
                    execution.context_id = reported[-1]
                    execution.output = result_text
                else:
                    execution.output = (
                        result_text.rstrip() + "\n\nContext ID: " + sub_context_id
                    )

                self.context_manager.add_execution(execution)

                # Log the full tool call result in verbose mode
                Logger("agent_factory").log_verbose(
                    f"TOOL RESULT: {tool_display_name}",
                    result if isinstance(result, str) else str(result),
                )

                result = self._truncate_tool_output(result, tool_display_name)
                return result

            except Exception as e:
                execution.end_time = time.time()
                execution.error = str(e)

                self.context_manager.add_execution(execution)

                raise

        agent_tool.on_invoke_tool = wrapped_invoke_tool
        return agent_tool

    def _create_context_aware_agent_tool(
        self,
        agent_key: str,
        sub_agent: Optional[Agent],
        tool_name: str,
        tool_description: str,
        context_strategy: str = "minimal",
        context_depth: int = 5,
        include_tool_history: bool = False,
    ) -> Any:
        """Create an agent tool that can share context with the sub-agent."""

        # Enhance tool description, but move common rules to the shared prompt (see settings.tools_common_rules)
        effective_description = tool_description or ""
        # Key local rules kept brief (one line), the rest in the shared block
        local_rule = "Call: pass a single field input (string). Allowed aliases: task, message, prompt."
        if effective_description:
            effective_description = effective_description + "\n" + local_rule
        else:
            effective_description = local_rule

        @function_tool(
            name_override=tool_name,
            description_override=effective_description,
        )
        async def run_agent_with_context(
            context: RunContextWrapper,
            input: str,
        ) -> str:
            local_sub_agent = sub_agent
            if local_sub_agent is None:
                local_sub_agent = await self.create_agent(agent_key)

            # Prepare human-readable context for the sub-agent
            # At this level, input must be a string, as normalization happened in `wrapped_invoke_tool`
            if not isinstance(input, str) or not input.strip():
                return f"❌ Empty input for tool '{tool_name}'. Please provide a non-empty 'input' (string)."

            raw_input = input.strip()

            # Context is passed ONLY if a context_id is explicitly specified in
            # the request; it names the sub-agent session to continue.
            requested_context_id = self._extract_context_id_from_text(raw_input)
            should_include_context = requested_context_id is not None

            if should_include_context:
                enhanced_input = self.context_manager.get_context_for_agent_tool(
                    strategy=context_strategy,
                    depth=context_depth,
                    include_tools=include_tool_history,
                    task_input=raw_input,
                )
            else:
                # For new sessions, pass only the original request without context
                enhanced_input = raw_input

            # Inherit user_id from parent context
            parent_user_id = (
                context.context.user_id
                if hasattr(context, "context") and hasattr(context.context, "user_id")
                else None
            )

            # The sub-agent session is the requested context, or a new one. Its id
            # is reported back with the result, so the caller can continue it.
            # The id comes from model-written text, so the session is also keyed
            # by user: a named id never opens another user's session.
            sub_context_id = requested_context_id or f"ctx-{uuid.uuid4().hex[:8]}"
            session = self._get_agent_session(
                agent_key,
                f"{sub_context_id}@{parent_user_id}" if parent_user_id else sub_context_id,
            )

            # Create GridRunContext for sub-agent with LOCAL session access
            parent_pipeline_id = (
                context.context.pipeline_id
                if hasattr(context, "context")
                and hasattr(context.context, "pipeline_id")
                else None
            )
            parent_step_id = (
                context.context.step_id
                if hasattr(context, "context") and hasattr(context.context, "step_id")
                else None
            )
            sub_run_ctx = GridRunContext(
                factory=self,
                context_id=sub_context_id,
                session=session,
                action_state=delegated_state(
                    getattr(getattr(context, "context", None), "action_state", None),
                    tool_name,
                    raw_input,
                ),
                action_depth=(
                    getattr(getattr(context, "context", None), "action_depth", 0) or 0
                )
                + 1,
                user_id=parent_user_id,
                container_id=self.container_id,
                pipeline_id=parent_pipeline_id,
                parent_step_id=parent_step_id,
                execution_mode="serial_subtree" if parent_pipeline_id else None,
                # The user's Stop reaches the sub-agent too: it ends after its step.
                run_control=getattr(getattr(context, "context", None), "run_control", None),
            )
            # A sub-agent reports into its caller's view (the web trace, not the
            # console), through a child that keeps its prose out of the answer
            # and nests its steps under the call that started it.
            sub_observer = (
                getattr(getattr(context, "context", None), "stream_observer", None)
                or self._stream_observer
            )
            nested_observer = hasattr(sub_observer, "nested")
            if nested_observer:
                sub_observer = sub_observer.nested(
                    agent_key, call_id=getattr(context, "tool_call_id", None)
                )
            sub_run_ctx.stream_observer = sub_observer

            preamble = await self._auto_run_preamble(
                agent_key,
                self.config.get_agent(agent_key),
                local_sub_agent,
                sub_run_ctx,
                user_message=enhanced_input,
            )
            if preamble:
                enhanced_input = f"{preamble}\n\n[Current user request]\n\n{enhanced_input}"

            set_current_factory(self)
            run_error: Optional[str] = None
            result = None
            try:
                result = get_runner().run_streamed(
                    starting_agent=local_sub_agent,
                    input=enhanced_input,
                    context=sub_run_ctx,
                    session=session,
                    max_turns=runner_max_turns(self.config.get_max_turns(agent_key)),
                    run_config=self._run_config(agent_key, observer=sub_observer),
                )
                control = sub_run_ctx.run_control
                if control is not None:
                    control.attach(result)
                try:
                    await self._consume_stream(
                        result,
                        observer=sub_observer,
                        agent_key=agent_key,
                        action_state=sub_run_ctx.action_state,
                    )
                finally:
                    if control is not None:
                        control.detach(result)
                if control is not None and control.stop_requested and result.final_output is None:
                    # Its finished steps are in its session; the caller is told
                    # it stopped, then stops after this step itself.
                    output = interrupted_run_report(result, f"Agent {agent_key}", StopRequested())
                else:
                    output = run_output_text(result, f"Agent {agent_key}")
            except (MaxTurnsExceeded, ModelBehaviorError, AgentsUserError) as exc:
                # The sub-agent stopped, but its caller goes on: it gets what the
                # sub-agent did so far instead of a bare error, and can decide
                # whether to retry without repeating work already done.
                run_error = str(exc) or type(exc).__name__
                logger.warning("Sub-agent %s stopped: %s: %s", agent_key, type(exc).__name__, run_error)
                output = interrupted_run_report(result, f"Agent {agent_key}", exc)
            except BaseException as exc:
                run_error = str(exc) or type(exc).__name__
                raise
            finally:
                reset_current_factory()
                if nested_observer and hasattr(sub_observer, "finish"):
                    try:
                        sub_observer.finish(error=run_error)
                    except Exception:
                        logger.debug("Failed to settle sub-agent trace", exc_info=True)

            if isinstance(output, str):
                output = f"{output.rstrip()}\n\nContext ID: {sub_context_id}"

            # Record the result as an assistant message so the main agent can discuss and provide corrections
            try:
                self.context_manager.add_tool_result_as_message(tool_name, output)
            except Exception as exc:
                logger.debug(
                    "Failed to record tool result in context: %s", exc, exc_info=exc
                )

            return output

        return run_agent_with_context

    def _extract_context_id_from_text(self, text: Optional[str]) -> Optional[str]:
        """Extract context identifier (ctx-XXXXXXXX) from arbitrary text."""
        if not text:
            return None
        match = CONTEXT_ID_REGEX.search(text)
        return match.group(0).lower() if match else None
