"""Structured reasoning trace for the web chat.

A turn is shown in the UI as a timeline of *steps*: the agent thinking, calling
a tool, reading context, handing work to a sub-agent. This module owns that
vocabulary, the wire format, and the recorder that mints and closes steps. It
knows nothing about HTML, websockets or the Agents SDK - the observer feeds it,
the transport ships its events, the client renders them.

Wire protocol (one JSON object per event; the transport adds ``run_id``):

``{"type": "step", "step": {...}}``
    Upsert a step by ``step.id``. The same id is sent again when the step
    completes, carrying its result and duration, so the client only ever needs
    a map from id to step.
``{"type": "reasoning", "id": ..., "delta": "..."}``
    Append a fragment to a reasoning step already announced by a ``step`` event.
    Keeps live thinking cheap: the full step is only re-sent when it closes.
``{"type": "step_removed", "id": ...}``
    Drop a step announced earlier (thinking that turned out to be empty).
``{"type": "token", "content": "..."}``
    A fragment of the user-visible answer.

Steps form a tree through ``parent_id``: a sub-agent's work is a timeline of its
own, nested under the ``agent`` step of the call that started it. The wire and
the stored trace stay flat lists; the client assembles the tree.
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

#: Longest tool payload kept in a step; the UI shows the head and says so.
DETAIL_LIMIT = 4000
#: Longest single context-chip label.
LABEL_LIMIT = 48


class StepKind(str, Enum):
    """What kind of work a step represents. Drives the icon and accent colour."""

    PREPARE = "prepare"
    ROUTING = "routing"
    REASONING = "reasoning"
    TOOL = "tool"
    HANDOFF = "handoff"
    AGENT = "agent"  # a sub-agent run; its steps nest under it
    MCP = "mcp"
    MESSAGE = "message"  # narration the agent wrote between its actions
    ERROR = "error"


class StepStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass(slots=True, frozen=True)
class ContextRef:
    """One thing the agent looked at, rendered as a chip underneath its step."""

    kind: str  # "file" | "url" | "query" | "agent"
    label: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "label": self.label, "detail": self.detail}


@dataclass(slots=True)
class Step:
    """One entry in a turn's timeline.

    ``detail`` holds the input (tool arguments), ``body`` the result or the
    reasoning text. Both are plain text; the client decides how to reveal them.
    """

    id: str
    kind: StepKind
    title: str
    status: StepStatus = StepStatus.RUNNING
    subtitle: str = ""
    detail: str = ""
    body: str = ""
    refs: list[ContextRef] = field(default_factory=list)
    at_ms: int = 0
    duration_ms: Optional[int] = None
    tone: str = "neutral"
    policy: Optional[dict[str, Any]] = None
    parent_id: Optional[str] = None  # the ``agent`` step this one runs under
    tool: str = ""  # the tool a ``tool`` or ``agent`` step called, as titled

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "status": self.status.value,
            "subtitle": self.subtitle,
            "detail": self.detail,
            "body": self.body,
            "refs": [ref.as_dict() for ref in self.refs],
            "at_ms": self.at_ms,
            "duration_ms": self.duration_ms,
            "tone": self.tone,
            "policy": self.policy,
            "parent_id": self.parent_id,
            "tool": self.tool,
        }


def summarize(value: Any, limit: int = DETAIL_LIMIT) -> str:
    """Render a tool payload as text the UI can show verbatim."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            try:  # Tool arguments usually arrive as a JSON string; pretty-print it.
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except ValueError:
                pass
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    else:
        text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... ({len(text) - limit} more characters)"


def clip(text: Any, limit: int = LABEL_LIMIT) -> str:
    """Collapse whitespace and cut to ``limit`` characters with an ellipsis."""
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _as_json(value: Any) -> Any:
    """Tool arguments arrive as dicts or as JSON strings; normalize to a dict."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


_REF_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("url", ("url", "urls", "link", "links", "source")),
    ("file", ("path", "paths", "file", "files", "file_path", "filename", "directory")),
    ("query", ("query", "q", "pattern", "search", "question", "keyword")),
)


def _ref_label(kind: str, value: str) -> str:
    if kind == "url":
        host = urlparse(value).netloc or value
        return clip(host[4:] if host.startswith("www.") else host)
    if kind == "file":
        return clip(value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or value)
    return clip(value)


def context_refs(arguments: Any) -> list[ContextRef]:
    """Pull the files, URLs and queries a tool call touched out of its arguments.

    Deliberately shallow and key-driven: the chips must never claim the agent
    read something it did not, so only well-known argument names are trusted.
    """
    payload = _as_json(arguments)
    if not isinstance(payload, dict):
        return []
    refs: list[ContextRef] = []
    seen: set[tuple[str, str]] = set()
    for kind, keys in _REF_KEYS:
        for key in keys:
            raw = payload.get(key)
            for value in raw if isinstance(raw, (list, tuple)) else [raw]:
                if not isinstance(value, str) or not value.strip():
                    continue
                detail = value.strip()
                if (kind, detail) in seen:
                    continue
                seen.add((kind, detail))
                refs.append(
                    ContextRef(kind, _ref_label(kind, detail), clip(detail, 200))
                )
    return refs


class TraceRecorder:
    """Owns the steps of a single turn: mints them, closes them, emits upserts.

    Synchronous on purpose - it is driven from the stream observer, which the
    Agents SDK calls inline. ``emit`` must therefore not block (the transport
    passes ``Queue.put_nowait``).
    """

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit
        self._clock = clock
        self._origin = clock()
        self._ids = itertools.count(1)
        self._steps: dict[str, Step] = {}
        # One live thinking step per agent: a sub-agent thinks alongside its
        # caller (and its siblings), keyed by the ``agent`` step it runs under.
        self._open_reasoning: dict[Optional[str], Step] = {}

    @property
    def elapsed_ms(self) -> int:
        return int((self._clock() - self._origin) * 1000)

    # -- step lifecycle ----------------------------------------------------
    def open(self, kind: StepKind, title: str, **fields: Any) -> Step:
        """Announce a step that is still in flight."""
        step = Step(
            id=f"s{next(self._ids)}",
            kind=kind,
            title=title,
            at_ms=self.elapsed_ms,
            **fields,
        )
        self._steps[step.id] = step
        self._publish(step)
        return step

    def close(
        self, step: Step, *, status: StepStatus = StepStatus.DONE, **fields: Any
    ) -> Step:
        """Complete a step, stamping how long it took."""
        for name, value in fields.items():
            setattr(step, name, value)
        step.status = status
        step.duration_ms = max(self.elapsed_ms - step.at_ms, 0)
        self._publish(step)
        return step

    def update(self, step: Step, **fields: Any) -> Step:
        """Patch and republish a running step without completing it."""
        for name, value in fields.items():
            setattr(step, name, value)
        self._publish(step)
        return step

    def note(self, kind: StepKind, title: str, **fields: Any) -> Step:
        """Record an instantaneous step (nothing to wait for)."""
        status = fields.pop("status", StepStatus.DONE)
        return self.close(self.open(kind, title, **fields), status=status)

    def fail(self, message: str) -> Step:
        return self.note(
            StepKind.ERROR, "Execution failed", body=message, status=StepStatus.ERROR
        )

    # -- reasoning ---------------------------------------------------------
    def reasoning_delta(self, delta: str, *, parent_id: Optional[str] = None) -> None:
        """Append a fragment of model reasoning, opening a step on first use."""
        step = self._open_reasoning.get(parent_id)
        if step is None:
            step = self.open(StepKind.REASONING, "Thinking", parent_id=parent_id)
            self._open_reasoning[parent_id] = step
        step.body += delta
        self._emit({"type": "reasoning", "id": step.id, "delta": delta})

    def end_reasoning(self, *, parent_id: Optional[str] = None) -> None:
        """Close an agent's live reasoning step, if any: the agent moved on."""
        step = self._open_reasoning.pop(parent_id, None)
        if step is None:
            return
        step.body = step.body.strip()
        if not step.body:
            self._discard(step)
            return
        self.close(step, subtitle=clip(step.body, 120))

    def end_all_reasoning(self) -> None:
        """The turn is over: settle every agent's thinking."""
        for parent_id in list(self._open_reasoning):
            self.end_reasoning(parent_id=parent_id)

    # -- persistence -------------------------------------------------------
    def snapshot(self) -> list[dict[str, Any]]:
        """The turn's timeline, for storing alongside the assistant message."""
        return [step.as_dict() for step in self._steps.values()]

    # -- internals ---------------------------------------------------------
    def _publish(self, step: Step) -> None:
        self._emit({"type": "step", "step": step.as_dict()})

    def _discard(self, step: Step) -> None:
        self._steps.pop(step.id, None)
        self._emit({"type": "step_removed", "id": step.id})


def is_tool_result(message: Any) -> bool:
    """A sub-agent report kept for the model's context, not a chat answer."""
    metadata = getattr(message, "metadata", None) or {}
    if metadata.get("kind") == "tool_result":
        return True
    # Stored before results were tagged.
    content = getattr(message, "content", None)
    return (
        getattr(message, "role", None) == "assistant"
        and isinstance(content, str)
        and content.startswith("Tool result of ")
    )


_LEGACY_KINDS = {
    "thinking": StepKind.REASONING,
    "tool_call": StepKind.TOOL,
    "tool_output": StepKind.TOOL,
    "handoff": StepKind.HANDOFF,
    "handoff_requested": StepKind.HANDOFF,
    "mcp": StepKind.MCP,
}


def normalize_steps(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Read a stored trace, upgrading the pre-``Step`` flat event format.

    Conversations saved by earlier builds hold ``{title, subtitle, details,
    status}`` dicts. Rendering them as steps keeps old chats readable instead of
    silently dropping their history.
    """
    known_status = {status.value for status in StepStatus}
    steps: list[dict[str, Any]] = []
    for index, event in enumerate(events or []):
        if not isinstance(event, dict):
            continue
        if {"id", "kind", "at_ms"} <= event.keys():
            steps.append(event)
            continue
        legacy_kind = str(event.get("kind") or "")
        at_ms = event.get("ts")
        steps.append(
            Step(
                id=f"legacy-{index}",
                kind=_LEGACY_KINDS.get(legacy_kind, StepKind.PREPARE),
                title=str(event.get("title") or legacy_kind or "Step"),
                status=(
                    StepStatus(event["status"])
                    if event.get("status") in known_status
                    else StepStatus.DONE
                ),
                subtitle=str(event.get("subtitle") or ""),
                body=str(event.get("details") or ""),
                at_ms=at_ms if isinstance(at_ms, int) else 0,
            ).as_dict()
        )
    return steps
