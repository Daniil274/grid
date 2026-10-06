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
import time
import uuid
from contextlib import suppress
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

from core.interruption import INTERRUPTED_TYPE, is_resumable
from core.pricing import format_usd
from core.steering import SteerMessage
from core.tool_check import summarize
from web_chat.attachments import AttachmentError, agent_message, normalize_images
from web_chat.delivery import DELIVERIES, MessageQueue, decide_delivery
from web_chat.observer import WebStreamObserver
from web_chat.spend import TurnSpend, tracking
from web_chat.systems import Resolution
from web_chat.trace import StepKind, TraceRecorder, is_tool_result

logger = logging.getLogger(__name__)

#: Internal sentinel marking the end of a turn's event stream.
_FINISHED = "_finished"


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
        edit_of: Optional[str] = None,
    ) -> None:
        """``message`` None is Continue: the turn resumes the conversation's
        interrupted turn with the agent that ran it. ``images`` are normalized
        ``data:`` URLs (web_chat.attachments) sent with the message."""
        self._session = session
        self._message = message
        self._images = tuple(images)
        self._edit_of = edit_of
        self._requested = (system_key, agent_key)
        self._factory: Any = None  # set once the turn knows where it runs
        self._resolution: Optional[Resolution] = None
        self._stop_requested = False
        self.restart_paused = False
        self.user_stopped = False
        #: How the turn ended: "answered", "interrupted", "error" or "stopped".
        self.outcome: Optional[str] = None
        self._answer_tail = ""  # the agent's latest words, for delivery decisions
        self.run_id = uuid.uuid4().hex
        self._started = time.monotonic()
        self._log: list[dict[str, Any]] = []
        self._subscribers: set["ChatSession"] = {session}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._recorder = TraceRecorder(self._queue.put_nowait, on_usage=self._check_token_budget)
        #: Whether the turn was already stopped for passing its token budget.
        self._budget_hit = False
        #: What this turn's model calls cost (web_chat.spend), and whether it was
        #: already stopped for passing its dollar budget.
        self._spend = TurnSpend(self._check_usd_budget)
        self._usd_budget_hit = False
        self._observer = WebStreamObserver(
            self._recorder,
            emit_token=self._token,
            reset_answer=lambda: self._queue.put_nowait({"type": "answer_reset"}),
            emit_image=lambda url: self._queue.put_nowait({"type": "image", "url": url}),
        )

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

    def steer(self, item: dict[str, Any], queue: "MessageQueue") -> bool:
        """Hand a queued message to the running agent for its next step.

        Delivered, it leaves the queue and shows in the chat; if the turn ends
        first it waits in the queue for the next turn. False when the agent
        has not started or cannot take messages mid-run.
        """
        if self._factory is None or not hasattr(self._factory, "steer"):
            return False

        def delivered(message: SteerMessage) -> None:
            queue.remove(message.message_id)
            self._queue.put_nowait({"type": "steered", "id": message.message_id, "text": message.text, "images": message.images})
            self._queue.put_nowait({"type": "queue", "items": queue.public()})

        def undelivered(message: SteerMessage) -> None:
            queue.requeue(message.message_id)

        return self._factory.steer(
            self._session.context_id,
            SteerMessage(item["id"], item["text"], list(item.get("images") or []), delivered, undelivered),
        )

    def withdraw(self, item_id: str) -> bool:
        """Take back a message handed by :meth:`steer` that the agent has not read."""
        if self._factory is None or not hasattr(self._factory, "withdraw_steer"):
            return False
        return self._factory.withdraw_steer(self._session.context_id, item_id)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    async def attach(self, session: "ChatSession") -> None:
        """Replay the turn so far to *session*, then stream to it live.

        Snapshotting the log and subscribing happen with no await in between,
        so every event lands exactly once: in the replay or in the live stream.
        The session's send lock keeps live events queued behind the replay.
        """
        async with session.send_lock:
            replay = list(self._log)
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
            # Stopped, timed out or failed: say so with what Continue needs.
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                if await self._announce_interruption() and self.outcome != "stopped":
                    self.outcome = "interrupted"
            self._record_activity()
            self._record_usage()
            # Release before "done": the client may submit its next turn at once.
            self._session.release()
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                await self._emit({
                    "type": "done",
                    "stopped": stopped,
                    "duration_ms": self._recorder.elapsed_ms,
                    "tokens_in": self._recorder.tokens_in,
                    "tokens_out": self._recorder.tokens_out,
                })

    # -- producer ----------------------------------------------------------
    async def _produce(self) -> None:
        space = self._session.space
        try:
            resolution = self._resolution = await self._resolve()
            label = self._session.agent_label(resolution.system, resolution.agent)
            self._observer.agent_label = label
            self._warn_about_tools(resolution, label)

            step = self._recorder.open(
                StepKind.PREPARE,
                "Preparing the runtime",
                subtitle=f"{resolution.system} · {resolution.agent}",
                body=f"Workspace: {space.workspace_label}",
            )
            await space.warm_agent(resolution.agent, resolution.system)
            self._recorder.close(step, title=f"Runtime ready · {label}")

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
                result = await resolution.factory.run_agent(
                    agent_key=resolution.agent,
                    message=agent_message(self._message, list(self._images)),
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
            self._recorder.fail(str(exc))
            self._queue.put_nowait({"type": "error", "content": str(exc)})
        finally:
            drop = getattr(self._observer, "drop_unwritten_calls", None)
            if drop is not None:
                drop()
            self._queue.put_nowait({"type": _FINISHED})

    def _warn_about_tools(self, resolution: Resolution, label: str) -> None:
        """Say which tools will fail before the agent starts; nothing is disabled."""
        try:
            issues = self._session.space.registry.agent_issues(resolution.system, resolution.agent)
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
        return text or ("Image" if self._images else None)

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
        while True:
            event = await self._queue.get()
            if event["type"] == _FINISHED:
                return
            await self._emit(event)

    async def _emit(self, event: dict[str, Any]) -> None:
        frame = {**event, "run_id": self.run_id}
        self._log.append(frame)
        for session in list(self._subscribers):
            try:
                await session.send(frame)
            except (WebSocketDisconnect, RuntimeError, OSError):
                # A closed tab must not stall the agent or the other viewers.
                self._subscribers.discard(session)

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
        # Only onto this turn's answer: a stopped turn stored none, and the other
        # assistant entries are sub-agent reports, which are not answers.
        self._session.manager.update_last_message_metadata(
            self._session.context_id,
            {"trace": self._recorder.snapshot()},
            when=lambda last: last.role == "assistant" and not is_tool_result(last),
        )


class ChatSession:
    """Command loop for one chat websocket, in the space of the user it serves."""

    def __init__(self, space: Any, websocket: WebSocket, context_id: str) -> None:
        self.space = space
        self._socket = websocket
        self.context_id = context_id
        self.manager = space.context_manager()
        self.send_lock = asyncio.Lock()

    @property
    def _turns(self) -> Any:
        """The space's TurnBoard (web_chat.turns)."""
        return self.space.turns

    async def serve(self) -> None:
        await self._socket.accept()
        self.manager.ensure_context(self.context_id)
        try:
            while True:
                await self._dispatch(await self._socket.receive_text())
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            # Leaving the page detaches; the turn keeps running for later viewers.
            active = self._turns.get(self.context_id)
            if active is not None:
                active[0].detach(self)

    async def send(self, event: dict[str, Any]) -> None:
        async with self.send_lock:
            await self._socket.send_json(event)

    async def send_unlocked(self, event: dict[str, Any]) -> None:
        """Send while the caller already holds :attr:`send_lock`."""
        await self._socket.send_json(event)

    def agent_label(self, system_key: str, agent_key: str) -> str:
        return self.space.registry.agent_label(system_key, agent_key)

    def release(self) -> None:
        self._turns.release(self.context_id)

    # -- commands ----------------------------------------------------------
    async def _dispatch(self, raw: str) -> None:
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

        message = payload.get("message")
        text = message.strip() if isinstance(message, str) else ""
        attached = payload.get("images")
        if not text and not attached:
            return
        if isinstance(payload.get("policy_filter"), str):
            # The switch as the user left it: the turn this message starts runs under it.
            self.space.set_policy_filter(self.context_id, payload["policy_filter"])
        try:
            # Decoding and re-encoding is CPU work: off the loop, so Stop and
            # the other commands stay responsive.
            images = await asyncio.to_thread(normalize_images, attached, self._image_config())
        except AttachmentError as exc:
            await self.send({"type": "error", "content": str(exc)})
            await self.send({"type": "done"})
            return
        if self._turns.is_claimed(self.context_id):
            # The agent is working: the message is delivered, not refused. A
            # decision can take seconds, so it runs beside the command loop.
            self._background(self._deliver_during_turn(text, images, payload.get("delivery")))
            return
        edit_of = payload.get("edit_of")
        if not (isinstance(edit_of, str) and 0 < len(edit_of) <= 64):
            edit_of = None
        await self._start(
            text, payload.get("system_key"), payload.get("agent_key"), images=images, edit_of=edit_of
        )

    async def _set_policy_filter(self, name: Any) -> None:
        """The user moved the policy switch: this conversation and its running turn follow."""
        ok = isinstance(name, str) and self.space.set_policy_filter(self.context_id, name)
        await self.send({"type": "policy_filter", "filter": name if ok else None, "ok": bool(ok)})

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

    def _background(self, coroutine: Any) -> None:
        """Run *coroutine* beside the command loop, owned by the space."""
        self._turns.spawn(coroutine)

    async def _deliver_during_turn(self, text: str, images: list[str], requested: Any) -> None:
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
        try:
            item = queue.add(
                text,
                images,
                delivery,
                state="steering" if delivery == "next_step" else "queued",
                front=delivery == "now",
            )
        except ValueError as exc:
            await self.send({"type": "error", "content": str(exc)})
            return
        await turn.announce(
            {"type": "delivery", "id": item["id"], "delivery": delivery, "decided_by": decided_by}, also_to=self
        )
        if delivery == "next_step" and not turn.steer(item, queue):
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

    async def _start_queued(self, queue: MessageQueue, item: dict[str, Any]) -> None:
        """Send a waiting message as the next turn. Refused - a limit, say - it
        goes back to the front of the queue instead of being lost."""
        queue.remove(item["id"])
        images = item.get("images") or []
        if not await self._start(item["text"], None, None, images=images):
            queue.add(item["text"], images, item.get("delivery") or "after_turn", front=True)
        await self.send({"type": "queue", "items": queue.public()})

    def _image_config(self) -> Any:
        """settings.image_processing of the space's config, or None for the defaults."""
        config = getattr(self.space, "config", None)
        settings = getattr(getattr(config, "config", None), "settings", None)
        return getattr(settings, "image_processing", None)

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
        images: list[str] | tuple[str, ...] = (),
        edit_of: Optional[str] = None,
        restart_resume: bool = False,
    ) -> bool:
        """Start a turn; ``message`` None continues the interrupted one.
        Whether it started; a refusal was sent to the socket.

        ``edit_of`` marks the message as a new version of an edited one, sent
        into the branch made for it (POST .../branches)."""
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
        # No turn of this conversation runs here, so one that the record says
        # is running died with an earlier process: record it before routing.
        self.manager.recover_abandoned_turn(self.context_id)
        if message is None and self.manager.pending_interruption(self.context_id) is None:
            await self.send({"type": "error", "content": "There is nothing to continue in this conversation."})
            await self.send({"type": "done"})
            return False
        # Admitting counts the turn for the day: only a request that will run.
        # Continuing an already admitted turn must not charge a second daily turn.
        refusal = None if restart_resume and message is None else self.space.admit_turn()
        if refusal is not None:
            await self.send({"type": "error", "content": refusal})
            await self.send({"type": "done"})
            return False
        self._turns.claim(self.context_id)
        turn = AgentTurn(
            self,
            message,
            system_key=system_key or None,
            agent_key=agent_key or None,
            images=tuple(images),
            edit_of=edit_of,
        )
        task = asyncio.create_task(turn.run())
        self._turns.register(self.context_id, turn, task)

        def ended(_task: asyncio.Task) -> None:
            self._turns.forget(self.context_id, turn)
            self._background(self._advance_queue(turn))

        task.add_done_callback(ended)
        return True

    @staticmethod
    async def _cancel(task: asyncio.Task) -> None:
        if task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def chat_session(space: Any, websocket: WebSocket, context_id: str) -> None:
    await ChatSession(space, websocket, context_id).serve()
