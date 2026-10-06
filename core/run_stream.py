"""Rendering and reading agent runs: stream events, observers, run output.

Kept apart from the factory so views (console, web trace) and callers can use
them without the factory's dependencies.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol

from agents import RawResponsesStreamEvent, RunItemStreamEvent
from agents.exceptions import MaxTurnsExceeded
from agents.items import ItemHelpers, MessageOutputItem

from core.action_policy import ActionRunState
from utils.cli_chat import CliChatRenderer
from utils.logger import Logger


class StreamObserver(Protocol):
    """Protocol for components that render streaming events."""

    def handle_event(
        self, event: Any, *, agent_key: Optional[str] = None
    ) -> Optional[str]:
        """Render a streaming event. Return text fragments to append to buffers if any."""


REASONING_DELTA_EVENTS = (
    "response.reasoning_text.delta",
    "response.reasoning_summary_text.delta",
)

#: Stream events whose ``delta`` is text the user is meant to read. Several
#: other events carry a ``delta`` too - most importantly
#: ``response.function_call_arguments.delta``, which streams a tool call's JSON
#: arguments - so a text sink has to name what it accepts rather than take
#: whatever has a delta on it.
OUTPUT_DELTA_EVENTS = (
    "response.output_text.delta",
    "response.refusal.delta",
)


def is_output_delta(data_type: Optional[str]) -> bool:
    """True when a raw event's delta belongs in the answer.

    An event that names itself must be on the list. An event that does not name
    itself is a provider shape the SDK did not normalize; those are still
    accepted, because dropping them would silence that provider entirely.
    """
    return data_type is None or data_type in OUTPUT_DELTA_EVENTS


def field_of(obj: Any, *names: str) -> Any:
    """First non-empty attribute/key among ``names``, on an object or a dict."""
    for name in names:
        if obj is None:
            continue
        if isinstance(obj, dict) and name in obj:
            return obj.get(name)
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def append_action_reasoning(action_state: Any, event: Any) -> None:
    """Append one reasoning delta to the state of this run only."""
    if not isinstance(action_state, ActionRunState) or not isinstance(
        event, RawResponsesStreamEvent
    ):
        return
    data = getattr(event, "data", None)
    if getattr(data, "type", None) not in REASONING_DELTA_EVENTS:
        return
    delta = getattr(data, "delta", None)
    if isinstance(delta, str) and delta:
        action_state.reasoning_text += delta


def run_output_text(result: Any, agent_label: str = "The agent") -> Any:
    """What a finished run hands back: its final output, never the run object's repr.

    A model may end its last turn with tool calls or reasoning and no text. Then
    the last text message of the run is the answer; failing that, an explicit
    note, so the caller knows there is no report instead of reading a dump.
    """
    for attribute in ("final_output", "output", "content"):
        value = getattr(result, attribute, None)
        if value is not None and value != "":
            return value
    items = list(getattr(result, "new_items", None) or [])
    for item in reversed(items):
        if isinstance(item, MessageOutputItem):
            text = ItemHelpers.text_message_output(item)
            if text.strip():
                return text
    tools = [
        tool_event_info(item).get("tool_name")
        for item in items
        if getattr(item, "type", "") == "tool_call_item"
    ]
    called = f" after {len(tools)} tool call(s): {', '.join(dict.fromkeys(t for t in tools if t))}" if tools else ""
    return (
        f"[{agent_label} finished without a written report{called}. "
        "Check its changes yourself or ask it for the report.]"
    )


def last_message_text(items: Any) -> str:
    """The newest non-empty assistant message among SDK run items, or ""."""
    for item in reversed(list(items or [])):
        if isinstance(item, MessageOutputItem):
            text = ItemHelpers.text_message_output(item)
            if text.strip():
                return text.strip()
    return ""


def interrupted_run_report(result: Any, agent_label: str, exc: BaseException) -> str:
    """What a run that stopped on an SDK error still hands back to its caller.

    The reason, the tools it called and its last text, so the caller can go on
    or retry without repeating work that was already done.
    """
    from core.interruption import StopRequested

    items = list(getattr(result, "new_items", None) or [])
    if not items:
        run_data = getattr(exc, "run_data", None)
        items = list(getattr(run_data, "new_items", None) or [])
    if isinstance(exc, MaxTurnsExceeded):
        reason = "reached its turn limit"
    elif isinstance(exc, StopRequested):
        reason = "was stopped by the user before it finished"
    else:
        reason = f"stopped on an error: {exc}"
    lines = [f"[{agent_label} {reason}]"]
    tools = [
        tool_event_info(item).get("tool_name")
        for item in items
        if getattr(item, "type", "") == "tool_call_item"
    ]
    if tools:
        called = ", ".join(dict.fromkeys(t for t in tools if t))
        lines.append(
            f"It made {len(tools)} tool call(s) before stopping: {called}. "
            "Their effects may already be in place - check before retrying."
        )
    text = last_message_text(items)
    if text:
        lines.append(f"Its last message:\n{text}")
    return "\n".join(lines)


#: Why a run ended before its answer (RunOutcome.stopped).
STOPPED_TIMEOUT = "timeout"
STOPPED_MAX_TURNS = "max_turns"
STOPPED_ERROR = "error"


@dataclass(frozen=True)
class RunOutcome:
    """What a finished run hands back: its answer and what it took.

    ``stopped`` is None for a run that answered, else why it ended early
    (``timeout``, ``max_turns``, ``error``); ``text`` is then the report of
    :func:`interrupted_run_report`. The counts are the Agents SDK's, summed
    over the run's retried attempts.
    """

    text: str
    stopped: Optional[str] = None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def run_usage(result: Any) -> tuple[int, int, int]:
    """Model calls, input and output tokens the SDK counted for a run so far."""
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)

    def count(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return count("requests"), count("input_tokens"), count("output_tokens")


def tool_event_info(item: Any) -> dict[str, Any]:
    """Normalize a ``tool_called``/``tool_output`` stream item.

    The SDK exposes tool identity in different shapes depending on the provider
    (function call, MCP call, hosted tool), so every consumer needs the same
    defensive unwrapping.
    """
    raw_item = getattr(item, "raw_item", None)
    function_data = field_of(raw_item, "function") or field_of(item, "function")
    tool_name = (
        field_of(raw_item, "name", "tool_name")
        or field_of(function_data, "name")
        or field_of(item, "name", "tool_name")
    )
    raw_type = field_of(raw_item, "type") or field_of(item, "type")
    if not tool_name and raw_type not in {"function_call_output", "tool_call_output"}:
        tool_name = raw_type
    call_id = field_of(raw_item, "call_id", "id") or field_of(item, "call_id", "id")
    output = field_of(item, "output")
    if output is None:
        output = field_of(raw_item, "output")
    return {
        "tool_name": tool_name,
        "server_label": field_of(raw_item, "server_label")
        or field_of(item, "server_label"),
        "call_id": str(call_id) if call_id else None,
        "arguments": field_of(raw_item, "arguments") or field_of(item, "arguments"),
        "output": output,
    }


class ConsoleStreamObserver:
    """Default stream observer that mirrors legacy console output."""

    def __init__(
        self,
        output_writer=None,
        *,
        render_text_deltas: bool = True,
        text_callback: Optional[Callable[[str], None]] = None,
        renderer: Optional[CliChatRenderer] = None,
    ) -> None:
        self._write = output_writer or print
        self._logger = logging.getLogger("grid.agent_factory.stream")
        self._render_text_deltas = render_text_deltas
        self._text_callback = text_callback
        self._renderer = renderer
        self._pending_tool_calls: Dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._pending_tool_calls_by_id: Dict[tuple[str, str], dict[str, Any]] = {}
        self._pending_tool_call_order: Dict[str, list[dict[str, Any]]] = {}
        self.reasoning_text: str = ""
        self._reasoning_buf: list[str] = []

    def _flush_reasoning_now(self) -> None:
        if not self._reasoning_buf:
            return
        text = "".join(self._reasoning_buf).strip()
        self._reasoning_buf = []
        if not text:
            return
        if self._renderer:
            self._renderer.print_status(f"reasoning: {text}", style="dim")
        else:
            self._emit(f"\n[reasoning] {text}", end="\n", flush=True)

    def _emit(self, message: str, *, end: str = "\n", flush: bool = False) -> None:
        self._write(message, end=end, flush=flush)

    @staticmethod
    def _format_args(arguments: Any) -> str:
        if isinstance(arguments, str):
            return arguments
        if isinstance(arguments, dict):
            parts = []
            for key, value in arguments.items():
                if isinstance(value, str) and len(value) > 60:
                    parts.append(f"{key}=...({len(value)} chars)")
                elif isinstance(value, (dict, list)):
                    parts.append(f"{key}={type(value).__name__}({len(value)})")
                else:
                    parts.append(f"{key}={value}")
            return " | ".join(parts)
        if arguments is not None:
            return str(arguments)
        return ""

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds < 1:
            return f"{seconds * 1000:.0f}ms"
        return f"{seconds:.2f}s"

    def _remember_tool_call(
        self,
        agent_key: Optional[str],
        tool_display_name: str,
        arguments: Any,
        call_id: Optional[str] = None,
    ) -> None:
        agent = agent_key or "agent"
        call_info = {
            "started_at": time.monotonic(),
            "arguments": arguments,
            "tool_display_name": tool_display_name,
            "call_id": call_id,
        }
        key = (agent, tool_display_name)
        self._pending_tool_calls.setdefault(key, []).append(call_info)
        self._pending_tool_call_order.setdefault(agent, []).append(call_info)
        if call_id:
            self._pending_tool_calls_by_id[(agent, call_id)] = call_info

    def _remove_pending_tool_call(self, agent: str, call_info: dict[str, Any]) -> None:
        display_name = call_info.get("tool_display_name")
        if display_name:
            key = (agent, display_name)
            pending = self._pending_tool_calls.get(key)
            if pending and call_info in pending:
                pending.remove(call_info)
                if not pending:
                    self._pending_tool_calls.pop(key, None)
        ordered = self._pending_tool_call_order.get(agent)
        if ordered and call_info in ordered:
            ordered.remove(call_info)
            if not ordered:
                self._pending_tool_call_order.pop(agent, None)
        call_id = call_info.get("call_id")
        if call_id:
            self._pending_tool_calls_by_id.pop((agent, call_id), None)

    def _finalize_tool_call_info(
        self,
        agent_key: Optional[str],
        tool_display_name: Optional[str],
        call_id: Optional[str] = None,
    ) -> dict[str, Any]:
        agent = agent_key or "agent"
        call_info = None
        if call_id:
            call_info = self._pending_tool_calls_by_id.get((agent, call_id))
        if call_info is None and tool_display_name:
            pending = self._pending_tool_calls.get((agent, tool_display_name))
            if pending:
                call_info = pending[0]
        if call_info is None:
            ordered = self._pending_tool_call_order.get(agent)
            if ordered:
                call_info = ordered[0]
        if call_info is None:
            return {}
        self._remove_pending_tool_call(agent, call_info)
        started_at = call_info.get("started_at")
        info = dict(call_info)
        if isinstance(started_at, (int, float)):
            info["duration"] = self._format_duration(time.monotonic() - started_at)
        return info

    def tool_call_started(
        self,
        agent_key: Optional[str],
        tool_display_name: str,
        arguments: Any = None,
        *,
        call_id: Optional[str] = None,
    ) -> None:
        self._remember_tool_call(
            agent_key, tool_display_name, arguments, call_id=call_id
        )
        args_str = self._format_args(arguments)
        if self._renderer:
            self._renderer.print_tool_call(
                tool_display_name,
                args_str,
                agent_name=agent_key,
            )
        elif args_str:
            self._emit(f"\n[tool] {tool_display_name} | {args_str}")
        else:
            self._emit(f"\n[tool] {tool_display_name}")

    def tool_call_finished(
        self,
        agent_key: Optional[str],
        tool_display_name: Optional[str],
        output: Any = "",
        *,
        call_id: Optional[str] = None,
    ) -> None:
        call_info = self._finalize_tool_call_info(
            agent_key,
            tool_display_name,
            call_id=call_id,
        )
        display_name = call_info.get("tool_display_name") or tool_display_name or "tool"
        duration = call_info.get("duration")
        output_str = str(output if output is not None else "")
        if len(output_str) > 200:
            output_str = output_str[:200] + "..."
        if self._renderer:
            self._renderer.print_tool_output(
                display_name,
                output_str,
                agent_name=agent_key,
                duration=duration,
            )
        else:
            self._emit(f"[tool-result] {display_name} -> {output_str}")

    def handle_event(
        self, event: Any, *, agent_key: Optional[str] = None
    ) -> Optional[str]:
        try:
            if isinstance(event, RunItemStreamEvent):
                self._flush_reasoning_now()
                name = getattr(event, "name", "")
                item = getattr(event, "item", None)
                if name == "tool_called" and item is not None:
                    info = tool_event_info(item)
                    tool_name = info.get("tool_name") or "tool"
                    arguments = info.get("arguments")
                    server_label = info.get("server_label")
                    tool_display_name = (
                        f"{server_label}.{tool_name}" if server_label else tool_name
                    )
                    self.tool_call_started(
                        agent_key,
                        tool_display_name,
                        arguments,
                        call_id=info.get("call_id"),
                    )
                    Logger("stream").log_verbose(
                        f"STREAM TOOL CALL: {tool_display_name}", arguments
                    )

                elif name == "tool_output" and item is not None:
                    info = tool_event_info(item)
                    tool_name = info.get("tool_name")
                    server_label = info.get("server_label")
                    event_tool_display_name = (
                        f"{server_label}.{tool_name}"
                        if server_label and tool_name
                        else tool_name
                    )
                    output_val = info.get("output")
                    if output_val is None:
                        output_val = ""
                    self.tool_call_finished(
                        agent_key,
                        event_tool_display_name,
                        output_val,
                        call_id=info.get("call_id"),
                    )
                    Logger("stream").log_verbose(
                        f"STREAM TOOL OUTPUT: {event_tool_display_name or 'tool'}",
                        output_val,
                    )

                elif name == "handoff_requested" and item is not None:
                    src = getattr(item, "agent", None)
                    src_name = getattr(src, "name", None) or agent_key or "agent"
                    raw_item = getattr(item, "raw_item", None)
                    target = getattr(raw_item, "name", None) or "agent"
                    if self._renderer:
                        self._renderer.print_handoff(src_name, target)
                    else:
                        self._emit(f"\n[handoff] {src_name} -> {target}")
                elif name == "handoff_occured" and item is not None:
                    src_agent = getattr(item, "source_agent", None)
                    dst_agent = getattr(item, "target_agent", None)
                    src_name = getattr(src_agent, "name", None) or "agent"
                    dst_name = getattr(dst_agent, "name", None) or "agent"
                    if self._renderer:
                        self._renderer.print_handoff(src_name, dst_name, completed=True)
                    else:
                        self._emit(f"[handoff-complete] {src_name} => {dst_name}")
                elif name == "mcp_list_tools" and item is not None:
                    raw_item = getattr(item, "raw_item", None)
                    server_label = getattr(raw_item, "server_label", None) or "mcp"
                    tools = getattr(raw_item, "tools", None)
                    count = len(tools) if tools is not None else "?"
                    if self._renderer:
                        self._renderer.print_status(
                            f"MCP {server_label}: {count} tool(s)",
                            style="bright_black",
                        )
                    else:
                        self._emit(f"[mcp] {server_label}: {count} tool(s)")
            elif isinstance(event, RawResponsesStreamEvent):
                content: Optional[str] = None
                data_type: Optional[str] = None
                if hasattr(event, "content") and event.content:
                    content = event.content
                elif hasattr(event, "delta") and event.delta:
                    content = event.delta
                elif hasattr(event, "text") and event.text:
                    content = event.text
                elif hasattr(event, "data") and event.data:
                    data = event.data
                    data_type = getattr(data, "type", None)

                    if data_type in REASONING_DELTA_EVENTS:
                        delta_text = getattr(data, "delta", None)
                        if isinstance(delta_text, str) and delta_text.strip():
                            self.reasoning_text += delta_text
                            self._reasoning_buf.append(delta_text)
                            if self._text_callback:
                                self._text_callback(delta_text)
                            if self._render_text_deltas:
                                self._emit(delta_text, end="", flush=True)
                            return delta_text
                        return None

                    if hasattr(data, "delta") and data.delta:
                        content = data.delta
                    elif hasattr(data, "content") and data.content:
                        content = data.content
                    elif hasattr(data, "text") and data.text:
                        content = data.text
                    elif isinstance(data, dict):
                        content = (
                            data.get("content") or data.get("delta") or data.get("text")
                        )

                if not is_output_delta(data_type):
                    # A delta from a non-text event (tool call arguments, audio,
                    # code interpreter) is not part of the answer.
                    return None

                has_content = bool(
                    content and isinstance(content, str) and content.strip()
                )
                # Flush buffered reasoning only when real (non-reasoning) content starts
                # or the response reaches a terminal boundary. Some providers (e.g.
                # OpenRouter GLM/DeepSeek) emit an interleaved empty
                # `response.output_text.delta` after every reasoning delta; flushing on
                # those printed reasoning one line per token.
                if has_content or data_type == "response.completed":
                    self._flush_reasoning_now()

                if has_content:
                    if self._text_callback:
                        self._text_callback(content)
                    if self._render_text_deltas:
                        self._emit(content, end="", flush=True)
                    return content
        except Exception:
            self._logger.exception("Failed to render streaming event")
        return None
