"""Tools the operator has an agent run before its model is asked.

``auto_run_tools`` of an agent's config: one-time ones once per user and
agent, per-message ones on every request; their output becomes part of the
agent's input.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

from agents import Agent

from core.action_policy import is_policy_block
from core.factory.run_context import AutoRunToolContext, GridRunContext
from schemas import AgentConfig
from utils.logger import Logger

logger = logging.getLogger("grid.agent_factory")


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
        result_parts: List[str] = []
        complete = True
        for auto_tool in agent_config.auto_run_tools or []:
            tool_every_run = bool(auto_tool.get("every_run", False))
            if tool_every_run != every_run:
                continue
            tool_name = auto_tool.get("name")
            tool_params = self._substitute_tool_params(
                dict(auto_tool.get("parameters", {})), working_dir, user_message
            )
            target_tool = next(
                (t for t in tools if getattr(t, "name", "") == tool_name), None
            )
            if target_tool and hasattr(target_tool, "on_invoke_tool"):
                logger.info(
                    f"Auto-running tool '{tool_name}' for agent '{agent_key}' (cwd={working_dir}, every_run={every_run})"
                )
                try:
                    if hasattr(self._stream_observer, "tool_call_started"):
                        self._stream_observer.tool_call_started(
                            agent_key, tool_name, tool_params
                        )
                    tool_ctx_wrapper = AutoRunToolContext(
                        run_context, tool_name=tool_name, operator_configured=True
                    )
                    tool_result = await target_tool.on_invoke_tool(
                        tool_ctx_wrapper, json.dumps(tool_params)
                    )
                    if hasattr(self._stream_observer, "tool_call_finished"):
                        self._stream_observer.tool_call_finished(
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
                        complete = False
                        logger.warning(
                            f"⚠️ Auto-run tool '{tool_name}' blocked by action policy"
                        )
                        continue
                    result_parts.append(
                        f"\n\n=== AUTO-RUN TOOL RESULT '{tool_name}' ===\n{tool_result}\n"
                    )
                    logger.debug(
                        f"Injected auto-run result of '{tool_name}' into instructions"
                    )
                except Exception as tool_err:
                    logger.warning(
                        f"⚠️ Error in auto-run tool '{tool_name}': {tool_err}"
                    )
        return "".join(result_parts), complete

    async def _execute_init_tools(
        self,
        init_tools: List[Dict[str, Any]],
        tools: list,
        run_context: "GridRunContext",
    ) -> str:
        """
        Run a list of raw tool-spec dicts and return concatenated results.

        Mirrors _execute_auto_run_tools() but accepts plain dicts instead of
        an AgentConfig, making it suitable for dynamic (orchestrated) agents.

        Args:
            init_tools: List of {"name": "tool_name", "parameters": {...}}.
            tools:      Resolved tool objects the agent has access to.
            run_context: GridRunContext used for tool invocation.

        Returns:
            Combined result string ready to be prepended to agent instructions.
        """
        result_parts: List[str] = []
        working_dir = (
            "/"
            if getattr(self, "container_id", None)
            else self.config.get_working_directory()
        )
        for spec in init_tools:
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
                    agent_key = (
                        getattr(run_context, "agent_key", None)
                        or getattr(run_context, "agent_name", None)
                        or getattr(run_context, "agent_id", None)
                    )
                    if not agent_key:
                        agent_key = getattr(
                            getattr(run_context, "context", None), "agent_key", None
                        )
                    if not agent_key:
                        agent_key = "dynamic-agent"
                    if hasattr(self._stream_observer, "tool_call_started"):
                        self._stream_observer.tool_call_started(
                            agent_key, tool_name, tool_params
                        )
                    wrapper = AutoRunToolContext(run_context, tool_name=tool_name)
                    result = await target_tool.on_invoke_tool(
                        wrapper, json.dumps(tool_params)
                    )
                    if hasattr(self._stream_observer, "tool_call_finished"):
                        self._stream_observer.tool_call_finished(
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
                        continue
                    result_parts.append(
                        f"\n\n=== CONTEXT [{tool_name}] ===\n{result}\n"
                    )
                except Exception as e:
                    logger.warning(f"⚠️ init_tool '{tool_name}' error: {e}")
            else:
                logger.warning(
                    f"⚠️ init_tool '{tool_name}' not found in agent's tool list"
                )
        return "".join(result_parts)

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
        working_dir = "/" if self.container_id else self.config.get_working_directory()
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
