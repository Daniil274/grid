"""parallel: several calls of an agent's own tools at the same time.

A model's response is one pass over the whole context, and the calls in one
response run one after another (the conversation's serial pipeline,
core.tracing.pipeline_registry). ``parallel`` is the one place they run side
by side: three researchers, a search and a file read at once, so the time of
a wave is its slowest call rather than their sum.

The calls are the agent's own tools, each exactly as if the agent had called
it: the action policy judges every one, its output is limited as usual, and
it shows up in the trace under the parallel call. What they change is kept
apart by core.parallel_lanes: each call runs in a lane of its own, file writes
only inside the ``write_paths`` it declared, changing calls one at a time.

The tool is built for each agent from that agent's tools (AgentFactory binds
it), so it can call nothing the agent could not call itself. MCP tools are
reached through their servers, not as function tools, and are not offered.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional

from agents import FunctionTool, RunContextWrapper, RunItemStreamEvent, function_tool
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall
from pydantic import BaseModel, ConfigDict, Field

from core import parallel_lanes
from core.action_policy import ActionGate

PARALLEL = "parallel"
#: Calls one parallel call may hold; more is a plan to split into waves.
MAX_CALLS = 16
DEFAULT_MAX_PARALLEL = 4


class ParallelCall(BaseModel):
    """One call of the batch."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="Name of one of your own tools, exactly as you would call it.")
    arguments: str = Field(description="The call's arguments: a JSON object, as for a direct call.")
    write_paths: List[str] = Field(
        description=(
            "Workspace files or directories this call may change through file tools; "
            "[] when it changes no files. Calls of one batch must not share paths."
        ),
    )


def parallel_tool(siblings: Iterable[Any], *, max_parallel: int = DEFAULT_MAX_PARALLEL) -> FunctionTool:
    """The ``parallel`` tool over *siblings*: the calling agent's own tools.

    The registry holds it with no siblings; AgentFactory binds a copy to each
    agent's tools.
    """
    tools: Dict[str, Any] = {
        tool.name: tool
        for tool in siblings
        if isinstance(tool, FunctionTool) and tool.name != PARALLEL
    }

    @function_tool(name_override=PARALLEL)
    async def parallel(context: RunContextWrapper, calls: List[ParallelCall]) -> str:
        """Run several of your own tool calls at the same time; their results come back together, in call order.

        Use it for two or more calls that do not depend on each other's
        results: launching independent agents, searches, reads. A single call
        is made directly, not through parallel. A call that needs another's
        result goes into the next parallel call or a plain call after it.

        Every call is judged by the policy as if you made it directly. Calls
        that read run freely; calls that change something (files, the tracker,
        shell commands) take turns. A call may change files only inside its
        write_paths, and no two calls of one batch may share a path.
        """
        return await run_parallel(context, calls, tools, max_parallel=max_parallel)

    return parallel


async def run_parallel(
    context: Any, calls: List[ParallelCall], tools: Dict[str, Any], *, max_parallel: int
) -> str:
    started = time.monotonic()
    raw_ctx = getattr(context, "context", None)
    try:
        lanes = _plan(raw_ctx, calls, tools)
    except ValueError as exc:
        return _error(f"{exc} Nothing was run.", available=sorted(tools))

    parent_id = getattr(context, "tool_call_id", None) or "parallel"
    observer = getattr(raw_ctx, "stream_observer", None)
    view = observer.nested("Parallel calls", call_id=parent_id) if hasattr(observer, "nested") else observer
    gate = asyncio.Semaphore(max(1, max_parallel))

    async def one(index: int, call: ParallelCall, lane: parallel_lanes.Lane) -> Dict[str, Any]:
        call_id = f"{parent_id}:{index}"
        async with gate:
            _show(view, "tool_called", call_id, call.tool, call.arguments)
            began = time.monotonic()
            with parallel_lanes.entering(lane):
                output = await tools[call.tool].on_invoke_tool(
                    _call_context(context, call_id, call.tool, call.arguments), call.arguments
                )
            seconds = round(time.monotonic() - began, 1)
            _show(view, "tool_output", call_id, call.tool, output)
        return {"tool": call.tool, "seconds": seconds, "output": _as_data(output)}

    error: Optional[str] = None
    try:
        results = await asyncio.gather(
            *(one(index, call, lane) for index, (call, lane) in enumerate(zip(calls, lanes)))
        )
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if view is not observer and hasattr(view, "finish"):
            view.finish(error=error)
    return json.dumps(
        {
            "results": results,
            "wall_seconds": round(time.monotonic() - started, 1),
            # Above the wall time is what running side by side saved.
            "sum_seconds": round(sum(result["seconds"] for result in results), 1),
        },
        ensure_ascii=False,
    )


def _plan(raw_ctx: Any, calls: List[ParallelCall], tools: Dict[str, Any]) -> List[parallel_lanes.Lane]:
    """A lane for each call, or ValueError naming what is wrong with the batch."""
    if not calls:
        raise ValueError("Give at least one call.")
    if len(calls) > MAX_CALLS:
        raise ValueError(f"At most {MAX_CALLS} calls at once; split the rest into the next wave.")
    for index, call in enumerate(calls):
        if call.tool == PARALLEL:
            raise ValueError(f"Call {index}: parallel cannot run inside parallel.")
        if call.tool not in tools:
            raise ValueError(f"Call {index}: '{call.tool}' is not one of your tools.")
        try:
            arguments = json.loads(call.arguments or "{}")
        except ValueError as exc:
            raise ValueError(f"Call {index} ({call.tool}): arguments are not JSON: {exc}.") from None
        if not isinstance(arguments, dict):
            raise ValueError(f"Call {index} ({call.tool}): arguments must be a JSON object.")

    parent = parallel_lanes.current_lane()
    locate = ActionGate._locator(raw_ctx)
    scopes: List[tuple[str, ...]] = []
    for index, call in enumerate(calls):
        located = []
        for raw in call.write_paths:
            path = locate(raw) if raw.strip() else None
            if path is None:
                raise ValueError(f"Call {index} ({call.tool}): write path '{raw}' is not in the workspace.")
            path = parallel_lanes.normalize(path)
            if parent is not None and not parent.allows(path):
                raise ValueError(
                    f"Call {index} ({call.tool}): write path '{raw}' is outside this agent's own "
                    f"write_paths ({', '.join(parent.write_paths) or 'none'})."
                )
            located.append(path)
        for other, earlier in enumerate(scopes):
            clash = parallel_lanes.overlap(located, earlier)
            if clash is not None:
                raise ValueError(
                    f"Calls {other} and {index} both write '{clash[1]}' / '{clash[0]}': run them "
                    "in sequence, or split the paths between them."
                )
        scopes.append(tuple(located))

    # Nested inside another batch, its calls take turns with that batch's too.
    changes = parent.changes if parent is not None else asyncio.Lock()
    return [
        parallel_lanes.Lane(label=f"{index}:{call.tool}", write_paths=scope, changes=changes)
        for index, (call, scope) in enumerate(zip(calls, scopes))
    ]


def _call_context(context: Any, call_id: str, tool: str, arguments: str) -> ToolContext:
    """The context of one inner call: the parallel call's run, the inner call's identity."""
    return ToolContext(
        context=context.context,
        usage=context.usage,
        tool_name=tool,
        tool_call_id=call_id,
        tool_arguments=arguments,
    )


def _show(view: Any, name: str, call_id: str, tool: str, value: Any) -> None:
    """Report an inner call to the trace as the SDK reports a call of the model."""
    if view is None or not hasattr(view, "handle_event"):
        return
    if name == "tool_called":
        raw = ResponseFunctionToolCall(
            type="function_call", call_id=call_id, name=tool, arguments=value
        )
        item = SimpleNamespace(type="tool_call_item", raw_item=raw)
    else:
        raw = {"type": "function_call_output", "call_id": call_id, "output": str(value)}
        item = SimpleNamespace(type="tool_call_output_item", raw_item=raw, output=value)
    view.handle_event(RunItemStreamEvent(name=name, item=item))


def _as_data(output: Any) -> Any:
    """A JSON answer as data, so it is not escaped a second time in the result."""
    if isinstance(output, str) and output[:1] in "{[":
        try:
            return json.loads(output)
        except ValueError:
            return output
    return output if isinstance(output, (str, int, float, bool, list, dict, type(None))) else str(output)


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


PARALLEL_TOOLS = {PARALLEL: parallel_tool(())}


# Where it acts (utils.tool_isolation): it runs the agent's own tools, each confined in turn
from utils.tool_isolation import WORKSPACE as _WORKSPACE  # noqa: E402

TOOL_ISOLATION = {PARALLEL: _WORKSPACE}

# What it does (utils.tool_effects): it starts the agent's own calls, each judged by itself
from utils import tool_effects as _effects  # noqa: E402

TOOL_EFFECTS = {PARALLEL: _effects.DELEGATE}
