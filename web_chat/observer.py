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

from core.run_stream import (
    REASONING_DELTA_EVENTS,
    field_of,
    is_output_delta,
    tool_event_info,
)
from web_chat.payload import normalize_payload
from web_chat.tool_summaries import tool_subtitle, tool_title
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


def _tokens(count: Any) -> str:
    """~9.8k, ~812: an estimate, so rounded."""
    value = int(count or 0)
    return f"~{value / 1000:.1f}k" if value >= 1000 else f"~{value}"


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
    data_type = field_of(data, "type")
    if isinstance(data, dict):
        return data.get("content") or data.get("delta") or data.get("text"), data_type
    for attr in ("delta", "content", "text"):
        value = getattr(data, attr, None)
        if value:
            return value, data_type
    return None, data_type


#: Who decided a call, as the policy badge says it (core.action_policy sources).
_POLICY_SOURCES = {
    "filter": "decided by the filter",
    "validator": "policy model",
    "user": "your answer",
    "user_grant": "allowed for this turn",
    "host_approval": "approved",
    "no_answer": "no answer in time",
}


class WebStreamObserver:
    """Feed a :class:`TraceRecorder` and an answer-token sink from one stream."""

    def __init__(
        self,
        recorder: TraceRecorder,
        *,
        emit_token: Callable[[str], None],
        reset_answer: Optional[Callable[[], None]] = None,
        emit_image: Optional[Callable[[str], None]] = None,
        agent_label: str = "",
    ) -> None:
        self._recorder = recorder
        self._emit_token = emit_token
        self._reset_answer = reset_answer
        self._emit_image = emit_image
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
        # The one step of this run that says how many tool outputs were left out.
        self._cleared: Optional[Step] = None
        self._cleared_count = 0
        # Calls the policy held in this turn, sub-agents included: one counter,
        # shared by the nested views (web_chat.system_activity).
        self._held = [0]

    @property
    def policy_blocks(self) -> int:
        return self._held[0]

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
        child._held = self._held
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

    def handle_compaction(self, tokens_before: int, tokens_after: int) -> None:
        """The agent's context was summarized (AgentFactory.compact_session)."""
        step = self._recorder.open(
            StepKind.COMPACT,
            "Context compacted",
            parent_id=self._parent_id,
            subtitle=f"{_tokens(tokens_before)} → {_tokens(tokens_after)} tokens",
        )
        self._recorder.close(step)

    def handle_outputs_cleared(self, count: int) -> None:
        """Old tool outputs were left out of a request to fit the context
        (core.context_budget): one step per run, with the largest count."""
        if count <= self._cleared_count:
            return
        self._cleared_count = count
        subtitle = f"{count} old tool output{'s' if count != 1 else ''} left out of the request"
        if self._cleared is None:
            self._cleared = self._recorder.open(
                StepKind.COMPACT, "Context trimmed", parent_id=self._parent_id, subtitle=subtitle
            )
            self._recorder.close(self._cleared)
        else:
            self._recorder.update(self._cleared, subtitle=subtitle)

    def handle_generated_image(self, url: str) -> None:
        """An image the model generated (core.generated_images): show it now."""
        if self._emit_image is not None:
            self._emit_image(url)

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
            self._recorder.close(block, status=StepStatus.ERROR, body=error,
                                 result_payload=normalize_payload(error))
        else:
            self._recorder.close(block)

    #: The web chat shows a held call with buttons: the gate may wait for them.
    accepts_approvals = True

    def handle_policy_event(self, event: dict[str, Any]) -> None:
        """Attach an argument-free policy badge to the matching action row.

        A call held for the user carries its approval id, so the row offers
        Allow and Decline; the gate's next event for the call replaces it.
        """
        if event.get("rule") != "policy_check":
            return
        decision = str(event.get("decision") or "unavailable")
        if decision not in {"allow", "review", "deny", "unavailable"}:
            return
        shadow = event.get("mode") == "shadow"
        source = str(event.get("source") or "")
        awaiting = bool(event.get("awaiting")) and isinstance(event.get("approval_id"), str)
        if decision != "allow" and not shadow and not awaiting:
            self._held[0] += 1
        tone = {
            "allow": "positive",
            "review": "warning",
            "deny": "critical",
            "unavailable": "critical",
        }[decision]
        title_parts = [str(event.get("tool") or "tool")]
        if event.get("filter"):
            title_parts.append(f"filter: {event['filter']}")
        reasons = event.get("reasons")
        if isinstance(reasons, list) and reasons:
            title_parts.append(", ".join(str(reason) for reason in reasons))
        title_parts.append(_POLICY_SOURCES.get(source, source or "policy"))
        latency = event.get("latency_ms")
        if source == "validator" and isinstance(latency, (int, float)):
            title_parts.append(f"{latency:g} ms")
        failures = event.get("validator_failures")
        if isinstance(failures, list) and failures:
            label_reason = "; ".join(str(item) for item in failures[-2:])
            title_parts.append(
                f"validator {'retried' if decision != 'unavailable' else 'failed'}: {label_reason}"
            )
        tool = str(event.get("tool") or "")
        if awaiting:
            label = "Waiting for you" if event.get("approvals") != "operator" else "Waiting for the operator"
        elif source == "user":
            label = "Allowed by you" if decision == "allow" else "Declined by you"
        elif source == "no_answer":
            label = "Not answered"
        elif source == "filter" and decision == "deny":
            label = "Blocked by filter"
        else:
            label = {
                "allow": "Policy ✓",
                "review": "Policy: review",
                "deny": "Policy: deny",
                "unavailable": "Policy unavailable",
            }[decision]
        if shadow and decision != "allow":
            label += " · shadow"
        policy: dict[str, Any] = {
            "decision": decision,
            "label": label,
            "title": " · ".join(title_parts),
        }
        if awaiting:
            policy["approval_id"] = event["approval_id"]
            policy["approvals"] = event.get("approvals") or "user"
        badge = {"tone": tone, "policy": policy}
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
                return self._on_raw_event(event, agent_key)
            if isinstance(event, RunItemStreamEvent):
                self._on_item_event(event, agent_key)
        except Exception:  # never let a rendering bug abort a run
            logger.exception("Failed to process web stream event")
        return None

    # -- raw token stream --------------------------------------------------
    def _on_raw_event(self, event: Any, agent_key: Optional[str] = None) -> Optional[str]:
        text, data_type = _delta_text(event)
        if data_type in REASONING_DELTA_EVENTS:
            # Forward whitespace too: paragraph breaks are part of the thinking.
            if isinstance(text, str) and text:
                self._recorder.reasoning_delta(text, parent_id=self._parent_id)
            return None
        if data_type == "response.completed":
            # Count this response's tokens before its thinking step closes:
            # the usage rides the completed response, and the open reasoning
            # step is the one those tokens belong to.
            data = getattr(event, "data", None)
            response = field_of(data, "response")
            usage = field_of(data, "usage") or field_of(response, "usage")
            self._recorder.record_usage(usage, parent_id=self._parent_id)
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
        tool_name = str(info.get("tool_name") or "")
        name = _tool_display_name(info)
        # Human heading/caption; fall back to the raw name and argument blob.
        title = tool_title(tool_name, info.get("server_label")) or name
        block = self._calls_by_id.get(call_id) if call_id else None
        subtitle = tool_subtitle(tool_name, arguments)
        if subtitle is None:
            subtitle = clip(summarize(arguments, 160).replace("\n", " "), 120)
        fields = {
            # A sub-agent's block is titled by the agent; the call keeps its name.
            "tool": name,
            "subtitle": subtitle,
            "detail": summarize(arguments),
            "input_payload": normalize_payload(arguments, role="input", tool=tool_name,
                                               markdown=bool(block and block.kind is StepKind.AGENT)),
            "refs": context_refs(arguments),
        }
        if block is not None and block.kind is StepKind.AGENT:
            # The sub-agent already opened this call's block; add what it was asked.
            self._recorder.update(block, **fields)
            self._tool_names[block.id] = str(info.get("tool_name") or "")
            return
        step = self._recorder.open(
            StepKind.TOOL, title, parent_id=self._parent_id, **fields
        )
        self._calls_in_order.append(step)
        self._tool_names[step.id] = tool_name
        badge = self._pending_by_call.pop(call_id, None) if call_id else None
        if badge is None:
            waiting = self._pending_policies.get(tool_name)
            badge = waiting.popleft() if waiting else None
        if badge is not None:
            self._recorder.update(step, **badge)
        if call_id:
            self._calls_by_id[call_id] = step

    def _on_tool_output(self, item: Any, agent_key: Optional[str]) -> None:
        info = tool_event_info(item)
        step = self._take_pending_call(info.get("call_id"))
        value = info.get("output")
        output = summarize(value)
        tool_name = self._tool_names.get(step.id, "") if step else str(info.get("tool_name") or "")
        result_payload = normalize_payload(
            value, tool=tool_name,
            markdown=bool(step and step.kind is StepKind.AGENT) or tool_name in {"Agent", "WebSpider"}
        )
        status = (
            StepStatus.ERROR
            if result_payload.get("is_error")
            else StepStatus.DONE
        )
        if step is None:
            # Output without a matching call (resumed run, auto-run tool): still
            # worth showing, just without a duration.
            name = _tool_display_name(info)
            title = (
                tool_title(str(info.get("tool_name") or ""), info.get("server_label"))
                or name
            )
            self._recorder.note(
                StepKind.TOOL, title, tool=name, body=output,
                result_payload=result_payload, status=status, parent_id=self._parent_id
            )
            return
        self._recorder.close(step, body=output, result_payload=result_payload, status=status)

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
            result_payload=normalize_payload(names),
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
