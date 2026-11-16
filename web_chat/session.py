"""Chat websocket transport: one turn at a time per conversation, always responsive.

A turn belongs to the user's space, not to the socket that started it. Closing or
reloading the page only detaches that socket; the agent keeps working, and a
socket that attaches later receives the turn's events so far and then follows
it live. Only an explicit ``stop`` ends a turn early.

Stop works in two steps. The first asks the agent to stop after the step it is
on: that step finishes and is saved, and the turn ends. A second Stop - a
first one before the agent has started, or one sent with ``now`` - cancels the
turn where it is. Either way the conversation records the interruption, and
``continue`` resumes it with everything the agent had done (see
core.interruption).

Commands (``stop``, the next message) are read on a loop that never awaits
inference, so a running turn can always be cancelled. Inference itself runs in a
producer task that pushes trace steps and answer tokens into a queue; this
module only forwards them and, when the turn ends, pins the recorded trace onto
the stored assistant message so a reloaded conversation shows the same timeline.

A turn begins by resolving where it runs: the client may pin a system, an agent,
both, or neither, and anything left open is routed per message.

A message sent while a turn runs is not refused: it is delivered now, at the
agent's next step, or after the turn (web_chat.delivery), as the user or the
decision model chooses. Waiting messages form the conversation's queue, which
moves on by itself when a turn answers - or when it was stopped for a ``now``
message; after a Stop or a failure the user decides.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from contextlib import suppress
from typing import Any, Awaitable, Callable, List, Optional, Sequence

from fastapi import WebSocket, WebSocketDisconnect

from core import revisions
from core.interruption import INTERRUPTED_TYPE, is_resumable
from core.pricing import format_usd
from core.steering import SteerMessage
from core.tool_check import summarize
from web_chat.attachments import (
    AttachmentError,
    agent_message,
    jpeg_urls,
    normalize_images,
    runtime_accepts_images,
    save_images,
    with_attachments,
)
from web_chat.audio_ingress import AudioAttachment, parse_audio_data_url, runtime_accepts_audio, transcribe_audio
from web_chat.accounts.service import SessionRevoked
from web_chat.delivery import DELIVERIES, MessageQueue, decide_delivery
from web_chat.errors import error_text
from web_chat.observer import WebStreamObserver
from web_chat.plan_board import PlanBoard
from web_chat.spend import TurnSpend, tracking
from web_chat.systems import Resolution
from web_chat.trace import StepKind, StepStatus, TraceRecorder, is_tool_result, slim_step
from web_chat import ui_state
from web_chat.ui_document import UiDocumentError, action_of, validate_values

logger = logging.getLogger(__name__)

#: Internal sentinel marking the end of a turn's event stream.
_FINISHED = "_finished"
#: An internal nudge that makes the consumer send its held text deltas.
_TICK = "_tick"
#: Conversation metadata: turn id -> the workspace checkpoint taken before it.
CHECKPOINTS_KEY = "checkpoints"
#: Text deltas are glued on the server: a frame leaves at most this often and
#: once this many characters are waiting. A 9,000-delta answer becomes tens of
#: frames, not thousands, and a reloaded page replays the whole text at once.
DELTA_FLUSH_MS = 60
DELTA_FLUSH_BYTES = 1536
#: One-shot frames kept for a socket that attaches later (routing, tool issues,
#: images, ...). Pin their bounded number: a busy turn cannot grow without end.
MAX_REPLAY_NOTES = 100
#: Replayed frames that are never dropped when the cap trims the rest: the
#: turn's own words to people - a public message (``say_public``) or an
#: interface it presented - are the agent's speech, not bookkeeping, and a
#: page that returns to a long turn replays every one of them.
_REPLAY_PINNED_TYPES = frozenset({"event", "ui"})
_DELTA_TYPES = frozenset({"token", "reasoning"})
#: Frames rebuilt from accumulated state instead of being kept one by one.
_STATEFUL_TYPES = frozenset({"token", "reasoning", "step", "step_removed", "answer_reset", "final_output", "done"})

#: What of a turn's timeline the map may show: a small, payload-free activity.
#: Six real steps at most, a short title, a message's public narration, and a
#: few project-relative file chips. Everything else of a step stays here.
ACTIVITY_LIMIT = 6
ACTIVITY_TITLE_LIMIT = 120
ACTIVITY_SUMMARY_LIMIT = 160
ACTIVITY_FILES_LIMIT = 4
#: The one context every trace has: the agent that ran the turn itself. A real
#: agent step opens a context of its own, nested under the context it runs in.
CONTEXT_ROOT_ID = "root"
CONTEXT_ROOT_LABEL = "Main agent"
_ACTIVITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")

#: How often an idle chat socket revalidates the session that opened it.
#: Every command revalidates too; this covers the stretches in between, when
#: the turn is long or the tab is simply left open.
SESSION_REVALIDATE_SECONDS = 30.0
_ACTIVITY_KINDS = frozenset(kind.value for kind in StepKind)
_ACTIVITY_STATUSES = frozenset(status.value for status in StepStatus)
#: A file chip never names another space of the workspace, only the project's own.
_FOREIGN_SPACE_ROOTS = frozenset({"user", "users", "shared", "projects"})


def _activity_text(value: Any, limit: int) -> str:
    """*value* as one bounded line: whitespace collapsed, cut to *limit*."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _activity_number(value: Any) -> Optional[float]:
    """A real nonnegative number, never a bool, NaN or infinity; else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if value >= 0 else None


def _activity_id(value: Any) -> Optional[str]:
    """*value* as a safe step/context id, or None: the allowlist every id meets."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _ACTIVITY_ID.fullmatch(text) else None


def _project_file_ref(value: Any) -> Optional[str]:
    """*value* as a safe project-relative POSIX path, or None.

    A plain ``src/a.py`` and a chat's ``project/src/a.py`` are the same file;
    only the project's own space is admitted. Absolute paths (``/``, ``\\``,
    a drive or a scheme), backslashes, ``.``/``..`` and empty segments, hidden
    and tool-state folders (``.beads``, ``.git``, ``.grid``, ``.codegraph``)
    and another space's root are all refused. This is a syntactic check: the
    path is not proof that the file exists.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "\\" in text or "\x00" in text or text.startswith(("/", "~")):
        return None
    if "://" in text or re.match(r"[A-Za-z]:", text):
        return None
    parts = text.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        return None
    if parts[0] == "project":
        parts = parts[1:]
    if not parts or parts[0] in _FOREIGN_SPACE_ROOTS:
        return None
    return "/".join(parts)


def _activity_files(step: dict[str, Any]) -> List[str]:
    """Up to :data:`ACTIVITY_FILES_LIMIT` unique project-relative file refs."""
    files: List[str] = []
    seen: set[str] = set()
    # An already public item carries normalized ``files``; a trace step carries
    # typed ``refs``. Both are re-normalized here, so the check holds either way.
    candidates = step.get("files")
    if not isinstance(candidates, list):
        refs = step.get("refs")
        candidates = [
            (ref.get("detail") or ref.get("label"))
            for ref in (refs if isinstance(refs, list) else [])
            if isinstance(ref, dict) and ref.get("kind") == "file"
        ]
    for candidate in candidates:
        path = _project_file_ref(candidate)
        if path is None or path in seen:
            continue
        seen.add(path)
        files.append(path)
        if len(files) >= ACTIVITY_FILES_LIMIT:
            break
    return files


def _activity_item(step: Any) -> Optional[dict[str, Any]]:
    """One step as the map may show it, or None when it is not a real step."""
    if not isinstance(step, dict):
        return None
    kind, status = step.get("kind"), step.get("status")
    if kind not in _ACTIVITY_KINDS or status not in _ACTIVITY_STATUSES:
        return None
    step_id = step.get("id")
    if _activity_id(step_id) is None:
        return None
    title = _activity_text(step.get("title"), ACTIVITY_TITLE_LIMIT)
    if not title:
        return None
    # Only a message's subtitle is public narration; every other subtitle and
    # body (a reasoning step's own thinking included) must never leave here.
    summary = ""
    if kind == StepKind.MESSAGE.value:
        summary = _activity_text(
            step.get("summary") or step.get("subtitle"), ACTIVITY_SUMMARY_LIMIT
        )
    return {
        "id": _activity_id(step_id),
        "kind": kind,
        "title": title,
        "status": status,
        "at_ms": _activity_number(step.get("at_ms")),
        "duration_ms": _activity_number(step.get("duration_ms")),
        "summary": summary,
        "files": _activity_files(step),
    }


def _effective_parents(direct: dict[str, str]) -> dict[str, str]:
    """Every context's acyclic parent: a missing id or a cycle falls back to root.

    A chain that returns to its own start is a cycle, so that context hangs
    from root. A chain that reaches a cycle elsewhere keeps its direct parent:
    those cyclic contexts are themselves rooted here, so the tree stays finite.
    """
    resolved: dict[str, str] = {}
    for context_id, parent in direct.items():
        effective = parent
        seen: set[str] = set()
        current = parent
        while current != CONTEXT_ROOT_ID:
            if current == context_id:
                effective = CONTEXT_ROOT_ID
                break
            if current in seen:
                break
            seen.add(current)
            current = direct.get(current, CONTEXT_ROOT_ID)
        resolved[context_id] = effective
    return resolved


def _activity_contexts(
    raw: List[Any],
) -> tuple[List[dict[str, Any]], List[Optional[str]], bool]:
    """The public hierarchy of *raw* steps and each step's owning context.

    A real ``agent`` step opens a context of its own, labelled with its title
    and timed by its own status and timings; it is also an event of the context
    it was delegated from. Every ordinary step belongs to the nearest existing
    agent context its ``parent_id`` names, or to root. ``total`` counts every
    raw step of a context before any public bound, and contexts keep every
    agent step even when its events are older than the six shown items.

    The third answer says whether a real trace exists at all: only then does
    root appear, so an empty or unreadable activity stays ``contexts: []``.
    """
    steps: List[tuple[int, dict[str, Any]]] = [
        (index, step) for index, step in enumerate(raw) if isinstance(step, dict)
    ]
    if not steps:
        return [], [None] * len(raw), False

    # Agent contexts in trace order; a repeated id is one context, first wins.
    agent_steps: List[tuple[str, dict[str, Any]]] = []
    agent_ids: set[str] = set()
    for _, step in steps:
        if step.get("kind") != StepKind.AGENT.value:
            continue
        agent_id = _activity_id(step.get("id"))
        if agent_id is None or agent_id in agent_ids:
            continue
        agent_ids.add(agent_id)
        agent_steps.append((agent_id, step))

    direct: dict[str, str] = {}
    for agent_id, step in agent_steps:
        parent_id = _activity_id(step.get("parent_id"))
        direct[agent_id] = (
            parent_id
            if parent_id is not None
            and parent_id != agent_id
            and parent_id in agent_ids
            else CONTEXT_ROOT_ID
        )
    parents = _effective_parents(direct)

    owners: List[Optional[str]] = [None] * len(raw)
    totals: dict[str, int] = {CONTEXT_ROOT_ID: 0}
    for index, step in steps:
        if step.get("kind") == StepKind.AGENT.value:
            agent_id = _activity_id(step.get("id"))
            if agent_id is not None and agent_id in parents:
                owner = parents[agent_id]
            else:  # a repeated or malformed agent row is still a delegation event
                parent_id = _activity_id(step.get("parent_id"))
                owner = (
                    parent_id
                    if parent_id is not None
                    and parent_id != agent_id
                    and parent_id in agent_ids
                    else CONTEXT_ROOT_ID
                )
        else:
            parent_id = _activity_id(step.get("parent_id"))
            owner = (
                parent_id
                if parent_id is not None and parent_id in agent_ids
                else CONTEXT_ROOT_ID
            )
        owners[index] = owner
        totals[owner] = totals.get(owner, 0) + 1

    root_statuses = [
        step.get("status")
        for index, step in steps
        if owners[index] == CONTEXT_ROOT_ID
    ]
    if StepStatus.RUNNING.value in root_statuses:
        root_status = StepStatus.RUNNING.value
    elif StepStatus.DONE.value in root_statuses:
        root_status = StepStatus.DONE.value
    else:
        root_status = None

    contexts: List[dict[str, Any]] = [{
        "id": CONTEXT_ROOT_ID,
        "parent_id": None,
        "label": CONTEXT_ROOT_LABEL,
        "status": root_status,
        "at_ms": None,
        "duration_ms": None,
        "total": totals.get(CONTEXT_ROOT_ID, 0),
    }]
    for agent_id, step in agent_steps:
        status = step.get("status")
        contexts.append({
            "id": agent_id,
            "parent_id": parents.get(agent_id, CONTEXT_ROOT_ID),
            "label": _activity_text(step.get("title"), ACTIVITY_TITLE_LIMIT),
            "status": status if status in _ACTIVITY_STATUSES else None,
            "at_ms": _activity_number(step.get("at_ms")),
            "duration_ms": _activity_number(step.get("duration_ms")),
            "total": totals.get(agent_id, 0),
        })
    return contexts, owners, True


def _public_contexts(value: Any) -> List[dict[str, Any]]:
    """An already public context list, sanitized again into the same contract."""
    entries = (
        [entry for entry in value if isinstance(entry, dict)]
        if isinstance(value, list)
        else []
    )
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        context_id = _activity_id(entry.get("id"))
        if context_id is None or context_id in by_id:
            continue
        total = entry.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            total = 0
        status = entry.get("status")
        by_id[context_id] = {
            "id": context_id,
            "parent_id": _activity_id(entry.get("parent_id")),
            "label": _activity_text(entry.get("label"), ACTIVITY_TITLE_LIMIT),
            "status": status if status in _ACTIVITY_STATUSES else None,
            "at_ms": _activity_number(entry.get("at_ms")),
            "duration_ms": _activity_number(entry.get("duration_ms")),
            "total": total,
        }
    if CONTEXT_ROOT_ID in by_id:
        root = by_id[CONTEXT_ROOT_ID]
        root.update(
            parent_id=None, label=CONTEXT_ROOT_LABEL, at_ms=None, duration_ms=None
        )

    direct = {
        context_id: (
            context["parent_id"]
            if context["parent_id"] is not None
            and context["parent_id"] != context_id
            and context["parent_id"] in by_id
            else CONTEXT_ROOT_ID
        )
        for context_id, context in by_id.items()
        if context_id != CONTEXT_ROOT_ID
    }
    parents = _effective_parents(direct)
    contexts: List[dict[str, Any]] = []
    if CONTEXT_ROOT_ID in by_id:
        contexts.append(by_id[CONTEXT_ROOT_ID])
    for context_id, context in by_id.items():
        if context_id == CONTEXT_ROOT_ID:
            continue
        context["parent_id"] = parents.get(context_id, CONTEXT_ROOT_ID)
        contexts.append(context)
    return contexts


def _public_activity(value: dict[str, Any]) -> dict[str, Any]:
    """An already public activity, re-checked: no wider shape reaches a client."""
    contexts = _public_contexts(value.get("contexts"))
    context_ids = {context["id"] for context in contexts}
    items: List[dict[str, Any]] = []
    for entry in value.get("items") if isinstance(value.get("items"), list) else []:
        item = _activity_item(entry)
        if item is None:
            continue
        context_id = _activity_id(entry.get("context_id"))
        if context_id not in context_ids:
            context_id = CONTEXT_ROOT_ID if CONTEXT_ROOT_ID in context_ids else None
        if context_id is None:
            continue
        item["context_id"] = context_id
        items.append(item)
    if items and CONTEXT_ROOT_ID not in context_ids:
        statuses = [item["status"] for item in items]
        root_status = (
            StepStatus.RUNNING.value if StepStatus.RUNNING.value in statuses
            else StepStatus.DONE.value if StepStatus.DONE.value in statuses
            else None
        )
        contexts.insert(0, {
            "id": CONTEXT_ROOT_ID,
            "parent_id": None,
            "label": CONTEXT_ROOT_LABEL,
            "status": root_status,
            "at_ms": None,
            "duration_ms": None,
            "total": len(items),
        })
        for item in items:
            if item["context_id"] not in {context["id"] for context in contexts}:
                item["context_id"] = CONTEXT_ROOT_ID
        context_ids = {context["id"] for context in contexts}

    total = value.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = len(items)
    total = max(total, len(items))
    return {
        "items": items[-ACTIVITY_LIMIT:],
        "total": total,
        "truncated": total > ACTIVITY_LIMIT,
        "contexts": contexts,
    }


def public_activity(steps: Any) -> dict[str, Any]:
    """A bounded, payload-free activity list of *steps*, for the map.

    Every item is an allowlisted projection of one real trace step; each maps
    to the same shape whether the step comes from a live recorder, a stored
    trace or an already public activity item. ``total`` counts every valid
    step before the bound, so a truncated list never hides how much there was.

    ``contexts`` is the trace's agent hierarchy: root for the turn's own agent
    and one context per real ``agent`` step, each item naming the context that
    owns it. An already public activity (a dict) is only re-sanitized, so live
    and stored reads pass through this same projector without widening it.
    """
    if isinstance(steps, dict):
        return _public_activity(steps)
    raw = list(steps) if isinstance(steps, (list, tuple)) else []
    contexts, owners, real = _activity_contexts(raw)
    items: List[dict[str, Any]] = []
    context_ids = {context["id"] for context in contexts}
    for index, step in enumerate(raw):
        item = _activity_item(step)
        if item is None:
            continue
        owner = owners[index] if index < len(owners) else None
        item["context_id"] = owner if owner in context_ids else CONTEXT_ROOT_ID
        if item["context_id"] not in context_ids:
            continue
        items.append(item)
    return {
        "items": items[-ACTIVITY_LIMIT:],
        "total": len(items),
        "truncated": len(items) > ACTIVITY_LIMIT,
        "contexts": contexts if real else [],
    }


def revision_error_event(exc: revisions.ContextCompatibilityError) -> dict[str, Any]:
    """A refusal the client can act on: reason, machine code, conversation, details."""
    return {
        "type": "error",
        "content": error_text(exc),
        "code": exc.code,
        "context_id": exc.context_id,
        "details": exc.details,
    }


def revision_notice_event(notice: dict[str, Any]) -> dict[str, Any]:
    """A re-pin the client is told about: the turn continues, so it is not an error.

    ``type`` is ``revision_notice`` - a client keys on it, never on the text.
    ``code`` is the machine reason (one of ``revisions.CONTINUABLE_CODES``):
    ``revision.legacy_context``, ``revision.grid_version_changed``,
    ``revision.system_config_changed`` or ``revision.runtime_changed``.
    ``content`` is the sentence to show, ``details.changes`` every graded
    difference and ``details.previous_digest`` / ``details.grid_version`` what
    the conversation came from and now runs on.
    """
    details = dict(notice.get("details") or {})
    return {
        "type": "revision_notice",
        "code": notice.get("code"),
        "content": notice.get("reason") or "",
        "context_id": notice.get("context_id"),
        "details": details,
    }


#: Checkpoints a conversation keeps, newest first; files listed per turn.
MAX_CHECKPOINTS = 40
MAX_LISTED_CHANGES = 200


class AgentTurn:
    """A single agent run, streamed to every socket attached to it.

    Owns the recorder for this turn and knows how to persist its trace. The
    producer/consumer split keeps the socket writable while the agent works.
    Every event is also kept, so a socket attaching mid-turn can be replayed
    to exactly where the others are.
    """

    def __init__(
        self,
        session: "ChatSession",
        message: Optional[str],
        *,
        system_key: Optional[str],
        agent_key: Optional[str],
        images: tuple[str, ...] = (),
        images_saved: bool = False,
        audio: Optional[AudioAttachment] = None,
        edit_of: Optional[str] = None,
        ui_origin: Optional[dict[str, Any]] = None,
    ) -> None:
        """``message`` None is Continue: the turn resumes the conversation's
        interrupted turn with the agent that ran it. ``images`` are the
        message's images (web_chat.attachments); ``images_saved`` says their
        files and link block were already written when the message arrived -
        a message sent while another turn ran."""
        self._session = session
        self._message = message
        self._images = tuple(images)
        self._images_saved = images_saved
        self._audio = audio
        self._edit_of = edit_of
        #: Where the request came from, when a UI button sent it (web_chat.ui_state).
        self._ui_origin = ui_origin
        self._requested = (system_key, agent_key)
        self._factory: Any = None  # set once the turn knows where it runs
        self._resolution: Optional[Resolution] = None
        self._stop_requested = False
        self.restart_paused = False
        self.user_stopped = False
        #: The admission gate: the turn's task starts at once and waits here
        #: (run) until its preamble (ChatSession._run_turn_preamble) admits it,
        #: so the conversation shows its active turn - and a Stop or a queued
        #: message reaches it - while the revision check, recovery and the
        #: admission still run.
        self._admitted: asyncio.Event = asyncio.Event()
        #: Whether the preamble let the turn go before it ever ran.
        self._refused = False
        #: How the turn ended: "answered", "interrupted", "error" or "stopped".
        self.outcome: Optional[str] = None
        self._answer_tail = ""  # the agent's latest words, for delivery decisions
        self.run_id = uuid.uuid4().hex
        self._started = time.monotonic()
        #: The answer so far, rebuilt as one ``token`` frame on attach.
        self._answer = ""
        #: One-shot frames a late socket still needs (routing, issues, images, ...).
        self._notes: list[dict[str, Any]] = []
        #: Text deltas not sent yet, in arrival order.
        self._pending: list[dict[str, Any]] = []
        self._pending_bytes = 0
        self._subscribers: set["ChatSession"] = {session}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._recorder = TraceRecorder(self._queue.put_nowait, on_usage=self._check_token_budget)
        #: Whether the turn was already stopped for passing its token budget.
        self._budget_hit = False
        #: What this turn's model calls cost (web_chat.spend), and whether it was
        #: already stopped for passing its dollar budget.
        self._spend = TurnSpend(self._check_usd_budget)
        self._usd_budget_hit = False
        #: The workspace before the agent ran (core.checkpoints), for Restore.
        self._checkpoint: Optional[dict] = None
        self._observer = WebStreamObserver(
            self._recorder,
            emit_token=self._token,
            reset_answer=lambda: self._queue.put_nowait({"type": "answer_reset"}),
            emit_image=lambda url: self._queue.put_nowait({"type": "image", "url": url}),
            # A public typed event of this turn, live: the page draws it at once
            # (web_chat.observer builds the frame; chat.js listens for "event").
            emit_event=lambda frame: self._queue.put_nowait(frame),
            # Every event of the turn carries the id a replay and a stored
            # message agree on (the same turn_id keys the checkpoints).
            turn_id=self.run_id,
            plan=PlanBoard.continuing(self._recorder, self._stored_messages()),
        )

    def _stored_messages(self) -> list[dict[str, Any]]:
        """The conversation's recent messages, for the plan its last turns left."""
        try:
            snapshot = self._session.manager.get_context_messages(self._session.context_id, limit=40)
        except Exception:
            logger.debug("No stored messages to continue the plan from", exc_info=True)
            return []
        return list((snapshot or {}).get("messages") or [])

    @property
    def message(self) -> str:
        return self._message if self._message is not None else ""

    @property
    def resumes(self) -> bool:
        return self._message is None

    @property
    def answer_tail(self) -> str:
        return self._answer_tail

    def _token(self, text: str) -> None:
        self._answer_tail = (self._answer_tail + text)[-2000:]
        self._queue.put_nowait({"type": "token", "content": text})

    def recent_steps(self) -> list[str]:
        """Titles of the turn's latest timeline steps."""
        return [step.get("title", "") for step in self._recorder.snapshot()][-8:]

    def recent_activity(self) -> dict[str, Any]:
        """The turn's latest steps as the map shows them: bounded, typed, no payloads.

        A public read for the map (web_chat.map_api): each item carries only
        its id, kind, title, status, timings, a message's public narration and
        safe project-relative file refs. A reasoning step's body and subtitle,
        every step's body, detail, input and result payload and a plan's
        content stay in the turn; this is a projection, never a recorder
        snapshot.
        """
        return public_activity(self._recorder.snapshot())

    def plan_snapshot(self) -> dict[str, Any]:
        """The plan of this turn, live, as its board holds it now.

        A public read for the map (web_chat.map_api): the tasks the turn's
        tracker calls changed, and whether this turn touched each of them.
        """
        return self._observer.plan_snapshot()

    def step_record(self, step_id: str) -> Optional[dict[str, Any]]:
        """A step of this turn with its payloads, for a lazy step opened live."""
        return self._recorder.step_record(step_id)

    async def steer(self, item: dict[str, Any], queue: "MessageQueue") -> bool:
        """Hand a queued message to the running agent for its next step.

        The message's files were kept and named in its text when it arrived
        (:meth:`ChatSession._keep_images`); the pictures themselves go to the
        agent only when the model this turn runs on may read them - as they do
        for a message that starts a turn.

        Delivered, it leaves the queue and shows in the chat; if the turn ends
        first it waits in the queue for the next turn. False when the agent
        has not started or cannot take messages mid-run.
        """
        if self._factory is None or not hasattr(self._factory, "steer"):
            return False
        images = list(item.get("images") or [])
        if images and self._resolution is not None and not await self._images_reach_model(self._resolution):
            images = []

        def delivered(message: SteerMessage) -> None:
            queue.remove(message.message_id)
            self._queue.put_nowait({"type": "steered", "id": message.message_id, "text": message.text, "images": message.images})
            self._queue.put_nowait({"type": "queue", "items": queue.public()})

        def undelivered(message: SteerMessage) -> None:
            queue.requeue(message.message_id)

        return self._factory.steer(
            self._session.context_id,
            SteerMessage(item["id"], item["text"], images, delivered, undelivered),
        )

    def withdraw(self, item_id: str) -> bool:
        """Take back a message handed by :meth:`steer` that the agent has not read."""
        if self._factory is None or not hasattr(self._factory, "withdraw_steer"):
            return False
        return self._factory.withdraw_steer(self._session.context_id, item_id)

    @property
    def tokens(self) -> tuple[int, int]:
        """(in, out): the tokens this turn's model calls reported so far."""
        return self._recorder.tokens_in, self._recorder.tokens_out

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    async def attach(self, session: "ChatSession") -> None:
        """Replay the turn's state to *session*, then stream to it live.

        The replay is a snapshot - the steps as they are now, the answer so
        far, the few one-shot notices - not the frame journal: a page that
        comes back mid-turn pays for what is on screen, not for every delta
        that produced it. Snapshotting and subscribing happen with no await in
        between, so every event lands exactly once: in the replay or live.
        """
        async with session.send_lock:
            replay = self._snapshot_frames()
            self._subscribers.add(session)
            header = {
                "type": "attached",
                "message": self.message,
                "resumes": self.resumes,
                "elapsed_ms": self.elapsed_ms,
                "run_id": self.run_id,
            }
            for event in (header, *replay):
                await session.send_unlocked(event)

    def detach(self, session: "ChatSession") -> None:
        self._subscribers.discard(session)

    def request_stop(self) -> bool:
        """The graceful Stop; True when the agent will stop after its step.

        False when that is not possible - the agent has not started yet, or
        this is the second Stop - and the caller cancels the turn instead.
        """
        if self._stop_requested or self._factory is None:
            return False
        self._stop_requested = True
        return self._factory.request_stop(self._session.context_id)

    def pause_for_restart(self) -> None:
        """Retry while preparing; never cancel a model response or a tool call."""
        if self._stop_requested or self.user_stopped or self._factory is None:
            return
        if self._factory.request_stop(self._session.context_id):
            self._stop_requested = True
            self.restart_paused = True

    def _check_token_budget(self, tokens_in: int, tokens_out: int) -> None:
        """Stop this turn at the next step once it passed its token budget.

        The recorder calls this after every model response with the turn's
        tokens so far (web_chat.trace); the budget is the user_limits
        ``max_tokens_per_turn``.
        """
        budget = self._token_budget()
        if budget is None or self._budget_hit or tokens_in + tokens_out <= budget:
            return
        self._budget_hit = True
        self._recorder.note(
            StepKind.ERROR,
            "Token budget exceeded",
            subtitle=f"{tokens_in + tokens_out:,} tokens spent of the {budget:,} allowed for one turn",
            body=(
                "The turn stops at the next step; the work done so far stays. "
                "Continue with a narrower request, or ask to raise "
                "user_limits.max_tokens_per_turn."
            ),
            tone="warn",
        )
        self.request_stop()

    def _check_usd_budget(self, charged_micro: int) -> None:
        """Stop this turn at the next step once its calls cost more than the user_limits
        ``usd_per_turn``; the spend arrives (web_chat.spend) as each call finishes."""
        limits = getattr(self._session.space, "limits", None)
        usd_budget = getattr(limits, "usd_budget", None)
        budget = usd_budget() if callable(usd_budget) else None
        if budget is None or self._usd_budget_hit or charged_micro <= budget:
            return
        self._usd_budget_hit = True
        self._recorder.note(
            StepKind.ERROR,
            "Cost budget exceeded",
            subtitle=f"{format_usd(charged_micro)} spent of the {format_usd(budget)} allowed for one turn",
            body=(
                "The turn stops at the next step; the work done so far stays. "
                "Continue with a narrower request, or ask to raise user_limits.usd_per_turn."
            ),
            tone="warn",
        )
        self.request_stop()

    def _token_budget(self) -> Optional[int]:
        limits = getattr(self._session.space, "limits", None)
        token_budget = getattr(limits, "token_budget", None)
        return token_budget() if callable(token_budget) else None

    async def announce(self, event: dict[str, Any], *, also_to: Optional["ChatSession"] = None) -> None:
        """Send an event from outside the producer, to every viewer.

        ``also_to`` reaches a socket that is not following the turn - the tab
        that sent a message while another one watches the agent work.
        """
        await self._emit(event)
        if also_to is not None and also_to not in self._subscribers:
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                await also_to.send({**event, "run_id": self.run_id})

    async def run(self) -> None:
        # The turn is registered before its preamble admits it (see
        # ChatSession._run_turn_preamble): the task waits here, already visible
        # as the conversation's active turn, until the admission. Refused - an
        # incompatible revision, the day's quota, a Stop while it prepared - it
        # ends here without a frame: the preamble sent the refusal, and the
        # claim and the record went back in the preamble's release.
        await self._admitted.wait()
        if self._refused:
            return
        # The producer starts from this context: its model calls count in the turn's spend.
        with tracking(self._spend):
            producer = asyncio.create_task(self._produce())
        stopped = False
        cause: tuple[Any, ...] = ()
        try:
            await self._consume()
        except asyncio.CancelledError as cancelled:
            stopped = True
            self.outcome = "stopped"
            cause = cancelled.args  # the producer records why (core.interruption.SHUTDOWN)
        finally:
            if not producer.done():
                producer.cancel(*cause)
            with suppress(asyncio.CancelledError):
                await producer
            self._persist_trace()
            with suppress(Exception, asyncio.CancelledError):
                await self._record_checkpoint()
            # Stopped, timed out or failed: say so with what Continue needs.
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                if await self._announce_interruption() and self.outcome != "stopped":
                    self.outcome = "interrupted"
            self._record_activity()
            self._record_usage()
            # The claim passes on here, in the same event-loop step as the
            # turn's last work: whichever message waits in the queue to start
            # next is claimed before this one lets go, so a GET from a
            # returning tab never lands between the two turns and finds no
            # active turn (ChatSession._succeed_queued). "done" follows it:
            # by the time the client can answer, the conversation is free - or
            # already in its next turn.
            self._session.hand_over(self)
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                done = {
                    "type": "done",
                    "stopped": stopped,
                    "duration_ms": self._recorder.elapsed_ms,
                    "tokens_in": self._recorder.tokens_in,
                    "tokens_out": self._recorder.tokens_out,
                }
                # The model's last prompt size: the context this turn ended
                # with. Absent when no call reported usage, never a zero.
                if self._recorder.last_input_tokens > 0:
                    done["context_tokens"] = self._recorder.last_input_tokens
                await self._emit(done)

    # -- producer ----------------------------------------------------------
    async def _produce(self) -> None:
        space = self._session.space
        try:
            # Auto-routing needs words; never ask the router to interpret base64.
            if self._audio is not None and not all(self._requested):
                await self._transcribe_audio()
            resolution = self._resolution = await self._resolve()
            label = self._session.agent_label(resolution.system, resolution.agent)
            self._observer.agent_label = label
            self._warn_about_tools(resolution, label)

            step = self._recorder.open(
                StepKind.PREPARE,
                "Preparing the runtime",
                subtitle=f"{resolution.system} · {resolution.agent}",
                body=f"Workspace: {space.workspace_label_of(self._session.context_id)}",
            )
            # Routing may land on another system than the one the conversation
            # is pinned to. That is refused here - explicitly, with its code -
            # before an MCP server is warmed or any model call is made. A
            # conversation with nothing pinned (a new or a legacy one) is
            # pinned or re-pinned by its turn below.
            manifest = revisions.preflight(self._session.manager, self._session.context_id)
            if manifest is not None and getattr(resolution.config, "config", None) is not None:
                revisions.compatible(manifest, resolution.config, self._session.context_id)
            await space.warm_agent(resolution.agent, resolution.system, context_id=self._session.context_id)
            if self._audio is not None:
                runtime = await resolution.factory.create_agent(resolution.agent)
                if not runtime_accepts_audio(runtime, self._audio):
                    await self._transcribe_audio()
            self._recorder.close(step, title=f"Runtime ready · {label}")
            self._checkpoint = await self._snapshot()

            space.update_conversation_metadata(
                self._session.context_id,
                created_by_web=True,
                system_key=self._requested[0],
                agent_key=self._requested[1],
                routed_system=resolution.system,
                routed_agent=resolution.agent,
                # Continue has no text of its own to name the chat by.
                title=self._title(),
            )
            self._factory = resolution.factory
            from core.fallback_model import ModelSelection, model_selection

            selection = ModelSelection()
            with model_selection(selection):
                if self._message is None:
                    result = await resolution.factory.continue_agent(
                        resolution.agent,
                        self._session.context_id,
                        stream=True,
                        user_id=space.user_id,
                        stream_observer=self._observer,
                        turn_id=self.run_id,
                    )
                else:
                    text, images = await self._message_for_agent(resolution)
                    result = await resolution.factory.run_agent(
                        agent_key=resolution.agent,
                        message=agent_message(text, images, self._audio.data_url if self._audio else None),
                        edit_of=self._edit_of,
                        context_id=self._session.context_id,
                        stream=True,
                        user_id=space.user_id,
                        stream_observer=self._observer,
                        turn_id=self.run_id,
                    )
            self._recorder.end_all_reasoning()
            self.outcome = "answered"
            if result:
                self._queue.put_nowait({"type": "final_output", "content": str(result)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.outcome = "error"
            logger.exception("Web chat run failed")
            self._recorder.fail(error_text(exc))
            event = (
                revision_error_event(exc)
                if isinstance(exc, revisions.ContextCompatibilityError)
                else {"type": "error", "content": error_text(exc)}
            )
            self._queue.put_nowait(event)
        finally:
            drop = getattr(self._observer, "drop_unwritten_calls", None)
            if drop is not None:
                drop()
            self._queue.put_nowait({"type": _FINISHED})

    def _warn_about_tools(self, resolution: Resolution, label: str) -> None:
        """Say which tools will fail before the agent starts; nothing is disabled."""
        try:
            registry = self._session.space.registry_of(self._session.context_id)
            issues = registry.agent_issues(resolution.system, resolution.agent)
        except Exception as exc:  # the check must never cost the turn
            logger.warning("Tool check failed for %s/%s: %s", resolution.system, resolution.agent, exc)
            return
        if not issues:
            return
        lines = summarize(issues)
        self._recorder.note(
            StepKind.ERROR,
            f"{len(lines)} tool problem(s) · {label}",
            subtitle="The tools stay enabled, but calls to them will likely fail",
            body="\n".join(f"- {line}" for line in lines),
            tone="warn",
        )
        self._queue.put_nowait({
            "type": "tool_issues",
            "system": resolution.system,
            "agent": resolution.agent,
            "agent_name": label,
            "summary": lines,
            "issues": [issue.to_dict() for issue in issues],
        })

    async def _transcribe_audio(self) -> None:
        """Both browser and Telegram enter here, at most once per recording."""
        if self._audio is None:
            return
        step = self._recorder.open(StepKind.PREPARE, "Transcribing audio locally")
        service = getattr(self._session.space.deployment, "voice_service", None)
        transcript = await transcribe_audio(self._audio, service)
        caption = self._message or ""
        self._message = f"{caption}\n\n{transcript}" if caption else transcript
        self._audio = None
        self._recorder.close(step, title="Audio transcribed")
        self._queue.put_nowait({"type": "audio_transcribed", "text": transcript})

    async def _message_for_agent(self, resolution: Resolution) -> tuple[str, list[str]]:
        """What the agent reads: the text with a link to every image, and the
        images themselves only when a model of this runtime may see them.

        The files are stored either way, so a chat keeps the picture as a file
        of its own space whatever the model can read - the message then names
        it in the chat's ``Attached files:`` block, with its download URL and
        its workspace path. A file that could not be stored is an error of the
        turn, never a picture quietly dropped.
        """
        text, _images = await self._session._keep_images(
            self._message or "", () if self._images_saved else self._images
        )
        if not self._images or not await self._images_reach_model(resolution):
            return text, []
        return text, jpeg_urls(self._images)

    async def _images_reach_model(self, resolution: Resolution) -> bool:
        """Whether the runtime that is about to run may read a picture.

        Asking builds the agent - which the turn does right after anyway, and
        the factory caches it. A factory that cannot be asked, or a runtime
        that fails to build here, keeps the images: only a model known to
        refuse them is spared them.
        """
        create = getattr(resolution.factory, "create_agent", None)
        if not callable(create):
            return True
        try:
            runtime = await create(resolution.agent)
        except Exception as exc:  # noqa: BLE001 - the turn reports a real build failure
            logger.warning("Could not tell whether %s reads images: %s", resolution.agent, exc)
            return True
        return runtime_accepts_images(runtime)

    async def _resolve(self) -> Resolution:
        """Decide the system and agent, showing the routing as its own step.

        While the conversation ends with an interrupted turn, the turn goes to
        the agent that ran it unless the user picked one: only that agent's
        session holds the work to resume.
        """
        system_key, agent_key = self._requested
        interrupted_by = self._interrupted_agent()
        if interrupted_by is not None and (self.resumes or (system_key is None and agent_key is None)):
            system_key, agent_key = interrupted_by
        elif self.resumes:
            raise RuntimeError("There is no interrupted turn to continue in this conversation.")
        routing_step = None
        if system_key is None or agent_key is None:
            routing_step = self._recorder.open(
                StepKind.ROUTING,
                "Choosing where to run",
                subtitle="No system pinned" if system_key is None else f"System: {system_key}",
            )

        resolution = await self._session.space.resolve_turn(
            self._routing_text(),
            system_key=system_key,
            agent_key=agent_key,
            context_id=self._session.context_id,
        )

        if routing_step is not None:
            self._recorder.close(
                routing_step,
                title=f"Routed to {resolution.system} → {resolution.agent}",
                subtitle=resolution.warning or self._session.agent_label(resolution.system, resolution.agent),
                body=resolution.warning,
            )
        self._queue.put_nowait({
            "type": "routed",
            "system": resolution.system,
            "agent": resolution.agent,
            "agent_name": self._session.agent_label(resolution.system, resolution.agent),
            "routed": resolution.routed,
        })
        return resolution

    def _title(self) -> Optional[str]:
        """What names the chat after this message; None leaves the name alone."""
        if self._message is None:
            return None
        text = " ".join(self._message.split())[:42]
        return text or ("Audio message" if self._audio else "Image" if self._images else None)

    def _routing_text(self) -> str:
        """What the router reads: the text, or a word that there are images."""
        if self.message or not self._images:
            return self.message
        return f"[{len(self._images)} image(s) attached]"

    def _interrupted_agent(self) -> Optional[tuple[str, str]]:
        """(system, agent) of the interrupted turn the conversation ends with."""
        manager = self._session.manager
        context_id = self._session.context_id
        if manager.pending_interruption(context_id) is None:
            return None
        metadata = manager.get_context_metadata(context_id)
        system, agent = metadata.get("routed_system"), metadata.get("routed_agent")
        return (system, agent) if system and agent else None

    async def _announce_interruption(self) -> bool:
        """Send the interruption this turn recorded, if it recorded one; True if so."""
        view = self._session.manager.conversation_view(self._session.context_id)
        for message in reversed((view or {}).get("messages", [])):
            metadata = getattr(message, "metadata", None) or {}
            if metadata.get("turn_id") != self.run_id:
                continue
            if metadata.get("type") == INTERRUPTED_TYPE:
                await self._emit({
                    "type": "interrupted",
                    "content": message.content,
                    "interruption": {
                        **metadata.get("interruption", {}),
                        "resumable": is_resumable(message),
                    },
                })
                return True
            return False
        return False

    # -- consumer ----------------------------------------------------------
    async def _consume(self) -> None:
        ticker = asyncio.create_task(self._ticks())
        try:
            while True:
                event = await self._queue.get()
                if event["type"] == _TICK:
                    await self._flush_deltas()
                    continue
                if event["type"] == _FINISHED:
                    await self._flush_deltas()
                    return
                if event["type"] in _DELTA_TYPES:
                    self._buffer_delta(event)
                    if self._pending_bytes >= DELTA_FLUSH_BYTES:
                        await self._flush_deltas()
                    continue
                # A step or a notice is a boundary: the text before it goes first.
                await self._flush_deltas()
                await self._emit(event)
        finally:
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
            # A turn stopped here - cancelled, out of budget, shut down - has
            # already produced text the tabs have not seen: the queued deltas
            # are taken into the buffer and the buffer is sent, before run()
            # says "done". Losing that tail made a stopped answer appear cut.
            self._drain_deltas()
            await self._flush_deltas()

    async def _ticks(self) -> None:
        """Nudge the consumer every :data:`DELTA_FLUSH_MS`, so text flows while the stream is quiet."""
        while True:
            await asyncio.sleep(DELTA_FLUSH_MS / 1000)
            self._queue.put_nowait({"type": _TICK})

    def _buffer_delta(self, event: dict[str, Any]) -> None:
        """Hold a text fragment, merging it with the one before when they match."""
        kind = event["type"]
        text = str(event.get("delta") if kind == "reasoning" else event.get("content") or "")
        if not text:
            return
        key = (kind, event.get("id"))
        last = self._pending[-1] if self._pending else None
        if last is not None and (last["type"], last.get("id")) == key:
            field = "delta" if kind == "reasoning" else "content"
            last[field] += text
        else:
            frame = {"type": kind, **({"id": event["id"]} if kind == "reasoning" else {}),
                     ("delta" if kind == "reasoning" else "content"): text}
            self._pending.append(frame)
        self._pending_bytes += len(text)
        if kind == "token":
            self._answer += text

    def _drain_deltas(self) -> None:
        """Move the text still waiting in the queue into the buffer (the rest is
        the recorder's business: its state is replayed, not its frames)."""
        while True:
            try:
                event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if event.get("type") in _DELTA_TYPES:
                self._buffer_delta(event)

    async def _flush_deltas(self) -> None:
        """Send the held text fragments as one frame per run of same-purpose text.

        Frame by frame, the one being sent leaving the buffer first: a flush
        cancelled in the middle of a send neither sends a frame twice nor loses
        the frames behind it - the flush in :meth:`_consume`'s finally has them.
        """
        while self._pending:
            await self._emit(self._pending.pop(0))
        self._pending_bytes = 0

    def _snapshot_frames(self) -> list[dict[str, Any]]:
        """What a socket attaching now needs: the state, not how it was reached."""
        frames = [dict(note) for note in self._notes]
        for step in self._recorder.snapshot():
            frames.append({"type": "step", "step": slim_step(step)})
        if self._answer:
            frames.append({"type": "token", "content": self._answer})
        return [{**frame, "run_id": self.run_id} for frame in frames]

    async def _emit(self, event: dict[str, Any]) -> None:
        frame = {**event, "run_id": self.run_id}
        # What a late socket is rebuilt from: the answer and one-shot notices.
        # Steps come from the recorder's current state; deltas are glued above.
        if frame["type"] == "answer_reset":
            self._answer = ""
        elif frame["type"] == "final_output":
            self._answer = str(frame.get("content") or "")
        elif frame["type"] not in _STATEFUL_TYPES and frame["type"] != "token":
            self._remember_replay(event)
        for session in list(self._subscribers):
            try:
                await session.send(frame)
            except (WebSocketDisconnect, RuntimeError, OSError):
                # A closed tab must not stall the agent or the other viewers.
                self._subscribers.discard(session)

    def _remember_replay(self, event: dict[str, Any]) -> None:
        """Keep a one-shot frame for a socket that attaches later.

        The cap bounds the bookkeeping frames (queue snapshots, delivery
        notices, images); the pinned ones - a public message the agent said
        with ``say_public``, an interface it presented - stay whole however
        long the turn runs: they are the words a returning page must still
        hear, not chatter to trim (web_chat.observer.EventLog keeps the same
        distinction for the stored events).
        """
        note = {key: value for key, value in event.items()}
        self._notes.append(note)
        if note.get("type") in _REPLAY_PINNED_TYPES:
            return
        spare = [kept for kept in self._notes if kept.get("type") not in _REPLAY_PINNED_TYPES]
        if len(spare) <= MAX_REPLAY_NOTES:
            return
        for index, kept in enumerate(self._notes):
            if kept is spare[0]:
                del self._notes[index]
                break

    # -- checkpoints -------------------------------------------------------
    async def _snapshot(self) -> Optional[dict]:
        """The chat's spaces now; None when they cannot be taken - the turn runs anyway."""
        space = self._session.space
        checkpoints = getattr(space, "checkpoints", None)
        if checkpoints is None:
            return None
        try:
            if hasattr(space, "snapshot"):
                return await asyncio.to_thread(space.snapshot, self._session.context_id)
            workspace = space.conversation_workspace(self._session.context_id)
            return await asyncio.to_thread(checkpoints.snapshot, workspace)
        except Exception:
            logger.warning("No checkpoint of %s before this turn", self._session.context_id, exc_info=True)
            return None

    async def _record_checkpoint(self) -> None:
        """Keep what the turn changed in the workspace, and say so: Restore goes
        back to the checkpoint taken before it."""
        before = self._checkpoint
        if before is None:
            return
        after = await self._snapshot()
        if after is None:
            return
        space = self._session.space
        compare = getattr(space, "checkpoint_changes", None) or space.checkpoints.changes
        changes = await asyncio.to_thread(compare, before, after)
        manager, context_id = self._session.manager, self._session.context_id
        kept = dict(manager.get_context_metadata(context_id).get(CHECKPOINTS_KEY) or {})
        kept[self.run_id] = {"before": before, "files": len(changes), "changes": changes[:MAX_LISTED_CHANGES]}
        if "spaces" in before:
            # Shared spaces restore only what this chat's turns changed: what each turn ended with.
            kept[self.run_id]["after"] = after
        # The newest turns only: an old checkpoint still restores, it is just not offered.
        kept = dict(list(kept.items())[-MAX_CHECKPOINTS:])
        manager.update_context_metadata(context_id, {CHECKPOINTS_KEY: kept})
        if changes:
            await self._emit({"type": "checkpoint", "turn_id": self.run_id, "files": len(changes), "changes": changes[:MAX_LISTED_CHANGES]})

    # -- persistence -------------------------------------------------------
    def _record_activity(self) -> None:
        """Count the turn for the system that ran it (web_chat.system_activity)."""
        space, resolution = self._session.space, self._resolution
        activity = getattr(space, "activity", None)
        if activity is None or resolution is None:
            return
        try:
            activity.record_turn(
                resolution.system,
                agent=resolution.agent,
                outcome=self.outcome or "error",
                duration_ms=self._recorder.elapsed_ms,
                user_id=space.user_id,
                policy_blocks=self._observer.policy_blocks,
            )
        except Exception:
            logger.exception("Recording the turn's activity failed")

    def _record_usage(self) -> None:
        """Add the turn's tokens to the user's day (web_chat.limits)."""
        record = getattr(self._session.space, "record_usage", None)
        if record is None:
            return
        try:
            record(self._recorder.tokens_in, self._recorder.tokens_out)
        except Exception:
            logger.exception("Recording the turn's token usage failed")

    def _persist_trace(self) -> None:
        # Onto this turn's own answer: a stopped turn's record is its
        # interruption message, and a sub-agent report may be appended after an
        # answer. The turn id finds the right message, never a neighbour's.
        # A request a UI button sent keeps its provenance on the user message:
        # visible in the thread, and the exact values the agent received.
        if self._ui_origin:
            self._session.manager.update_turn_message_metadata(
                self._session.context_id,
                self.run_id,
                {ui_state.ORIGIN_KEY: dict(self._ui_origin)},
                when=lambda last: last.role == "user",
            )
        ui_blocks = self._observer.ui_blocks()
        # What the model saw in its last call of the turn - the context the
        # answer was written at; history shows it beside the message.
        context_tokens = self._recorder.last_input_tokens
        kept = self._session.manager.update_turn_message_metadata(
            self._session.context_id,
            self.run_id,
            {
                # A step still in flight when the turn ends gets its interval
                # frozen here (web_chat.trace.TraceRecorder.snapshot), and the
                # turn's own duration is stored beside it: history must show the
                # same numbers the live "done" frame showed, not re-derive them.
                "trace": self._recorder.snapshot(final=True),
                "duration_ms": self._recorder.elapsed_ms,
                # The turn's public communication, durable with the message: a
                # reconnect (or a reload) replays the sentences and statuses
                # instead of deriving them again from the trace, and the same
                # event ids make a live frame and its stored twin one thing.
                "events": self._observer.turn_events(public_only=True),
                # The interfaces this turn presented, with the values they
                # hold (web_chat.ui_state): beside the answer, not in a tool
                # payload compaction may drop, so a reload shows them again.
                **({"ui": ui_blocks} if ui_blocks else {}),
                **({"context_tokens": context_tokens} if context_tokens > 0 else {}),
            },
            when=lambda last: last.role == "assistant" and not is_tool_result(last),
        )
        if not kept:
            # Loud, not silent: a turn with no message to keep its trace on is a
            # bug in the turn's own record, and history would lose its timings.
            logger.warning("No message of turn %s to keep its trace on", self.run_id)


class ChatSession:
    """Command loop for one chat websocket, in the space of the user it serves.

    ``authorize`` re-asks who the socket acts for, for a server with accounts:
    it returns the current user or raises
    :class:`web_chat.accounts.service.SessionRevoked`. It is called before
    every command and, while the socket is idle, every
    ``revalidate_interval`` seconds. Without it (a single-user server, or a
    transport without sessions) the socket acts for its space as before.
    """

    def __init__(
        self,
        space: Any,
        websocket: WebSocket,
        context_id: str,
        *,
        authorize: Optional[Callable[[], Awaitable[Any]]] = None,
        revalidate_interval: Optional[float] = None,
    ) -> None:
        self.space = space
        self._socket = websocket
        self.context_id = context_id
        self._authorize = authorize
        self._revalidate_interval = (
            SESSION_REVALIDATE_SECONDS if revalidate_interval is None else revalidate_interval
        )
        self.manager = space.context_manager()
        # A chat id that does not exist yet is a new chat: the client picks it
        # before the first message. An existing one - including a legacy one
        # loaded from disk - is left exactly as stored.
        self.manager.ensure_context(self.context_id)
        self.send_lock = asyncio.Lock()

    @property
    def _turns(self) -> Any:
        """The space's TurnBoard (web_chat.turns)."""
        return self.space.turns

    async def serve(self) -> None:
        await self._socket.accept()
        self.manager.ensure_context(self.context_id)
        watcher = None
        if self._authorize is not None:
            watcher = asyncio.create_task(self._watch_session(), name="chat-session-watch")
        try:
            while True:
                await self._dispatch(await self._socket.receive_text())
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
            # Leaving the page detaches; the turn keeps running for later viewers.
            active = self._turns.get(self.context_id)
            if active is not None:
                active[0].detach(self)

    # -- the session behind the socket -------------------------------------
    async def _revalidate(self) -> None:
        """Raise :class:`SessionRevoked` unless the socket may still act.

        The user is the one the session identifies now; it must also be the
        user whose space this socket serves. A session that somehow maps to
        another user is treated as ended, never as a new identity.
        """
        if self._authorize is None:
            return
        user = await self._authorize()
        owner = getattr(self.space, "user_id", None)
        if owner is not None and getattr(user, "id", None) != owner:
            raise SessionRevoked("The session no longer belongs to this space")

    async def _authorized(self) -> bool:
        try:
            await self._revalidate()
        except SessionRevoked:
            return False
        except Exception:  # noqa: BLE001 - an unknown answer is never permission to act
            logger.exception("Session revalidation failed; refusing the command")
            return False
        return True

    async def _watch_session(self) -> None:
        """Close the socket once its session ends, even when no command comes."""
        while True:
            await asyncio.sleep(self._revalidate_interval)
            try:
                await self._revalidate()
            except SessionRevoked:
                await self._refuse_revoked()
                return
            except Exception:  # noqa: BLE001 - no evidence of authorization means no access
                logger.exception("Session revalidation failed; closing the socket")
                await self._refuse_revoked()
                return

    async def _refuse_revoked(self) -> None:
        """Close the socket with 1008; its commands and approvals are refused."""
        logger.info("Closing chat socket for %s: its session ended", self.context_id)
        with suppress(RuntimeError, OSError):
            await self._socket.close(code=1008, reason="Session ended")

    async def send(self, event: dict[str, Any]) -> None:
        async with self.send_lock:
            await self._socket.send_json(event)

    async def send_unlocked(self, event: dict[str, Any]) -> None:
        """Send while the caller already holds :attr:`send_lock`."""
        await self._socket.send_json(event)

    def agent_label(self, system_key: str, agent_key: str) -> str:
        return self.space.registry.agent_label(system_key, agent_key)

    def release(self, turn: "AgentTurn") -> None:
        self._turns.release(self.context_id, turn)

    def hand_over(self, turn: "AgentTurn") -> None:
        """Give a finished turn's claim back and the conversation on, in one
        event-loop step (see :meth:`_succeed_queued`).
        """
        self._succeed_queued(turn)

    # -- commands ----------------------------------------------------------
    async def _dispatch(self, raw: str) -> None:
        if not await self._authorized():
            await self._refuse_revoked()
            return
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
        except (ValueError, TypeError):
            await self.send({"type": "error", "content": "Invalid message payload"})
            return

        if payload.get("action") == "stop":
            await self._stop(now=payload.get("now") is True)
            return
        if payload.get("action") == "attach":
            await self._attach()
            return
        if payload.get("action") == "policy_filter":
            await self._set_policy_filter(payload.get("filter"))
            return
        if payload.get("action") == "execution_target":
            # Where this conversation works: the server, or one of the user's
            # devices (core.factory.execution). A live switch: the running turn
            # and its tools follow it from the next call on.
            await self._set_execution_target(payload.get("target"), payload.get("workdir"))
            return
        if payload.get("action") == "policy_review":
            await self._answer_review(payload)
            return
        if getattr(self.space, "restart_pending", False):
            await self.send({"type": "error", "content": "Server restart in progress. Please send this message again when it is ready."})
            if not self._turns.is_claimed(self.context_id):
                await self.send({"type": "done"})
            return
        if payload.get("action") == "continue":
            await self._start(None, payload.get("system_key"), payload.get("agent_key"))
            return
        if payload.get("action") in ("unqueue", "send_queued"):
            await self._queued_action(payload.get("action"), payload.get("id"))
            return
        if payload.get("action") == "ui_action":
            # A button of a presented interface: a normal user turn, its text
            # composed by the server from the stored document (web_chat.ui_state).
            await self._ui_action(payload)
            return

        message = payload.get("message")
        text = message.strip() if isinstance(message, str) else ""
        attached = payload.get("images")
        audio_payload = payload.get("audio")
        if not text and not attached and not audio_payload:
            return
        if isinstance(payload.get("policy_filter"), str):
            # The switch as the user left it: the turn this message starts runs under it.
            self.space.set_policy_filter(self.context_id, payload["policy_filter"])
        if isinstance(payload.get("execution_target"), str):
            # The same for where it works: the choice travels with the message,
            # so the turn starts on the machine the user is looking at.
            self.space.set_execution_target(
                self.context_id, payload["execution_target"], payload.get("execution_workdir")
            )
        try:
            # Decoding and re-encoding is CPU work: off the loop, so Stop and
            # the other commands stay responsive.
            images = await asyncio.to_thread(normalize_images, attached, self._image_config())
        except AttachmentError as exc:
            await self.send({"type": "error", "content": error_text(exc)})
            await self.send({"type": "done"})
            return
        try:
            audio = await asyncio.to_thread(parse_audio_data_url, audio_payload) if audio_payload is not None else None
        except ValueError as exc:
            await self.send({"type": "error", "content": error_text(exc)})
            await self.send({"type": "done"})
            return
        if self._turns.is_claimed(self.context_id):
            if audio is not None:
                await self.send({"type": "busy", "content": "Audio cannot be added while this conversation is busy. Please retry after the current turn."})
                return
            # The agent is working: the message is delivered, not refused. A
            # decision can take seconds, so it runs beside the command loop.
            self._background(self._deliver_during_turn(text, images, payload.get("delivery")))
            return
        edit_of = payload.get("edit_of")
        if not (isinstance(edit_of, str) and 0 < len(edit_of) <= 64):
            edit_of = None
        await self._start(
            text, payload.get("system_key"), payload.get("agent_key"), images=images, audio=audio, edit_of=edit_of
        )

    async def _set_policy_filter(self, name: Any) -> None:
        """The user moved the policy switch: this conversation and its running turn follow."""
        ok = isinstance(name, str) and self.space.set_policy_filter(self.context_id, name)
        await self.send({"type": "policy_filter", "filter": name if ok else None, "ok": bool(ok)})

    async def _set_execution_target(self, target: Any, workdir: Any) -> None:
        """The user pointed this chat at a device, or brought it back to the cloud.

        The answer is the state the chat is in afterwards - the target, its
        folder and the devices it may switch to - so a refused switch leaves the
        client showing what is true, not what it asked for.
        """
        if target is None:
            # A client asking where this chat works: nothing changes.
            await self.send({"type": "execution_target", "ok": True, **self.space.execution_target(self.context_id)})
            return
        folder = workdir if isinstance(workdir, str) else ""
        ok = isinstance(target, str) and self.space.set_execution_target(self.context_id, target, folder)
        state = self.space.execution_target(self.context_id)
        await self.send({"type": "execution_target", "ok": bool(ok), **state})

    async def _answer_review(self, payload: dict[str, Any]) -> None:
        """The user allowed or declined a call held in this conversation."""
        approval_id = payload.get("approval_id")
        ok = isinstance(approval_id, str) and self.space.answer_action_review(
            self.context_id,
            approval_id,
            approve=payload.get("approve") is True,
            remember=payload.get("remember") is True,
        )
        await self.send({"type": "policy_review", "approval_id": approval_id, "ok": bool(ok)})

    async def _ui_action(self, payload: dict[str, Any]) -> None:
        """A button of a presented interface, as the user's next turn.

        Everything is re-checked here and nowhere else: the conversation this
        socket is already authorized for, the message and block it owns, the
        action the *stored* document allows, the values against that document's
        state schema and the revisions the client is looking at. The text the
        agent receives is composed by the server from that document - a
        template, tool name or prompt from the browser is never used. A repeated
        ``request_id`` repeats the acknowledgement and starts nothing.
        """
        # The ids travel back on every refusal, so the page can tell the
        # widget which of its buttons was refused and settle that attempt.
        where = {
            key: payload[key]
            for key in ("message_id", "ui_id")
            if isinstance(payload.get(key), str) and payload[key]
        }
        fields = {
            "message_id": payload.get("message_id"),
            "ui_id": payload.get("ui_id"),
            "action_id": payload.get("action_id"),
            "request_id": payload.get("request_id"),
        }
        for name, value in fields.items():
            if not isinstance(value, str) or not 0 < len(value) <= 64:
                await self._ui_refused("ui.schema", f"{name} is required", **where)
                return
        revisions = {}
        for name in ("document_revision", "state_revision"):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                await self._ui_refused("ui.schema", f"{name} must be a non-negative integer", **where)
                return
            revisions[name] = value
        if not isinstance(payload.get("values"), dict):
            await self._ui_refused("ui.schema", "values must be an object", **where)
            return
        known = ui_state.known_submit(self.manager, self.context_id, fields["request_id"])
        if known is not None:
            # A retry of an honoured press is still successful while its turn runs.
            await self.send({"type": "ui_submitted", "text": known.get("text", ""), "origin": known.get("origin"), "duplicate": True})
            return
        if self._turns.is_claimed(self.context_id):
            # A scoped refusal, not a bare `busy`: the page opened this socket
            # for one button press and must settle that attempt, while the
            # conversation's real running turn stays untouched.
            await self._ui_refused(
                "ui.busy", "This conversation already has an active turn.", **where
            )
            return
        try:
            _, block = ui_state.find_block(
                self.manager, self.context_id, fields["message_id"], fields["ui_id"]
            )
            document = block.get("document") or {}
            if revisions["document_revision"] != block.get("revision"):
                raise ui_state.UiStateError(
                    "ui.stale", "This interface has changed; reload it before using its buttons."
                )
            action_of(document, fields["action_id"])
            values = validate_values(document, payload.get("values"))
            saved = ui_state.save_values(
                self.manager,
                self.context_id,
                fields["message_id"],
                fields["ui_id"],
                {
                    "values": values,
                    "state_revision": revisions["state_revision"],
                    "request_id": f"submit:{fields['request_id']}",
                },
            )
        except UiDocumentError as exc:
            await self._ui_refused(exc.code, exc.message, **where)
            return
        except ui_state.UiStateError as exc:
            await self._ui_refused(exc.code, exc.message, **where, **exc.details)
            return
        text = ui_state.compose_submission(document, fields["action_id"], values)
        origin = ui_state.origin_of(
            fields["message_id"], fields["ui_id"], fields["action_id"], document, values, saved["state_revision"]
        )
        await self.send({"type": "ui_submitted", "text": text, "origin": origin})
        if not await self._start(text, None, None, ui_origin=origin):
            # The turn did not start (a refusal was already sent): nothing was
            # sent, and this socket's one button attempt must still settle.
            await self._ui_refused(
                "ui.refused",
                "The choice was not sent: the conversation could not start a turn.",
                **where,
            )
            return
        ui_state.claim_submit(
            self.manager, self.context_id, fields["request_id"], {"text": text, "origin": origin}
        )

    async def _ui_refused(self, code: str, message: str, **details: Any) -> None:
        """A refused UI request: a scoped, typed frame that also settles it.

        ``ui_action`` opens a socket for one button press; an ``error`` or
        ``busy`` frame alone would leave that page waiting for a ``done`` it
        will never get (the conversation itself is not this turn). The typed
        frame tells the page to close this attempt - and only this one.
        """
        await self.send({"type": "ui_refused", "code": code, "content": message, **details})

    def _background(self, coroutine: Any) -> None:
        """Run *coroutine* beside the command loop, owned by the space."""
        self._turns.spawn(coroutine)

    async def _deliver_during_turn(self, text: str, images: Sequence[Any], requested: Any) -> None:
        """Deliver a message sent while a turn runs: now, next step or after the turn."""
        active = self._turns.running(self.context_id)
        if active is None:
            # The turn ended meanwhile: it is an ordinary message now.
            await self._start(text, None, None, images=images)
            return
        turn, task = active
        queue = MessageQueue(self.manager, self.context_id)
        if requested in DELIVERIES:
            delivery, decided_by = requested, "user"
        else:
            delivery, decided_by = await decide_delivery(
                self.space.deployment,
                message=text,
                task=turn.message,
                steps=turn.recent_steps(),
                agent_text=turn.answer_tail,
            )
        # The queue is stored: it takes the normalized copies, while the files
        # themselves are kept now, in the chat's workspace, and named in the
        # text the agent reads when the message is delivered or started later.
        try:
            text, images = await self._keep_images(text, images)
        except AttachmentError as exc:
            await self.send({"type": "error", "content": error_text(exc)})
            return
        try:
            item = queue.add(
                text,
                images,
                delivery,
                state="steering" if delivery == "next_step" else "queued",
                front=delivery == "now",
            )
        except ValueError as exc:
            await self.send({"type": "error", "content": error_text(exc)})
            return
        await turn.announce(
            {"type": "delivery", "id": item["id"], "delivery": delivery, "decided_by": decided_by}, also_to=self
        )
        if delivery == "next_step" and not await turn.steer(item, queue):
            queue.requeue(item["id"])
        await turn.announce({"type": "queue", "items": queue.public()}, also_to=self)
        if delivery == "now":
            # The queue starts the message once the turn has stopped.
            await self._cancel(task)

    async def _queued_action(self, action: Any, item_id: Any) -> None:
        """``unqueue`` drops a waiting message; ``send_queued`` sends it now."""
        queue = MessageQueue(self.manager, self.context_id)
        item = queue.get(item_id) if isinstance(item_id, str) else None
        if item is None:
            await self.send({"type": "queue", "items": queue.public()})
            return
        active = self._turns.running(self.context_id)
        if action == "send_queued" and active is None:
            await self._start_queued(queue, item)
            return
        if item.get("state") == "steering" and active is not None and not active[0].withdraw(item["id"]):
            # The agent is reading it right now: it is delivered, not dropped,
            # and leaves the queue as delivered.
            await active[0].announce({"type": "queue", "items": queue.public()}, also_to=self)
            return
        queue.remove(item["id"])
        if action == "send_queued":
            # Sent now while the agent works: first in line, and the turn stops.
            queue.add(item["text"], item.get("images") or [], "now", front=True)
        snapshot = {"type": "queue", "items": queue.public()}
        if active is None:
            await self.send(snapshot)
            return
        await active[0].announce(snapshot)
        if action == "send_queued":
            if not active[0]._admitted.is_set():
                # Not admitted yet: its task is still waiting at run()'s gate,
                # and cancelling it there ends the turn with no "done" of its
                # own. Its preamble sees the stop, sends its refusal, and the
                # message stays first in line to start at once (see
                # _run_turn_preamble and _advance_queue).
                active[0].user_stopped = True
                await self._cancel(active[1])
                return
            await self._cancel(active[1])

    async def _advance_queue(self, finished: AgentTurn) -> None:
        """After a turn, send the next waiting message - unless the user should decide.

        The queue moves on when the turn answered, or when it was stopped for a
        ``now`` message. After a Stop, a timeout or a failure the messages wait.
        """
        if getattr(self.space, "restart_pending", False) or self._turns.is_claimed(self.context_id):
            return
        queue = MessageQueue(self.manager, self.context_id)
        item = queue.next_queued()
        if item is None:
            return
        if finished.outcome != "answered" and item.get("delivery") != "now":
            return
        await self._start_queued(queue, item)

    def _succeed_queued(self, finished: AgentTurn) -> None:
        """Release a finished turn's claim, passing it to the next queued message.

        Called from the turn's own finally (:meth:`hand_over`), with no await
        in between: whichever message starts next is claimed before the
        finished one lets go, so a GET - a resync, a reload, another tab -
        never lands between two turns and sees no active turn while a message
        waits in the queue. The successor's admission (its revision check,
        recovery, the quota) runs beside the command loop; refused, the
        message waits at the front of the queue again instead of being lost.
        """
        # The finished turn's claim goes first: the claim check below is then
        # about another turn, never this one's own. All of this runs in one
        # event-loop step - nothing can run in between - so a GET still sees
        # an active turn: the finished one, then its successor (see
        # _run_turn_preamble and _succeed_queued's caller).
        self._turns.release(self.context_id, finished)
        item = None
        queue = None
        if not getattr(self.space, "restart_pending", False) and not self._turns.is_claimed(self.context_id):
            queue = MessageQueue(self.manager, self.context_id)
            item = queue.next_queued()
            if item is not None and finished.outcome != "answered" and item.get("delivery") != "now":
                # A stop, a timeout, a failure: only a message asked to go
                # *now* still starts; the rest wait for the user again, in
                # place - their id and their position are what the page
                # already shows (see _advance_queue).
                item = None
        if item is None:
            return
        # Taken out here, in the same step: between the turns the queue is
        # never ahead of what starts.
        queue.remove(item["id"])
        successor = AgentTurn(
            self,
            item["text"],
            system_key=None,
            agent_key=None,
            images=tuple(item.get("images") or []),
            images_saved=True,
        )
        # Claimed and registered before the successor's preamble runs, so its
        # message is the conversation's active turn at once (see _start).
        self._turns.claim(self.context_id, successor)
        # The successor's task starts here, in the same event-loop step the
        # finished turn's claim goes: the two turns hand over with no moment
        # in between where a GET would see no active turn - registered at
        # once, admitted by its preamble beside the command loop.
        task = asyncio.create_task(successor.run())
        self._turns.register(self.context_id, successor, task)
        task.add_done_callback(self._turn_ended(successor))
        self._background(self._admit_queued_turn(successor, queue, item, task))

    async def _admit_queued_turn(
        self, turn: AgentTurn, queue: MessageQueue, item: dict[str, Any], task: asyncio.Task
    ) -> None:
        """Run a queued message's preamble, its turn already claimed for it.

        Refused - a limit, an incompatible revision, a Stop in between - the
        message goes back to the front of the queue instead of being lost.
        """
        if not await self._run_turn_preamble(turn, None, restart_resume=False, task=task):
            queue.add(
                item["text"],
                item.get("images") or [],
                item.get("delivery") or "after_turn",
                front=True,
            )
        await self.send({"type": "queue", "items": queue.public()})

    async def _start_queued(self, queue: MessageQueue, item: dict[str, Any]) -> None:
        """Send a waiting message as the next turn. Refused - a limit, say - it
        goes back to the front of the queue instead of being lost.

        Its files were kept when it arrived, and its text already names them;
        only their normalized copies wait in the queue for the model."""
        queue.remove(item["id"])
        images = item.get("images") or []
        if not await self._start(item["text"], None, None, images=images, images_saved=True):
            queue.add(item["text"], images, item.get("delivery") or "after_turn", front=True)
        await self.send({"type": "queue", "items": queue.public()})

    def _image_config(self) -> Any:
        """settings.image_processing of the space's config, or None for the defaults."""
        config = getattr(self.space, "config", None)
        settings = getattr(getattr(config, "config", None), "settings", None)
        return getattr(settings, "image_processing", None)

    def workspace_for_files(self) -> Optional[Any]:
        """The directory this chat's files live in; None when the space has none.

        A chat may work in a directory of its own (web_chat.space): its files
        go there, and the download URL names the chat so the route opens them
        in it. A space that cannot name a directory has nowhere to keep them,
        and says so rather than dropping the files.
        """
        space, context_id = self.space, self.context_id
        file_spaces = getattr(space, "file_spaces", None)
        if callable(file_spaces):
            found = file_spaces(context_id)
            return found[0] if isinstance(found, tuple) else found
        conversation_workspace = getattr(space, "conversation_workspace", None)
        if callable(conversation_workspace):
            return conversation_workspace(context_id)
        return None

    async def _keep_images(self, text: str, images: Sequence[Any]) -> tuple[str, list[str]]:
        """Keep *images* in the chat's workspace; the text that names them.

        Returns *text* with the chat's ``Attached files:`` block naming every
        kept file (its URL and its workspace path) and the normalized
        ``data:`` URLs the model reads. A chat with nowhere to keep them, or a
        write that fails, raises :class:`AttachmentError`: the turn reports it
        to the user instead of sending a message whose picture was lost.
        Blocking file work, so the caller runs this off the event loop when it
        is not already awaited there.
        """
        if not images:
            return text, []
        workspace = self.workspace_for_files()
        if workspace is None:
            raise AttachmentError(
                "The image could not be kept: this chat has no workspace to save it in."
            )
        from web_chat.uploads import UploadError

        try:
            saved = await asyncio.to_thread(save_images, list(images), workspace, self.context_id)
        except (UploadError, OSError) as exc:
            logger.warning("Could not store an image in the workspace: %s", exc)
            raise AttachmentError(f"The image could not be saved in the chat's workspace: {exc}") from None
        return with_attachments(text, saved), jpeg_urls(images)

    async def _attach(self) -> None:
        active = self._turns.get(self.context_id)
        if active is None:
            # The turn ended between the page loading and attaching; the stored
            # conversation already holds its result.
            await self.send({"type": "done", "detached": True})
            return
        await active[0].attach(self)

    async def _stop(self, *, now: bool = False) -> None:
        logger.info("Stop requested from the chat for %s (now=%s)", self.context_id, now)
        active = self._turns.running(self.context_id)
        if active is None:
            await self.send({"type": "done", "stopped": True})
            return
        turn, task = active
        if turn.restart_paused and not turn.user_stopped and not now:
            turn.user_stopped = True
            await turn.announce({"type": "stopping"})
            return
        turn.user_stopped = True
        if not turn._admitted.is_set():
            # Not admitted yet: its task is still waiting at run()'s gate, and
            # cancelling it there ends the turn with no "done" of its own. The
            # preamble sees the stop, sends its refusal to its own socket and
            # lets the claim go (see _run_turn_preamble); this page is told here.
            await self.send({"type": "done", "stopped": True})
            await self._cancel(task)
            return
        if not now and turn.request_stop():
            await turn.announce({"type": "stopping"})
            return
        await self._cancel(task)

    async def _start(
        self,
        message: Optional[str],
        system_key: Any,
        agent_key: Any,
        *,
        images: Sequence[Any] = (),
        images_saved: bool = False,
        audio: Optional[AudioAttachment] = None,
        edit_of: Optional[str] = None,
        restart_resume: bool = False,
        ui_origin: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Start a turn; ``message`` None continues the interrupted one.
        Whether it started; a refusal was sent to the socket.

        The turn is claimed and registered the moment its message is
        accepted - before the revision check, recovery and admission, which
        can each take a while - so a GET from any tab in that window still
        sees the conversation's active turn with the user's message in it,
        and a message sent by another socket in it is queued rather than
        racing a second turn. A refusal unregisters and releases (see
        _run_turn_preamble), so the conversation is never left busy by a
        turn that never ran.

        ``edit_of`` marks the message as a new version of an edited one, sent
        into the branch made for it (POST .../branches). ``images_saved``: the
        message's files are already in the workspace, named in ``message``."""
        if getattr(self.space, "restart_pending", False):
            await self.send({"type": "error", "content": "Server restart in progress. Please try again shortly."})
            await self.send({"type": "done"})
            return False
        if self._turns.is_claimed(self.context_id):
            await self.send({"type": "busy", "content": "This conversation already has an active turn."})
            return False
        if not self.space.registry.selection_is_valid(system_key or None, agent_key or None):
            await self.send({"type": "error", "content": "Unknown system or agent"})
            await self.send({"type": "done"})
            return False
        turn = AgentTurn(
            self,
            message,
            system_key=system_key or None,
            agent_key=agent_key or None,
            images=tuple(images),
            images_saved=images_saved,
            audio=audio,
            edit_of=edit_of,
            ui_origin=ui_origin,
        )
        if not self._turns.claim(self.context_id, turn):
            # A claim raced this one in the window above: one turn per
            # conversation, and the other one was first.
            await self.send({"type": "busy", "content": "This conversation already has an active turn."})
            return False
        # Registered, its task started and waiting at run()'s admission gate
        # until the preamble admits it: the conversation's active turn is
        # visible from the moment its message is accepted (see
        # _run_turn_preamble), and a refusal - or a Stop while it prepares -
        # releases it again.
        return await self._run_turn_preamble(turn, system_key, restart_resume=restart_resume)

    async def _run_turn_preamble(
        self,
        turn: "AgentTurn",
        system_key: Any,
        *,
        restart_resume: bool,
        task: Optional[asyncio.Task] = None,
    ) -> bool:
        """Admit an accepted turn - its revision, recovery and quota - and run it.

        The turn's task starts at once and waits in run() at its admission
        gate, registered beside it, so the conversation shows its active turn
        - and a Stop or a queued message reaches it - from the moment the
        message is accepted. Every refusal here, and a Stop that arrives while
        the preamble runs, lets the waiting task go and releases the record
        and the claim, so the conversation is never left busy by a turn that
        never ran.
        """
        if task is None:
            # Created and registered here, in the same event-loop step the
            # message is accepted: no window a GET could land in. A caller
            # that claims a successor turn (see _succeed_queued) passes the
            # task it created for the same reason.
            task = asyncio.create_task(turn.run())
            self._turns.register(self.context_id, turn, task)
            task.add_done_callback(self._turn_ended(turn))
        admitted = False
        try:
            # A conversation this runtime cannot continue is refused before routing,
            # warm-up, the daily-turn quota and any metadata write: the client gets
            # the machine code, not a generic failure after work has begun. One that
            # only drifted re-pins and continues, with a notice instead.
            try:
                notices = await self._check_revision(system_key)
            except revisions.ContextCompatibilityError as exc:
                await self.send(revision_error_event(exc))
                await self.send({"type": "done"})
                return False
            if turn.user_stopped:
                # A Stop reached it while it prepared: it never runs.
                await self.send({"type": "done", "stopped": True})
                return False
            # Recovery mutates stored execution state, so it must follow admission.
            self.manager.recover_abandoned_turn(self.context_id)
            # ``resumes`` - not ``message``, a property that is never None:
            # the request is a Continue (see AgentTurn.message).
            if turn.resumes and self.manager.pending_interruption(self.context_id) is None:
                await self.send({"type": "error", "content": "There is nothing to continue in this conversation."})
                await self.send({"type": "done"})
                return False
            # Admitting counts the turn for the day: only a request that will run.
            # Continuing an already admitted turn must not charge a second daily turn.
            refusal = None if restart_resume and turn.resumes else self.space.admit_turn(turn)
            if refusal is not None:
                await self.send({"type": "error", "content": refusal})
                await self.send({"type": "done"})
                return False
            if turn.user_stopped:
                await self.send({"type": "done", "stopped": True})
                return False
            # The conversation continues on a revision this runtime re-pinned it to
            # (a Grid version, a different system configuration, or a conversation
            # stored before pinning existed). Tell the client before the turn runs,
            # with a typed event that is not an error: nothing failed.
            for notice in notices:
                await self.send(revision_notice_event(notice))
            if turn.user_stopped:
                await self.send({"type": "done", "stopped": True})
                return False
            admitted = True
            return True
        finally:
            if admitted:
                # The gate opens: the turn's waiting task takes over here.
                turn._admitted.set()
            else:
                # The turn never ran. Let its waiting task go at the gate (it
                # ends without a frame of its own: the refusal went to the
                # socket above), and give the claim and the record back - a
                # started turn hands its claim over in run()'s own finally.
                self._turns.forget(self.context_id, turn)
                self._turns.release(self.context_id, turn)
                turn._refused = True
                turn._admitted.set()
                if turn.user_stopped:
                    # What waits in the queue to go *now* still starts, as it
                    # would after a running turn was stopped (see _advance_queue).
                    self._background(self._advance_queue(turn))

    def _turn_ended(self, turn: "AgentTurn") -> Callable[[asyncio.Task], None]:
        """What runs when a started turn's task ends: forget its record.

        run() hands the claim over in its own finally, in the same event-loop
        step as its last frame, so nothing starts here. A task cancelled
        before its first step never reaches that finally: this callback is
        the turn's last chance to let the claim go, and the chat must not
        stay busy for good.
        """

        def ended(_task: asyncio.Task) -> None:
            self._turns.forget(self.context_id, turn)
            self._turns.release(self.context_id, turn)

        return ended

    async def _check_revision(self, system_key: Any) -> list[dict[str, Any]]:
        """Judge a conversation that cannot run here, before any work.

        The checks that need no configuration (unknown conversation, a manifest
        of another conversation, one this runtime cannot read) run first. Then
        the system the conversation last ran on - the selection the client sent,
        else what the stored metadata records, else the default system - is
        resolved and graded. A real incompatibility is refused with a machine
        code. A Grid version, runtime or resolved system configuration that
        moved on, and a conversation stored before pinning existed, are
        admitted: the turn's own gate re-pins it, and the client is told with a
        typed notice (not an error). An explicit selection of another system is
        still refused here rather than silently re-pinned.

        Returns the notices to send; raises ContextCompatibilityError on a
        refusal. Nothing is written here.
        """
        state = revisions.context_state(self.manager, self.context_id)
        if state["status"] in ("new", "ephemeral"):
            return []
        metadata = self.manager.get_context_metadata(self.context_id)
        requested = system_key if isinstance(system_key, str) and system_key else None
        pinned_system = requested or metadata.get("routed_system") or metadata.get("system_key")
        registry = self.space.registry_of(self.context_id)
        config_of = getattr(registry, "config", None)
        if not callable(config_of):
            # A transport without configs (tests, a future backend): the turn's
            # own gate still judges before any model call.
            return []
        config = config_of(pinned_system or registry.default_key())
        if getattr(config, "config", None) is None:
            return []
        decision = revisions.assess(self.manager, self.context_id, config)
        return [decision["notice"]] if decision.get("notice") else []

    @staticmethod
    async def _cancel(task: asyncio.Task) -> None:
        if task.done():
            return
        # A message and a Stop can arrive in one burst: the turn's task exists
        # but has not taken its first step, and a cancel now would skip run()
        # altogether - no interruption recorded, no "done" sent. One pass of
        # the loop lets it start, so the cancel lands inside run().
        await asyncio.sleep(0)
        if task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def chat_session(
    space: Any,
    websocket: WebSocket,
    context_id: str,
    *,
    authorize: Optional[Callable[[], Awaitable[Any]]] = None,
    revalidate_interval: Optional[float] = None,
) -> None:
    """Serve one chat socket; *authorize* revalidates its session (see :class:`ChatSession`)."""
    await ChatSession(
        space, websocket, context_id, authorize=authorize, revalidate_interval=revalidate_interval
    ).serve()
