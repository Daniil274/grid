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
        reset_answer: Optional[Callable[[], None]] = None,
        agent_label: str = "",
    ) -> None:
        self._recorder = recorder
        self._emit_token = emit_token
        self._reset_answer = reset_answer
        # Text streamed since the last action. If another action follows, it was
        # narration ("let me check the diff"), not the answer, and moves to the trace.
        self._narration = ""
        self.agent_label = agent_label
        self._calls_by_id: dict[str, Step] = {}
        self._calls_in_order: deque[Step] = deque()
        # The validator judges a call while it executes, which can be before the
        # SDK streams its ``tool_called`` item. Such verdicts wait here - by call
        # id when the gate knows it, otherwise per tool name in call order.
        self._pending_by_call: dict[str, dict[str, Any]] = {}
        self._pending_policies: dict[str, deque[dict[str, Any]]] = {}
        # Bare tool name per open call row, for verdicts that carry no call id.
        self._tool_names: dict[str, str] = {}
        # The ``agent`` step this observer's steps nest under; None at the top.
        self._parent_id: Optional[str] = None
        self._block: Optional[Step] = None

    def nested(
        self, agent_label: str, call_id: Optional[str] = None
    ) -> "WebStreamObserver":
        """A view for a sub-agent run inside this turn.

        The run becomes an ``agent`` step - the very row of the call that
        started it, found by ``call_id`` - and everything the sub-agent does
        nests under it. The call registry is shared, so policy verdicts for the
        sub-agent's actions find their rows. Its output text is the tool result
        the caller receives, never the user-facing answer.
        """
        child = WebStreamObserver(
            self._recorder, emit_token=lambda _text: None, agent_label=agent_label
        )
        child._calls_by_id = self._calls_by_id
        child._calls_in_order = self._calls_in_order
        child._pending_by_call = self._pending_by_call
        child._pending_policies = self._pending_policies
        child._tool_names = self._tool_names
        block = self._calls_by_id.get(call_id) if call_id else None
        if block is None and call_id is None:
            # Without an id, adopt the caller's call only when it is unambiguous.
            candidates = [
                step
                for step in self._calls_in_order
                if step.status is StepStatus.RUNNING
                and step.kind is StepKind.TOOL
                and step.parent_id == self._parent_id
            ]
            block = candidates[0] if len(candidates) == 1 else None
        if block is not None:
            self._recorder.update(block, kind=StepKind.AGENT, title=agent_label)
        else:
            # The call can start before the SDK streams its ``tool_called``
            # item; the block opens now and that item fills it in later.
            block = self._recorder.open(
                StepKind.AGENT, agent_label, parent_id=self._parent_id
            )
            if call_id:
                self._calls_by_id[call_id] = block
                self._calls_in_order.append(block)
        child._parent_id = block.id
        child._block = block
        return child

    def finish(self, error: Optional[str] = None) -> None:
        """The sub-agent run ended: settle its thinking and its block.

        The caller's ``tool_output`` normally closes the block with the report;
        this makes sure a failed or cancelled run does not spin forever.
        """
        self._recorder.end_reasoning(parent_id=self._parent_id)
        block = self._block
        if block is None or block.status is not StepStatus.RUNNING:
            return
        if error:
            self._recorder.close(block, status=StepStatus.ERROR, body=error)
        else:
            self._recorder.close(block)

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
        failures = event.get("validator_failures")
        if isinstance(failures, list) and failures:
            label_reason = "; ".join(str(item) for item in failures[-2:])
            subtitle_parts.append(
                f"validator {'retried' if decision != 'unavailable' else 'failed'}: {label_reason}"
            )
        tool = str(event.get("tool") or "")
        label = {
            "allow": "Policy ✓",
            "review": "Policy: review",
            "deny": "Policy: deny",
            "unavailable": "Policy unavailable",
        }[decision]
        if shadow and decision != "allow":
            label += " · shadow"
        badge = {
            "tone": tone,
            "policy": {
                "decision": decision,
                "label": label,
                "title": " · ".join(subtitle_parts),
            },
        }
        call_id = event.get("call_id")
        if isinstance(call_id, str) and call_id:
            # Parallel calls finish validation in any order, so only the id
            # pins a verdict to the right row.
            step = self._calls_by_id.get(call_id)
            if step is None:
                self._pending_by_call[call_id] = badge
            else:
                self._recorder.update(step, **badge)
            return
        step = next(
            (
                candidate
                for candidate in self._calls_in_order
                if candidate.status is StepStatus.RUNNING
                and candidate.policy is None
                and self._tool_names.get(candidate.id) == tool
            ),
            None,
        )
        if step is None:
            self._pending_policies.setdefault(tool, deque()).append(badge)
            return
        self._recorder.update(step, **badge)

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
                self._recorder.reasoning_delta(text, parent_id=self._parent_id)
            return None
        if data_type == "response.completed":
            self._recorder.end_reasoning(parent_id=self._parent_id)
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
            self._recorder.end_reasoning(parent_id=self._parent_id)
        self._narration += text
        self._emit_token(text)
        return text

    # -- run items ---------------------------------------------------------
    def _on_item_event(self, event: Any, agent_key: Optional[str]) -> None:
        name = getattr(event, "name", "")
        item = getattr(event, "item", None)
        if item is None:
            return
        self._recorder.end_reasoning(parent_id=self._parent_id)
        if name in {"tool_called", "handoff_requested"}:
            self._flush_narration()
        handler = {
            "tool_called": self._on_tool_called,
            "tool_output": self._on_tool_output,
            "handoff_requested": self._on_handoff_requested,
            "handoff_occured": self._on_handoff_occured,
            "mcp_list_tools": self._on_mcp_list_tools,
        }.get(name)
        if handler is not None:
            handler(item, agent_key)

    def _flush_narration(self) -> None:
        text, self._narration = self._narration.strip(), ""
        if not text:
            return
        self._recorder.note(
            StepKind.MESSAGE,
            "Message",
            subtitle=clip(text, 120),
            body=text,
            parent_id=self._parent_id,
        )
        if self._reset_answer is not None:
            self._reset_answer()

    def _on_tool_called(self, item: Any, agent_key: Optional[str]) -> None:
        info = tool_event_info(item)
        arguments = info.get("arguments")
        call_id = info.get("call_id")
        fields = {
            # A sub-agent's block is titled by the agent; the call keeps its name.
            "tool": _tool_display_name(info),
            "subtitle": clip(summarize(arguments, 160).replace("\n", " "), 120),
            "detail": summarize(arguments),
            "refs": context_refs(arguments),
        }
        block = self._calls_by_id.get(call_id) if call_id else None
        if block is not None and block.kind is StepKind.AGENT:
            # The sub-agent already opened this call's block; add what it was asked.
            self._recorder.update(block, **fields)
            self._tool_names[block.id] = str(info.get("tool_name") or "")
            return
        step = self._recorder.open(
            StepKind.TOOL, fields["tool"], parent_id=self._parent_id, **fields
        )
        self._calls_in_order.append(step)
        self._tool_names[step.id] = str(info.get("tool_name") or "")
        badge = self._pending_by_call.pop(call_id, None) if call_id else None
        if badge is None:
            waiting = self._pending_policies.get(str(info.get("tool_name") or ""))
            badge = waiting.popleft() if waiting else None
        if badge is not None:
            self._recorder.update(step, **badge)
        if call_id:
            self._calls_by_id[call_id] = step

    def _on_tool_output(self, item: Any, agent_key: Optional[str]) -> None:
        info = tool_event_info(item)
        step = self._take_pending_call(info.get("call_id"))
        output = summarize(info.get("output"))
        if step is None:
            # Output without a matching call (resumed run, auto-run tool): still
            # worth showing, just without a duration.
            name = _tool_display_name(info)
            self._recorder.note(
                StepKind.TOOL, name, tool=name, body=output, parent_id=self._parent_id
            )
            return
        self._recorder.close(step, body=output)

    def _on_handoff_requested(self, item: Any, agent_key: Optional[str]) -> None:
        target = field_of(getattr(item, "raw_item", None), "name") or "agent"
        self._recorder.note(
            StepKind.HANDOFF,
            f"Delegating to {target}",
            subtitle=self.agent_label,
            body="The agent is handing the next part of the task to a sub-agent.",
            parent_id=self._parent_id,
        )

    def _on_handoff_occured(self, item: Any, agent_key: Optional[str]) -> None:
        source = (
            field_of(getattr(item, "source_agent", None), "name")
            or agent_key
            or self.agent_label
        )
        target = field_of(getattr(item, "target_agent", None), "name") or "agent"
        self._recorder.note(
            StepKind.HANDOFF,
            f"{source} → {target}",
            subtitle="Control transferred",
            parent_id=self._parent_id,
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
            parent_id=self._parent_id,
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
