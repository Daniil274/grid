"""The places where Grid changes how the OpenAI Agents SDK runs tools.

The SDK ends a whole run when a model calls a tool that does not exist, and
when an MCP call fails. Grid reports both to the model instead, so the agent
can correct itself. Both changes replace SDK internals, so ``install`` checks
that those internals are still there and fails loudly if an SDK upgrade moved
them: a silently missing patch would bring back runs that die on one bad call.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any

from agents import _run_impl
from agents.exceptions import ModelBehaviorError
from agents.mcp.util import MCPUtil
from agents.tool import FunctionTool
from openai.types.responses import ResponseFunctionToolCall

from core.action_policy import ActionGate
from core.parallel_lanes import guard as lane_guard
from utils.logger import Logger

logger = logging.getLogger("grid.sdk_patches")

# Short names models reach for, mapped to the tools Grid registers.
_MODEL_TOOL_SHORTCUTS = {
    "read": "file_read",
    "write": "file_write",
    "edit": "file_edit",
    "grep": "grep_tool",
    "glob": "glob_tool",
    "bash": "bash_tool",
}

# Harmony-format models sometimes glue the channel onto the tool name:
# "call_worker<|channel|>commentary", "call_worker_commentary".
_CHANNEL_SUFFIX = re.compile(
    r"(?:<\|channel\|>|[._])(?:commentary|analysis|final|tool)$", re.IGNORECASE
)

_STUB_LISTED_TOOLS = 40


def tool_error_output(tool_name: str, exc: BaseException) -> str:
    """The text an agent gets when a tool call fails, instead of a dead run."""
    logger.warning(
        "Tool '%s' failed, returning the error to the agent: %s: %s",
        tool_name,
        type(exc).__name__,
        exc,
    )
    detail = str(exc) or type(exc).__name__
    return (
        f"Error: tool '{tool_name}' failed: {detail}. "
        "Check the tool name and arguments, then retry or continue without it."
    )


def resolve_model_tool_name(name: str, function_map: dict) -> str:
    """Map a model-generated tool name to a registered tool, if one matches."""
    from tools.function_tools import TOOL_ALIASES

    name = name.strip()
    if name in function_map:
        return name

    shortcut = _MODEL_TOOL_SHORTCUTS.get(name)
    for candidate in (TOOL_ALIASES.get(name), shortcut, TOOL_ALIASES.get(shortcut or "")):
        if candidate and candidate in function_map:
            return candidate

    # After the aliases: some of them legitimately end in a channel word.
    bare = _CHANNEL_SUFFIX.sub("", name).strip()
    if bare and bare != name:
        resolved = resolve_model_tool_name(bare, function_map)
        if resolved in function_map:
            return resolved
    return name


def missing_tool_stub(tool_name: str, available: list[str]) -> FunctionTool:
    """A tool that tells the agent the one it called does not exist."""
    close = difflib.get_close_matches(tool_name, available, n=3, cutoff=0.5)
    listed = sorted(available)[:_STUB_LISTED_TOOLS]
    more = "..." if len(available) > _STUB_LISTED_TOOLS else ""
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    message = (
        f"Error: Tool '{tool_name}' is not available.{hint} "
        f"Available tools include: {', '.join(listed)}{more}"
    )

    async def on_invoke_tool(_ctx: Any, _input: str) -> str:
        return message

    return FunctionTool(
        name=tool_name,
        description=f"Missing tool stub for {tool_name}",
        params_json_schema={},
        on_invoke_tool=on_invoke_tool,
        strict_json_schema=False,
        is_enabled=True,
    )


def _patch_unknown_tool_calls() -> None:
    """Resolve model tool names; answer calls to unknown tools with a stub."""
    original = _run_impl.RunImpl.__dict__["process_model_response"].__func__

    def process_model_response(cls, *, agent, all_tools, response, output_schema, handoffs):
        function_map = {t.name: t for t in all_tools if isinstance(t, FunctionTool)}
        handoff_names = {handoff.tool_name for handoff in handoffs}
        stubs = []
        for item in response.output:
            if not isinstance(item, ResponseFunctionToolCall):
                continue
            name = resolve_model_tool_name(item.name, function_map)
            if name != item.name:
                item.name = name
            if name in function_map or name in handoff_names:
                continue
            stub = missing_tool_stub(name, list(function_map))
            stubs.append(stub)
            function_map[name] = stub
        return original(
            cls,
            agent=agent,
            all_tools=[*all_tools, *stubs],
            response=response,
            output_schema=output_schema,
            handoffs=handoffs,
        )

    process_model_response.__grid_patch__ = True
    _run_impl.RunImpl.process_model_response = classmethod(process_model_response)


def _patch_mcp_tool_errors() -> None:
    """Route MCP calls through the policy gate; return their errors to the agent."""
    original = MCPUtil.__dict__["invoke_mcp_tool"].__func__

    async def invoke_mcp_tool(cls, server, tool, context, input_json):
        name = tool.name

        async def invoke(ctx, args):
            return await original(cls, server, tool, ctx, args)

        raw_ctx = getattr(context, "context", None)
        gate = getattr(getattr(raw_ctx, "factory", None), "action_gate", None)
        # In a batch, the call the gate lets through runs as its lane allows.
        run = lane_guard(invoke, name, "mcp", None, getattr(gate, "config", None), ActionGate._locator)
        try:
            if gate is None:
                result = await run(context, input_json)
            else:
                result = await gate.invoke(
                    name,
                    "mcp",
                    context,
                    input_json,
                    run,
                    descriptor=ActionGate.describe_tool(tool, name, "mcp"),
                )
        except ModelBehaviorError as exc:
            cause = exc.__cause__
            detail = getattr(cause, "msg", None)
            message = (
                f"Invalid JSON for tool '{name}': {detail} (input was: {input_json!r})"
                if detail
                else f"Invalid JSON for tool '{name}': {exc}"
            )
            logger.warning("MCP tool '%s' got invalid arguments: %s", name, message)
            return message
        except Exception as exc:
            # Server failures (unknown tool, timeout, lost connection) arrive as
            # AgentsException/UserError, which would end the run as well.
            return tool_error_output(name, exc)
        Logger("sdk_patches").log_verbose(f"MCP TOOL RESULT: {name}", result)
        return result

    invoke_mcp_tool.__grid_patch__ = True
    MCPUtil.invoke_mcp_tool = classmethod(invoke_mcp_tool)


_PATCHES = (
    (_run_impl.RunImpl, "process_model_response", _patch_unknown_tool_calls),
    (MCPUtil, "invoke_mcp_tool", _patch_mcp_tool_errors),
)


def install() -> None:
    """Apply the patches once; raise if the SDK no longer has what they replace."""
    try:
        for owner, attribute, patch in _PATCHES:
            if not getattr(owner.__dict__[attribute].__func__, "__grid_patch__", False):
                patch()
    except (AttributeError, KeyError) as exc:
        raise RuntimeError(
            "openai-agents internals changed: Grid can no longer turn failed tool "
            "calls into errors for the agent. Pin the SDK version from "
            f"pyproject.toml or update core/sdk_patches.py. ({exc!r})"
        ) from exc
