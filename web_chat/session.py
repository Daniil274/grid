"""Chat websocket transport: one turn at a time per conversation, always responsive.

A turn belongs to the server, not to the socket that started it. Closing or
reloading the page only detaches that socket; the agent keeps working, and a
socket that attaches later receives the turn's events so far and then follows
it live. Only an explicit ``stop`` cancels a turn.

Commands (``stop``, the next message) are read on a loop that never awaits
inference, so a running turn can always be cancelled. Inference itself runs in a
producer task that pushes trace steps and answer tokens into a queue; this
module only forwards them and, when the turn ends, pins the recorded trace onto
the stored assistant message so a reloaded conversation shows the same timeline.

A turn begins by resolving where it runs: the client may pin a system, an agent,
both, or neither, and anything left open is routed per message.
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

from core.context import safe_lock
from web_chat.observer import WebStreamObserver
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
        message: str,
        *,
        system_key: Optional[str],
        agent_key: Optional[str],
    ) -> None:
        self._session = session
        self._message = message
        self._requested = (system_key, agent_key)
        self.run_id = uuid.uuid4().hex
        self._started = time.monotonic()
        self._log: list[dict[str, Any]] = []
        self._subscribers: set["ChatSession"] = {session}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._recorder = TraceRecorder(self._queue.put_nowait)
        self._observer = WebStreamObserver(
            self._recorder,
            emit_token=lambda text: self._queue.put_nowait({"type": "token", "content": text}),
            reset_answer=lambda: self._queue.put_nowait({"type": "answer_reset"}),
        )

    @property
    def message(self) -> str:
        return self._message

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
                "message": self._message,
                "elapsed_ms": self.elapsed_ms,
                "run_id": self.run_id,
            }
            for event in (header, *replay):
                await session.send_unlocked(event)

    def detach(self, session: "ChatSession") -> None:
        self._subscribers.discard(session)

    async def run(self) -> None:
        producer = asyncio.create_task(self._produce())
        stopped = False
        try:
            await self._consume()
        except asyncio.CancelledError:
            stopped = True
        finally:
            if not producer.done():
                producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
            self._persist_trace()
            # Release before "done": the client may submit its next turn at once.
            self._session.release()
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                await self._emit({
                    "type": "done",
                    "stopped": stopped,
                    "duration_ms": self._recorder.elapsed_ms,
                })

    # -- producer ----------------------------------------------------------
    async def _produce(self) -> None:
        runtime = self._session.runtime
        try:
            resolution = await self._resolve()
            label = self._session.agent_label(resolution.system, resolution.agent)
            self._observer.agent_label = label

            step = self._recorder.open(
                StepKind.PREPARE,
                "Preparing the runtime",
                subtitle=f"{resolution.system} · {resolution.agent}",
                body=f"Workspace: {runtime.workspace_path}",
            )
            await runtime.warm_agent(resolution.agent, resolution.system)
            self._recorder.close(step, title=f"Runtime ready · {label}")

            runtime.update_conversation_metadata(
                self._session.context_id,
                created_by_web=True,
                system_key=self._requested[0],
                agent_key=self._requested[1],
                routed_system=resolution.system,
                routed_agent=resolution.agent,
                title=" ".join(self._message.split())[:42],
            )
            result = await resolution.factory.run_agent(
                agent_key=resolution.agent,
                message=self._message,
                context_id=self._session.context_id,
                stream=True,
                user_id=runtime.user_id,
                stream_observer=self._observer,
            )
            self._recorder.end_all_reasoning()
            if result:
                self._queue.put_nowait({"type": "final_output", "content": str(result)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Web chat run failed")
            self._recorder.fail(str(exc))
            self._queue.put_nowait({"type": "error", "content": str(exc)})
        finally:
            self._queue.put_nowait({"type": _FINISHED})

    async def _resolve(self) -> Resolution:
        """Decide the system and agent, showing the routing as its own step."""
        system_key, agent_key = self._requested
        routing_step = None
        if system_key is None or agent_key is None:
            routing_step = self._recorder.open(
                StepKind.ROUTING,
                "Choosing where to run",
                subtitle="No system pinned" if system_key is None else f"System: {system_key}",
            )

        resolution = await self._session.runtime.resolve_turn(
            self._message,
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
    def _persist_trace(self) -> None:
        manager = self._session.manager
        with safe_lock(manager._lock):
            bucket = manager._contexts.get(self._session.context_id)
            conversation = (bucket or {}).get("conversation")
            if not conversation:
                return
            last = conversation[-1]
            if getattr(last, "role", None) != "assistant" or is_tool_result(last):
                # No answer of this turn was stored (it was stopped); the only
                # assistant entries are sub-agent reports, which are not answers.
                return
            last.metadata = {**(last.metadata or {}), "trace": self._recorder.snapshot()}
            if manager.persist_path:
                manager._save_to_file()


class ChatSession:
    """Command loop for one chat websocket."""

    def __init__(self, server: Any, websocket: WebSocket, context_id: str) -> None:
        self._server = server
        self._socket = websocket
        self.context_id = context_id
        self.runtime = server.runtime
        self.manager = self.runtime.context_manager()
        self.send_lock = asyncio.Lock()

    @property
    def _turns(self) -> dict[str, tuple[AgentTurn, asyncio.Task]]:
        return self._server._chat_turns

    async def serve(self) -> None:
        await self._socket.accept()
        with safe_lock(self.manager._lock):
            if self.context_id not in self.manager._contexts:
                self.manager._create_context(self.context_id)
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
        return self._server.agent_label(system_key, agent_key)

    def release(self) -> None:
        self._server._active_chat_contexts.discard(self.context_id)

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
            await self._stop()
            return
        if payload.get("action") == "attach":
            await self._attach()
            return

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return
        await self._start(message.strip(), payload.get("system_key"), payload.get("agent_key"))

    async def _attach(self) -> None:
        active = self._turns.get(self.context_id)
        if active is None:
            # The turn ended between the page loading and attaching; the stored
            # conversation already holds its result.
            await self.send({"type": "done", "detached": True})
            return
        await active[0].attach(self)

    async def _stop(self) -> None:
        active = self._turns.get(self.context_id)
        if active is not None and not active[1].done():
            await self._cancel(active[1])
        else:
            await self.send({"type": "done", "stopped": True})

    async def _start(self, message: str, system_key: Any, agent_key: Any) -> None:
        if self.context_id in self._server._active_chat_contexts:
            await self.send({"type": "busy", "content": "This conversation already has an active turn."})
            return
        if not self._server.selection_is_valid(system_key, agent_key):
            await self.send({"type": "error", "content": "Unknown system or agent"})
            await self.send({"type": "done"})
            return
        self._server._active_chat_contexts.add(self.context_id)
        turn = AgentTurn(self, message, system_key=system_key or None, agent_key=agent_key or None)
        task = asyncio.create_task(turn.run())
        self._turns[self.context_id] = (turn, task)
        task.add_done_callback(lambda _task: self._forget(turn))

    def _forget(self, turn: AgentTurn) -> None:
        active = self._turns.get(self.context_id)
        if active is not None and active[0] is turn:
            del self._turns[self.context_id]

    @staticmethod
    async def _cancel(task: asyncio.Task) -> None:
        if task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def chat_session(server: Any, websocket: WebSocket, context_id: str) -> None:
    await ChatSession(server, websocket, context_id).serve()
