"""Translate Agents SDK stream events into the web chat's trace protocol.

The one rule that shapes this module: *reasoning is not the answer*. Providers
interleave ``response.reasoning_text.delta`` with ``response.output_text.delta``
on the same stream, so a naive observer splices the model's private thinking
into the visible reply. Here the two are separated at the source - thinking goes
to the trace, output goes to the answer - which is what lets the UI show a
step-by-step reasoning timeline alongside a clean response.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Iterable, Optional

from core.action_policy import is_policy_block
from core.context import is_tool_result
from core.interruption import COMPACTED_TYPE, CONTINUATION_TYPE, INTERRUPTED_TYPE
from core.run_stream import (
    REASONING_DELTA_EVENTS,
    field_of,
    is_output_delta,
    tool_event_info,
)
from web_chat.payload import normalize_payload
from web_chat.plan_board import TRACKED_TOOLS, PlanBoard, claimed_task_ids
from web_chat.tool_summaries import tool_subtitle, tool_title
from web_chat.trace import (
    Step,
    StepKind,
    StepStatus,
    TraceRecorder,
    clip,
    context_refs,
    normalize_steps,
    redundant_with_payload,
    summarize,
)

logger = logging.getLogger("grid.web_chat.observer")


# -- typed communication events --------------------------------------------
#
# The timeline says what an agent *did*; these events say what it *said*, and
# who may read it. Three kinds: a ``message`` is meant for people, a ``status``
# is the short "what is happening" line around it, and a ``report`` is an
# internal work product - thinking, a sub-agent's result, a tool's output.
# Visibility is an explicit field, never inferred at the edge: an internal
# event stays internal even when it is serialized, and ``public_events`` is the
# one gate the wire and the history API pass through. That is what keeps a
# reasoning body or a tool payload from leaking by being labelled otherwise.
#
# The events are durable with the conversation: a turn that stores an explicit
# list keeps it on its assistant message (``metadata["events"]``), and a turn
# from before the contract derives the same events from its stored trace and
# message body. Replay and history therefore stay compatible without a second
# copy having to exist. Ids are derived from the kind, the step (or role) and the
# text, which is what a live frame and a later replay of the same turn share.

EVENTS_KEY = "events"
EVENT_VERSION = 1
MAX_EVENT_CHARS = 20_000
MAX_STATUS_CHARS = 500
USER_AUTHOR = "user"
SYSTEM_AUTHOR = "system"


class EventKind(str, Enum):
    """What a communication is; ``kind`` alone does not decide visibility."""

    MESSAGE = "message"
    STATUS = "status"
    REPORT = "report"


class EventVisibility(str, Enum):
    """Who may read an event: everyone in the chat, or nobody outside the server."""

    PUBLIC = "public"
    INTERNAL = "internal"


def _event_text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def stable_event_id(kind: EventKind | str, origin: Any, content: Any = "") -> str:
    """A short id that is the same for the same event on every replay.

    It is built from what identifies the event at its source - its kind, the
    step or role that produced it, and its text - never from a counter that a
    reload would reset differently.
    """
    kind = kind.value if isinstance(kind, EventKind) else str(kind)
    raw = "|".join((kind, str(origin or "event"), _event_text(content, 256)))
    return "ev-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class CommunicationEvent:
    """One typed communication; immutable so a stored copy cannot be mutated."""

    id: str
    kind: EventKind
    author: str
    content: str
    visibility: EventVisibility = EventVisibility.PUBLIC
    author_key: str = ""
    status: str = "done"
    at: Optional[str] = None
    at_ms: int = 0
    turn_id: Optional[str] = None
    message_id: Optional[str] = None
    step_id: Optional[str] = None
    parent_id: Optional[str] = None
    version: int = EVENT_VERSION

    @property
    def is_public(self) -> bool:
        return self.visibility is EventVisibility.PUBLIC

    def as_dict(self) -> dict[str, Any]:
        """The wire/storage form. Keeps ``visibility``, so a reader cannot guess wrong."""
        return {
            "v": self.version,
            "id": self.id,
            "kind": self.kind.value,
            "visibility": self.visibility.value,
            "author": self.author,
            "author_key": self.author_key,
            "content": self.content,
            "status": self.status,
            "at": self.at,
            "at_ms": self.at_ms,
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "step_id": self.step_id,
            "parent_id": self.parent_id,
        }

    def public_dict(self) -> Optional[dict[str, Any]]:
        """The event as a reader may see it; None for an internal one."""
        return self.as_dict() if self.is_public else None

    @classmethod
    def from_dict(cls, data: Any) -> Optional["CommunicationEvent"]:
        """Read one stored event, tolerating an unknown or damaged field."""
        if not isinstance(data, dict):
            return None
        try:
            version = int(data.get("v") or EVENT_VERSION)
            kind = EventKind(str(data.get("kind") or ""))
        except (TypeError, ValueError):
            return None
        if version != EVENT_VERSION:
            return None
        try:
            visibility = EventVisibility(str(data.get("visibility") or EventVisibility.INTERNAL.value))
        except ValueError:
            visibility = EventVisibility.INTERNAL
        event_id = data.get("id")
        if not isinstance(event_id, str) or not event_id:
            return None
        return cls(
            id=event_id,
            kind=kind,
            author=str(data.get("author") or ""),
            content=_event_text(data.get("content"), MAX_EVENT_CHARS),
            visibility=visibility,
            author_key=str(data.get("author_key") or ""),
            status=str(data.get("status") or "done"),
            at=data.get("at") if isinstance(data.get("at"), str) else None,
            at_ms=data.get("at_ms") if isinstance(data.get("at_ms"), int) else 0,
            turn_id=data.get("turn_id") if isinstance(data.get("turn_id"), str) else None,
            message_id=data.get("message_id") if isinstance(data.get("message_id"), str) else None,
            step_id=data.get("step_id") if isinstance(data.get("step_id"), str) else None,
            parent_id=data.get("parent_id") if isinstance(data.get("parent_id"), str) else None,
            version=version,
        )


def serialize_events(events: Iterable[CommunicationEvent], *, public_only: bool = False) -> list[dict[str, Any]]:
    """The stored/wire form; ``public_only`` applies the visibility gate."""
    return [event.as_dict() for event in events if not public_only or event.is_public]


def deserialize_events(raw: Any) -> list[CommunicationEvent]:
    """Read a stored event list, dropping what cannot be trusted."""
    if not isinstance(raw, list):
        return []
    return [event for item in raw if (event := CommunicationEvent.from_dict(item)) is not None]


def public_events(events: Iterable[CommunicationEvent]) -> list[CommunicationEvent]:
    """The one gate public surfaces use: internal events never pass it."""
    return [event for event in events if event.is_public]


class EventLog:
    """The public events of one turn, in order and safe to store.

    Only public events belong here by default: what the agent said to people
    and the statuses around it. ``author`` and ``author_key`` may be callables
    because a turn learns which agent runs it after the log exists.
    """

    def __init__(
        self,
        *,
        turn_id: Optional[str] = None,
        message_id: Optional[str] = None,
        author: str | Callable[[], str] = "",
        author_key: str | Callable[[], str] = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.turn_id = turn_id
        self.message_id = message_id
        self._author = author
        self._author_key = author_key
        self._clock = clock
        self._events: list[CommunicationEvent] = []
        self._by_id: dict[str, CommunicationEvent] = {}

    def _resolve(self, value: str | Callable[[], str]) -> str:
        try:
            return value() if callable(value) else str(value or "")
        except Exception:  # noqa: BLE001 - a label lookup must never cost the turn
            return ""

    @property
    def events(self) -> list[CommunicationEvent]:
        return list(self._events)

    def add(
        self,
        kind: EventKind | str,
        content: Any,
        *,
        visibility: EventVisibility | str = EventVisibility.PUBLIC,
        status: str = "done",
        author: Optional[str] = None,
        author_key: Optional[str] = None,
        message_id: Optional[str] = None,
        step_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        at: Optional[str] = None,
        at_ms: int = 0,
        event_id: Optional[str] = None,
        limit: int = MAX_EVENT_CHARS,
    ) -> CommunicationEvent:
        """Record one event; the same id twice records it once."""
        kind = kind if isinstance(kind, EventKind) else EventKind(kind)
        visibility = visibility if isinstance(visibility, EventVisibility) else EventVisibility(visibility)
        bounded = _event_text(content, limit)
        origin = step_id or f"{kind.value}:{len(self._events)}"
        event_id = event_id or stable_event_id(kind, origin, bounded)
        existing = self._by_id.get(event_id)
        if existing is not None:
            return existing
        event = CommunicationEvent(
            id=event_id,
            kind=kind,
            author=author if author is not None else self._resolve(self._author) or SYSTEM_AUTHOR,
            author_key=author_key if author_key is not None else self._resolve(self._author_key),
            content=bounded,
            visibility=visibility,
            status=status,
            at=at,
            at_ms=at_ms,
            turn_id=self.turn_id,
            message_id=message_id or self.message_id,
            step_id=step_id,
            parent_id=parent_id,
            version=EVENT_VERSION,
        )
        self._events.append(event)
        self._by_id[event_id] = event
        return event

    def message(self, content: Any, **fields: Any) -> CommunicationEvent:
        """What the agent says to people."""
        return self.add(EventKind.MESSAGE, content, **fields)

    def status(self, content: Any, **fields: Any) -> CommunicationEvent:
        """What happens around the words; kept short."""
        fields.setdefault("limit", MAX_STATUS_CHARS)
        return self.add(EventKind.STATUS, content, **fields)

    def report(self, content: Any, **fields: Any) -> CommunicationEvent:
        """An internal work product; internal unless a caller says otherwise."""
        fields.setdefault("visibility", EventVisibility.INTERNAL)
        return self.add(EventKind.REPORT, content, **fields)

    def snapshot(self, *, public_only: bool = False) -> list[dict[str, Any]]:
        return serialize_events(self._events, public_only=public_only)


#: Steps whose title is a status a person may read. Their body is not.
PUBLIC_STATUS_STEP_KINDS = frozenset(
    {
        StepKind.PREPARE.value,
        StepKind.ROUTING.value,
        StepKind.TOOL.value,
        StepKind.COMPACT.value,
        StepKind.PLAN.value,
        StepKind.HANDOFF.value,
        StepKind.MCP.value,
    }
)


def _step_field(step: Any, name: str) -> Any:
    return step.get(name) if isinstance(step, dict) else getattr(step, name, None)


def _payload_text(step: Any) -> str:
    """A report's text from a step, without ever spreading a whole payload."""
    body = _step_field(step, "body")
    if isinstance(body, str) and body.strip():
        return body
    for name in ("result_payload", "input_payload"):
        payload = _step_field(step, name)
        if isinstance(payload, dict):
            raw = payload.get("raw_text")
            if isinstance(raw, str) and raw:
                return raw
    return ""


def event_from_step(
    step: Any,
    *,
    turn_id: Optional[str] = None,
    message_id: Optional[str] = None,
    author: str = "",
    author_key: str = "",
    at: Optional[str] = None,
) -> Optional[CommunicationEvent]:
    """One typed event for a stored timeline step, or None for a silent step.

    This mapping is the privacy boundary: only top-level narration becomes a
    public message; thinking and everything a sub-agent does is an internal
    report; a tool step becomes a public status that carries its title, never
    its arguments or output.
    """
    kind = str(_step_field(step, "kind") or "")
    step_id = _step_field(step, "id")
    parent_id = _step_field(step, "parent_id")
    at_ms = _step_field(step, "at_ms")
    at_ms = at_ms if isinstance(at_ms, int) else 0
    status = str(_step_field(step, "status") or StepStatus.DONE.value)
    title = str(_step_field(step, "title") or "")
    subtitle = str(_step_field(step, "subtitle") or "")
    nested = bool(parent_id)

    def make(
        event_kind: EventKind,
        content: str,
        *,
        visibility: EventVisibility = EventVisibility.PUBLIC,
        event_status: str = status,
        limit: int = MAX_EVENT_CHARS,
    ) -> CommunicationEvent:
        bounded = _event_text(content, limit)
        origin = step_id or title or "step"
        return CommunicationEvent(
            id=stable_event_id(event_kind, origin, bounded),
            kind=event_kind,
            author=author or SYSTEM_AUTHOR,
            author_key=author_key,
            content=bounded,
            visibility=visibility,
            status=event_status,
            at=at,
            at_ms=at_ms,
            turn_id=turn_id,
            message_id=message_id,
            step_id=step_id if isinstance(step_id, str) else None,
            parent_id=parent_id if isinstance(parent_id, str) else None,
            version=EVENT_VERSION,
        )

    if kind == StepKind.MESSAGE.value:
        content = _step_field(step, "body") or subtitle
        if nested:
            return make(
                EventKind.REPORT,
                content,
                visibility=EventVisibility.INTERNAL,
                limit=MAX_STATUS_CHARS,
            )
        return make(EventKind.MESSAGE, content)

    if kind == StepKind.REASONING.value:
        return make(
            EventKind.REPORT,
            _step_field(step, "body") or subtitle,
            visibility=EventVisibility.INTERNAL,
            event_status=StepStatus.DONE.value,
            limit=MAX_STATUS_CHARS,
        )

    if kind == StepKind.AGENT.value:
        return make(
            EventKind.REPORT,
            _payload_text(step) or subtitle or title,
            visibility=EventVisibility.INTERNAL,
            limit=MAX_STATUS_CHARS,
        )

    if kind == StepKind.ERROR.value:
        return make(
            EventKind.STATUS,
            _payload_text(step) or title,
            visibility=EventVisibility.INTERNAL if nested else EventVisibility.PUBLIC,
            event_status=StepStatus.ERROR.value,
            limit=MAX_STATUS_CHARS,
        )

    if kind in PUBLIC_STATUS_STEP_KINDS:
        if nested:
            return make(
                EventKind.REPORT,
                _payload_text(step) or title,
                visibility=EventVisibility.INTERNAL,
                limit=MAX_STATUS_CHARS,
            )
        content = f"{title} · {subtitle}" if subtitle and subtitle not in title else title
        return make(EventKind.STATUS, content, event_status=status, limit=MAX_STATUS_CHARS)

    return None


def events_from_trace(
    steps: Any,
    *,
    turn_id: Optional[str] = None,
    message_id: Optional[str] = None,
    author: str = "",
    author_key: str = "",
    at: Optional[str] = None,
) -> list[CommunicationEvent]:
    """Typed events for a stored trace (current steps or the legacy flat form)."""
    events: list[CommunicationEvent] = []
    for step in normalize_steps(steps or []) if steps else []:
        event = event_from_step(
            step,
            turn_id=turn_id,
            message_id=message_id,
            author=author,
            author_key=author_key,
            at=at,
        )
        if event is not None:
            events.append(event)
    return events


def _message_text(message: Any) -> str:
    getter = getattr(message, "get_text_content", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:  # noqa: BLE001 - a broken getter must not hide the message
            return ""
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else ""


def _message_meta(message: Any) -> dict[str, Any]:
    metadata = getattr(message, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _message_event(
    kind: EventKind,
    origin: str,
    content: Any,
    *,
    status: str = "done",
    visibility: EventVisibility = EventVisibility.PUBLIC,
    author: str,
    author_key: str,
    at: Optional[str],
    turn_id: Optional[str],
    message_id: Optional[str],
    limit: int = MAX_EVENT_CHARS,
) -> CommunicationEvent:
    text = _event_text(content, limit)
    return CommunicationEvent(
        id=stable_event_id(kind, origin, text),
        kind=kind,
        author=author,
        author_key=author_key,
        content=text,
        visibility=visibility,
        status=status,
        at=at,
        turn_id=turn_id,
        message_id=message_id,
        version=EVENT_VERSION,
    )


def message_events(
    message: Any,
    *,
    author_label: Optional[str] = None,
    public_only: bool = False,
) -> list[CommunicationEvent]:
    """Every typed event a stored message carries, derived when it stored none.

    Stored events are authoritative (an explicitly empty list means none). An
    old message is read through its trace; a sub-agent report message becomes
    one internal report and nothing else. ``public_only`` applies the gate at
    the end, so a caller cannot accidentally surface an internal event.
    """
    metadata = _message_meta(message)
    message_id = metadata.get("message_id")
    message_id = message_id if isinstance(message_id, str) else None
    turn_id = metadata.get("turn_id")
    turn_id = turn_id if isinstance(turn_id, str) else None
    author_key = str(metadata.get("agent") or metadata.get("agent_key") or "")
    author = author_label or str(metadata.get("agent_name") or "") or author_key or "assistant"
    at = getattr(message, "timestamp", None)
    at = at if isinstance(at, str) else None

    if EVENTS_KEY in metadata:
        stored = deserialize_events(metadata.get(EVENTS_KEY))
        if stored or metadata.get(EVENTS_KEY) == []:
            return public_events(stored) if public_only else stored

    role = str(getattr(message, "role", "") or "")
    message_type = str(metadata.get("type") or "")
    text = _message_text(message)

    if is_tool_result(message):
        events = [
            _message_event(
                EventKind.REPORT, "tool-result", text,
                visibility=EventVisibility.INTERNAL,
                author=author, author_key=author_key, at=at,
                turn_id=turn_id, message_id=message_id, limit=MAX_STATUS_CHARS,
            )
        ]
    elif message_type == CONTINUATION_TYPE:
        events = [
            _message_event(
                EventKind.STATUS, "continuation", text or "Continued", status="continued",
                author=USER_AUTHOR, author_key=USER_AUTHOR, at=at,
                turn_id=turn_id, message_id=message_id, limit=MAX_STATUS_CHARS,
            )
        ]
    elif message_type == COMPACTED_TYPE:
        events = [
            _message_event(
                EventKind.STATUS, "compacted", "Context compacted",
                author=SYSTEM_AUTHOR, author_key=SYSTEM_AUTHOR, at=at,
                turn_id=turn_id, message_id=message_id, limit=MAX_STATUS_CHARS,
            )
        ]
    elif role == "user":
        events = [
            _message_event(
                EventKind.MESSAGE, "user", text,
                author=USER_AUTHOR, author_key=USER_AUTHOR, at=at,
                turn_id=turn_id, message_id=message_id,
            )
        ]
    elif role != "assistant":
        events = []
    else:
        events = events_from_trace(
            metadata.get("trace") or metadata.get("trace_events"),
            turn_id=turn_id,
            message_id=message_id,
            author=author,
            author_key=author_key,
            at=at,
        )
        if message_type == INTERRUPTED_TYPE:
            if text:
                events.append(
                    _message_event(
                        EventKind.STATUS, "interrupted", text, status="interrupted",
                        author=author, author_key=author_key, at=at,
                        turn_id=turn_id, message_id=message_id, limit=MAX_STATUS_CHARS,
                    )
                )
        elif text:
            # The answer itself is the last public message of the turn.
            events.append(
                _message_event(
                    EventKind.MESSAGE, "final", text,
                    author=author, author_key=author_key, at=at,
                    turn_id=turn_id, message_id=message_id,
                )
            )
    return public_events(events) if public_only else events


def public_message_events(message: Any, *, author_label: Optional[str] = None) -> list[dict[str, Any]]:
    """The public events of one history entry, ready for the wire."""
    return serialize_events(
        message_events(message, author_label=author_label, public_only=True),
        public_only=True,
    )


def with_author(event: CommunicationEvent, author: str, author_key: str = "") -> CommunicationEvent:
    """A copy of *event* under another display author (the registry knows the label)."""
    return replace(event, author=author or event.author, author_key=author_key or event.author_key)


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
        plan: Optional[PlanBoard] = None,
        emit_event: Optional[Callable[[dict[str, Any]], None]] = None,
        events: Optional[EventLog] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        self._recorder = recorder
        self._emit_token = emit_token
        self._reset_answer = reset_answer
        self._emit_image = emit_image
        self._emit_event = emit_event
        # Text streamed since the last action. If another action follows, it was
        # narration ("let me check the diff"), not the answer, and moves to the trace.
        self._narration = ""
        self.agent_label = agent_label
        # Typed public events of this run. A sub-agent's view gets None: what it
        # says is a report for its caller, never a message to the user.
        self.events: Optional[EventLog] = (
            events
            if events is not None
            else EventLog(
                turn_id=turn_id,
                author=lambda: self.agent_label or SYSTEM_AUTHOR,
            )
        )
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
        # The conversation's plan (web_chat.plan_board), fed by the tracker
        # calls of this turn and of its sub-agents; the calls it waits on, by id.
        self._plan = plan if plan is not None else PlanBoard(recorder)
        self._plan_calls: dict[str, tuple[str, Any]] = {}
        # Rows of tool calls the model is still writing, with the characters
        # written so far. A call with long arguments takes minutes to write;
        # without its row the run looks stalled until the call is complete.
        # Keyed by the model response, the output item id and its index: over
        # Chat Completions every call of a response has the same item id
        # (the SDK's fake one) and only the index tells them apart.
        self._writing: dict[tuple, Step] = {}
        self._written_chars: dict[tuple, int] = {}
        self._response_seq = 0
        # Rows of tools the host ran itself (auto_run_tools, init_tools), oldest first per tool.
        self._host_calls: dict[str, deque[Step]] = {}

    @property
    def policy_blocks(self) -> int:
        return self._held[0]

    # -- typed public events ----------------------------------------------
    def _publish_event(self, event: Optional[CommunicationEvent]) -> None:
        """Hand a public event to the transport; an internal one never leaves."""
        if event is None or not event.is_public or self._emit_event is None:
            return
        frame = event.public_dict()
        if frame is not None:
            self._emit_event({"type": "event", "event": frame})

    def record_message(
        self, text: Any, *, step_id: Optional[str] = None, at_ms: int = 0, status: str = "done"
    ) -> Optional[CommunicationEvent]:
        """Say something to people, now and in the stored record."""
        if self.events is None:
            return None
        event = self.events.message(text, step_id=step_id, at_ms=at_ms, status=status)
        self._publish_event(event)
        return event

    def record_status(
        self,
        text: Any,
        *,
        step_id: Optional[str] = None,
        at_ms: int = 0,
        status: str = "done",
        author: Optional[str] = None,
    ) -> Optional[CommunicationEvent]:
        """Record a short public status around the agent's words."""
        if self.events is None:
            return None
        event = self.events.status(text, step_id=step_id, at_ms=at_ms, status=status, author=author)
        self._publish_event(event)
        return event

    def record_report(self, text: Any, **fields: Any) -> Optional[CommunicationEvent]:
        """Keep an internal work product; it is never published."""
        if self.events is None:
            return None
        return self.events.report(text, **fields)

    def turn_events(self, *, public_only: bool = True) -> list[dict[str, Any]]:
        """The events of this run so far, ready to store or replay."""
        if self.events is None:
            return []
        return self.events.snapshot(public_only=public_only)

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
        # A sub-agent narrates for its caller: its words become part of the
        # internal report, never a public message of this turn.
        child.events = None
        child._calls_by_id = self._calls_by_id
        child._calls_in_order = self._calls_in_order
        child._pending_by_call = self._pending_by_call
        child._pending_policies = self._pending_policies
        child._tool_names = self._tool_names
        child._held = self._held
        child._plan = self._plan
        child._plan_calls = self._plan_calls
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

    def handle_models_skipped(self, skipped: list[tuple[str, str]]) -> None:
        """Models of the agent's list this user cannot use now, with the reason each."""
        self._recorder.note(
            StepKind.ERROR,
            f"{len(skipped)} model(s) unavailable",
            parent_id=self._parent_id,
            subtitle=", ".join(key for key, _ in skipped),
            body="\n".join(f"- {key}: {reason}" for key, reason in skipped),
            tone="warn",
        )

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
        self.drop_unwritten_calls()
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

    # -- tools the host runs (auto_run_tools, init_tools) -------------------
    def tool_call_started(
        self, agent_key: Optional[str], tool_name: str, arguments: Any = None,
        *, call_id: Optional[str] = None,
    ) -> None:
        """Show a tool the host runs before the model is asked as a trace row."""
        name = str(tool_name or "tool")
        subtitle = tool_subtitle(name, arguments)
        if subtitle is None:
            subtitle = clip(summarize(arguments, 160).replace("\n", " "), 120)
        step = self._recorder.open(
            StepKind.TOOL, tool_title(name, None) or name, parent_id=self._parent_id,
            tool=name, subtitle=subtitle or "set-up step", detail=summarize(arguments),
            input_payload=normalize_payload(arguments, role="input", tool=name),
        )
        self._host_calls.setdefault(name, deque()).append(step)

    def tool_call_finished(
        self, agent_key: Optional[str], tool_name: Optional[str], output: Any = "",
        *, call_id: Optional[str] = None,
    ) -> None:
        """Close that row; a failure or a policy refusal is shown as an error."""
        name = str(tool_name or "tool")
        waiting = self._host_calls.get(name)
        step = waiting.popleft() if waiting else None
        text = output if isinstance(output, str) else str(output if output is not None else "")
        blocked = is_policy_block(text)
        payload = normalize_payload(output, tool=name)
        # A tool that reports a failure in its payload (MCP `isError`, or a
        # memory tool's soft `{"error": ...}`) is an error even without a prefix.
        failed = blocked or bool(payload.get("is_error")) or text.lstrip().startswith(("❌", "Error", "error:"))
        status = StepStatus.ERROR if failed else StepStatus.DONE
        if blocked:
            self._held[0] += 1
        # The typed payload already carries this text: keep it once.
        raw = output if isinstance(output, str) else ""
        body = "" if redundant_with_payload(raw, payload) else summarize(output)
        if step is None:
            self._recorder.note(
                StepKind.TOOL, tool_title(name, None) or name, tool=name, body=body,
                result_payload=payload, status=status, parent_id=self._parent_id,
            )
            return
        self._recorder.close(step, body=body, result_payload=payload, status=status)

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

    # -- tool calls being written -----------------------------------------
    def _writing_key(self, item_id: Any, data: Any) -> tuple:
        return (self._response_seq, item_id, field_of(data, "output_index"))

    def _on_call_started(self, data: Any) -> None:
        item = field_of(data, "item")
        if field_of(item, "type") != "function_call":
            return
        item_id, call_id = field_of(item, "id"), field_of(item, "call_id")
        if not item_id:
            return
        name = str(field_of(item, "name") or "")
        step = self._recorder.open(
            StepKind.TOOL, tool_title(name, None) or name or "Tool call",
            parent_id=self._parent_id, tool=name, subtitle="writing the call…",
        )
        key = self._writing_key(item_id, data)
        self._writing[key] = step
        self._written_chars[key] = 0
        self._tool_names[step.id] = name
        if call_id:
            # Its tool_called item, or a sub-agent's block, takes this row over.
            self._calls_by_id[call_id] = step
            self._calls_in_order.append(step)

    def _on_call_written(self, data: Any, text: Optional[str]) -> None:
        item_id = field_of(data, "item_id")
        key = self._writing_key(item_id, data)
        step = self._writing.get(key) if item_id else None
        if step is None or not isinstance(text, str):
            return
        before = self._written_chars[key]
        after = self._written_chars[key] = before + len(text)
        # Every 2000 characters: often enough to show progress, rarely enough
        # not to flood the trace with an update per token.
        if before == 0 or after // 2000 != before // 2000:
            self._recorder.update(step, subtitle=f"writing the call: {after:,} characters so far")

    def drop_unwritten_calls(self, error: str = "The model stopped before it finished this call.") -> None:
        """Settle the rows of calls whose writing never finished (the run ended)."""
        for item_id, step in list(self._writing.items()):
            del self._writing[item_id]
            self._written_chars.pop(item_id, None)
            if step.status is StepStatus.RUNNING and step.kind is StepKind.TOOL:
                self._recorder.close(step, status=StepStatus.ERROR, body=error)

    # -- raw token stream --------------------------------------------------
    def _on_raw_event(self, event: Any, agent_key: Optional[str] = None) -> Optional[str]:
        text, data_type = _delta_text(event)
        if data_type == "response.created":
            # A new model response: its output indexes start again from 0.
            self._response_seq += 1
            return None
        if data_type == "response.output_item.added":
            self._on_call_started(getattr(event, "data", None))
            return None
        if data_type == "response.function_call_arguments.delta":
            self._on_call_written(getattr(event, "data", None), text)
            return None
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
        step = self._recorder.note(
            StepKind.MESSAGE,
            "Message",
            subtitle=clip(text, 120),
            body=text,
            parent_id=self._parent_id,
        )
        # The same words are a typed public message, with an id a replay keeps.
        self.record_message(text, step_id=step.id, at_ms=step.at_ms)
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
        if block is None:
            # Some providers change a call's id while it is written: the row
            # that showed it being written is the earliest one of this tool
            # still being written - the call takes it, keeping its place.
            block = self._oldest_writing(tool_name)
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
            self._forget_writing(block)
            self._recorder.update(block, **fields)
            self._tool_names[block.id] = str(info.get("tool_name") or "")
            self._follow_plan(tool_name, arguments, call_id)
            return
        if block is not None and self._forget_writing(block):
            # The row that showed the call while it was written becomes the call's,
            # with the tokens of the response that wrote it.
            self._recorder.claim_usage(block)
            step = self._recorder.update(block, title=title, **fields)
        else:
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
        self._follow_plan(tool_name, arguments, call_id)

    def _follow_plan(self, tool_name: str, arguments: Any, call_id: Optional[str]) -> None:
        """Hand a tracker call to the plan board; its result follows by call id."""
        if tool_name not in TRACKED_TOOLS and not claimed_task_ids(arguments):
            return
        self._plan.called(tool_name, arguments)
        if call_id:
            self._plan_calls[call_id] = (tool_name, arguments)

    def _on_tool_output(self, item: Any, agent_key: Optional[str]) -> None:
        info = tool_event_info(item)
        step = self._take_pending_call(info.get("call_id"))
        value = info.get("output")
        planned = self._plan_calls.pop(info.get("call_id"), None)
        if planned is not None:
            self._plan.finished(*planned, value)
        output = summarize(value)
        tool_name = self._tool_names.get(step.id, "") if step else str(info.get("tool_name") or "")
        result_payload = normalize_payload(
            value, tool=tool_name,
            markdown=bool(step and step.kind is StepKind.AGENT) or tool_name in {"Agent", "WebSpider"}
        )
        # The typed payload already carries this text: keep it once.
        raw = value if isinstance(value, str) else ""
        body = "" if redundant_with_payload(raw, result_payload) else output
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
                StepKind.TOOL, title, tool=name, body=body,
                result_payload=result_payload, status=status, parent_id=self._parent_id
            )
            return
        self._recorder.close(step, body=body, result_payload=result_payload, status=status)

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

    def _oldest_writing(self, tool_name: str) -> Optional[Step]:
        """The earliest row of a *tool_name* call still being written."""
        for step in self._writing.values():
            if self._tool_names.get(step.id) == tool_name and step.status is StepStatus.RUNNING:
                return step
        return None

    def _forget_writing(self, step: Step) -> bool:
        """Whether *step* was a row of a call being written; it no longer is."""
        for item_id, writing in list(self._writing.items()):
            if writing is step:
                del self._writing[item_id]
                self._written_chars.pop(item_id, None)
                return True
        return False

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
