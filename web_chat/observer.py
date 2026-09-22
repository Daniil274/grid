"""Translate Agents SDK stream events into the web chat's trace protocol.

The one rule that shapes this module: *reasoning is not the answer*. Providers
interleave ``response.reasoning_text.delta`` with ``response.output_text.delta``
on the same stream, so a naive observer splices the model's private thinking
into the visible reply. Here the two are separated at the source - thinking goes
to the trace, output goes to the answer - which is what lets the UI show a
step-by-step reasoning timeline alongside a clean response.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Callable, Optional

from core.agent_factory import (
    REASONING_DELTA_EVENTS,
    field_of,
    is_output_delta,
    tool_event_info,
)
from web_chat.trace import (
    Step,
    StepKind,
    StepStatus,
    TraceRecorder,
    clip,
    context_refs,
    summarize,
)

logger = logging.getLogger("grid.web_chat.observer")


def _tool_display_name(info: dict[str, Any]) -> str:
    name = info.get("tool_name") or "tool"
    server = info.get("server_label")
    return f"{server}.{name}" if server else str(name)


def _delta_text(event: Any) -> tuple[Optional[str], Optional[str]]:
    """Return ``(text, data_type)`` for a raw response event.

    Mirrors ``ConsoleStreamObserver``: the payload hides under a different
    attribute depending on the provider, so every plausible shape is probed.
    """
    for attr in ("content", "delta", "text"):
        value = getattr(event, attr, None)
        if value:
            return value, None
    data = getattr(event, "data", None)
    if not data:
        return None, None
    data_type = getattr(data, "type", None)
    if isinstance(data, dict):
        return data.get("content") or data.get("delta") or data.get("text"), data_type
    for attr in ("delta", "content", "text"):
        value = getattr(data, attr, None)
        if value:
            return value, data_type
    return None, data_type


class WebStreamObserver:
    """Feed a :class:`TraceRecorder` and an answer-token sink from one stream."""

    def __init__(
        self,
        recorder: TraceRecorder,
        *,
        emit_token: Callable[[str], None],
        agent_label: str = "",
    ) -> None:
        self._recorder = recorder
        self._emit_token = emit_token
        self.agent_label = agent_label
        self._calls_by_id: dict[str, Step] = {}
        self._calls_in_order: deque[Step] = deque()

    def handle_policy_event(self, event: dict[str, Any]) -> None:
        """Attach an argument-free policy badge to the matching action row."""
        if event.get("rule") != "policy_check":
            return
        decision = str(event.get("decision") or "unavailable")
        if decision not in {"allow", "review", "deny", "unavailable"}:
            return
        mode = str(event.get("mode") or "")
        shadow = mode == "shadow"
        tone = {
            "allow": "positive",
            "review": "warning",
            "deny": "critical",
            "unavailable": "critical",
        }.get(decision, "neutral")
        source = "Decisions validator"
        latency = event.get("latency_ms")
        subtitle_parts = [str(event.get("tool") or "tool"), source]
        subtitle_parts.extend(
            f"{name}: {event[name]}"
            for name in ("action", "chain")
            if event.get(name) in {"allow", "deny", "review", "unavailable"}
        )
        if isinstance(latency, (int, float)):
            subtitle_parts.append(f"{latency:g} ms")
        step = next(
            (
                candidate
                for candidate in reversed(self._calls_in_order)
                if candidate.status is StepStatus.RUNNING
                and candidate.title.rsplit(".", 1)[-1] == str(event.get("tool") or "")
            ),
            None,
        )
        if step is None:
            return
        label = {
            "allow": "Policy ✓",
            "review": "Policy: review",
            "deny": "Policy: deny",
            "unavailable": "Policy unavailable",
        }[decision]
        if shadow and decision != "allow":
            label += " · shadow"
        self._recorder.update(
            step,
            tone=tone,
            policy={
                "decision": decision,
                "label": label,
                "title": " · ".join(subtitle_parts),
            },
        )

    # -- entry point -------------------------------------------------------
    def handle_event(
        self, event: Any, *, agent_key: Optional[str] = None
    ) -> Optional[str]:
        from agents import RawResponsesStreamEvent, RunItemStreamEvent

        try:
            if isinstance(event, RawResponsesStreamEvent):
                return self._on_raw_event(event)
            if isinstance(event, RunItemStreamEvent):
                self._on_item_event(event, agent_key)
        except Exception:  # never let a rendering bug abort a run
            logger.exception("Failed to process web stream event")
        return None

    # -- raw token stream --------------------------------------------------
    def _on_raw_event(self, event: Any) -> Optional[str]:
        text, data_type = _delta_text(event)
        if data_type in REASONING_DELTA_EVENTS:
            # Forward whitespace too: paragraph breaks are part of the thinking.
            if isinstance(text, str) and text:
                self._recorder.reasoning_delta(text)
            return None
        if data_type == "response.completed":
            self._recorder.end_reasoning()
        if not is_output_delta(data_type):
            # Tool call arguments stream as deltas of their own; they belong to
            # the step that is already showing them, never to the answer.
            return None
        if not isinstance(text, str) or not text:
            return None
        if text.strip():
            # Only *visible* output means the model stopped thinking and started
            # answering. Some providers interleave blank output deltas with
            # reasoning; ending the step on those would shred it into fragments.
            self._recorder.end_reasoning()
        self._emit_token(text)
        return text

    # -- run items ---------------------------------------------------------
    def _on_item_event(self, event: Any, agent_key: Optional[str]) -> None:
        name = getattr(event, "name", "")
        item = getattr(event, "item", None)
        if item is None:
            return
        self._recorder.end_reasoning()
        handler = {
            "tool_called": self._on_tool_called,
            "tool_output": self._on_tool_output,
            "handoff_requested": self._on_handoff_requested,
            "handoff_occured": self._on_handoff_occured,
            "mcp_list_tools": self._on_mcp_list_tools,
        }.get(name)
        if handler is not None:
            handler(item, agent_key)

    def _on_tool_called(self, item: Any, agent_key: Optional[str]) -> None:
        info = tool_event_info(item)
        arguments = info.get("arguments")
        step = self._recorder.open(
            StepKind.TOOL,
            _tool_display_name(info),
            subtitle=clip(summarize(arguments, 160).replace("\n", " "), 120),
            detail=summarize(arguments),
            refs=context_refs(arguments),
        )
        self._calls_in_order.append(step)
        call_id = info.get("call_id")
        if call_id:
            self._calls_by_id[call_id] = step

    def _on_tool_output(self, item: Any, agent_key: Optional[str]) -> None:
        info = tool_event_info(item)
        step = self._take_pending_call(info.get("call_id"))
        output = summarize(info.get("output"))
        if step is None:
            # Output without a matching call (resumed run, auto-run tool): still
            # worth showing, just without a duration.
            self._recorder.note(StepKind.TOOL, _tool_display_name(info), body=output)
            return
        self._recorder.close(step, body=output)

    def _on_handoff_requested(self, item: Any, agent_key: Optional[str]) -> None:
        target = field_of(getattr(item, "raw_item", None), "name") or "agent"
        self._recorder.note(
            StepKind.HANDOFF,
            f"Delegating to {target}",
            subtitle=self.agent_label,
            body="The agent is handing the next part of the task to a sub-agent.",
        )

    def _on_handoff_occured(self, item: Any, agent_key: Optional[str]) -> None:
        source = (
            field_of(getattr(item, "source_agent", None), "name")
            or agent_key
            or self.agent_label
        )
        target = field_of(getattr(item, "target_agent", None), "name") or "agent"
        self._recorder.note(
            StepKind.HANDOFF, f"{source} → {target}", subtitle="Control transferred"
        )

    def _on_mcp_list_tools(self, item: Any, agent_key: Optional[str]) -> None:
        raw_item = getattr(item, "raw_item", None)
        server = field_of(raw_item, "server_label") or "mcp"
        tools = field_of(raw_item, "tools") or []
        names = [str(field_of(tool, "name") or tool) for tool in tools]
        self._recorder.note(
            StepKind.MCP,
            f"Connected to {server}",
            subtitle=f"{len(names)} tools available",
            body="\n".join(names),
        )

    def _take_pending_call(self, call_id: Optional[str]) -> Optional[Step]:
        """Match an output to its call, falling back to the oldest open call."""
        step = self._calls_by_id.pop(call_id, None) if call_id else None
        if step is None:
            while self._calls_in_order:
                candidate = self._calls_in_order.popleft()
                if candidate.status is StepStatus.RUNNING:
                    return candidate
            return None
        try:
            self._calls_in_order.remove(step)
        except ValueError:
            pass
        return step
