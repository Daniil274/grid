"""Admin-requested restarts at saved agent step boundaries.

Only this planned handoff is resumed automatically. An unplanned crash can
leave a tool's effects unknown and still requires the normal Continue flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from core.interruption import StopReason
from web_chat.delivery import MessageQueue
from web_chat.session import ChatSession

logger = logging.getLogger("grid.web_chat.restart")


class SilentSocket:
    """A restored turn runs without a browser; a later socket can attach."""

    async def send_json(self, event: dict) -> None:
        pass


class ServerRestart:
    def __init__(
        self, spaces: Any, path: Path | None, stop_server: Callable[[], None] | None,
        *, other_work: Callable[[], bool] = lambda: False,
        may_resume: Callable[[str], bool] = lambda user_id: True,
    ) -> None:
        self.spaces, self.path, self.stop_server = spaces, path, stop_server
        self.other_work, self.may_resume = other_work, may_resume
        self.instance_id = uuid.uuid4().hex
        self.phase = "running"
        self.error: str | None = None
        self.recovery_errors = 0
        self._unrecovered: list[dict] = []
        self._checkpoint_broken = False
        self._task: asyncio.Task | None = None

    @property
    def pending(self) -> bool:
        return self.phase in {"preparing", "restarting"}

    def status(self) -> dict:
        return {
            "enabled": self.path is not None and self.stop_server is not None and not self._checkpoint_broken,
            "instance_id": self.instance_id,
            "phase": self.phase,
            "active_turns": sum(space.turns.claimed_count for space in self.spaces.live()),
            "other_work": self.other_work(),
            "error": self.error,
            "recovery_errors": self.recovery_errors,
        }

    def request(self) -> dict:
        if self._checkpoint_broken:
            raise ValueError("The saved restart checkpoint is unreadable. Inspect the server log before restarting.")
        if not self.status()["enabled"]:
            raise ValueError("Restart is available when running grid-web-chat directly.")
        if not self.pending:
            # Test persistence before stopping any agents.
            self._write(self._unrecovered)
            self.phase, self.error = "preparing", None
            self.spaces.pause_for_restart(True)
            self._task = asyncio.create_task(self._prepare(), name="server-restart")
        return self.status()

    def _write(self, turns: list[dict]) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_suffix(".tmp")
        with partial.open("w", encoding="utf-8") as stream:
            json.dump({"version": 1, "turns": turns}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, self.path)
        if os.name == "posix":
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    async def _prepare(self) -> None:
        captured: dict[tuple[str, str], tuple[Any, Any]] = {}
        try:
            # No timeout cancellation: a tool may already be changing external state.
            while True:
                for space in self.spaces.live():
                    for context_id, turn, _ in space.turns.running_turns():
                        captured[space.user_id, context_id] = (space, turn)
                        turn.pause_for_restart()
                if all(space.idle for space in self.spaces.live()) and not self.other_work():
                    break
                await asyncio.sleep(0.1)

            rows = []
            for (user_id, context_id), (space, turn) in captured.items():
                if turn.user_stopped:
                    continue
                manager = space.context_manager()
                interruption = manager.pending_interruption(context_id)
                queued = MessageQueue(manager, context_id).next_queued()
                if turn.restart_paused and interruption is not None and interruption.reason == StopReason.USER_STOP:
                    if interruption.in_flight:
                        raise RuntimeError("A paused tool has an unknown outcome. Inspect the chat before restarting.")
                    mode = "continue"
                elif queued is not None and (turn.outcome == "answered" or queued.get("delivery") == "now"):
                    mode = "queue"
                else:
                    continue
                rows.append({"user_id": user_id, "context_id": context_id, "turn_id": turn.run_id, "mode": mode})
            for space in self.spaces.live():
                # Ordinary history writes log errors; a restart must refuse on one.
                space.context_manager().save(strict=True)
            current = {(row["user_id"], row["context_id"]) for row in rows}
            rows.extend(row for row in self._unrecovered if (row["user_id"], row["context_id"]) not in current)
            self._write(rows)
            self.phase = "restarting"
            # Let the HTTP 202/status response leave before closing the listener.
            await asyncio.sleep(0.5)
            self.stop_server()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Server restart refused before shutdown")
            self.error = "Restart could not save a safe checkpoint. The server is still running; use Continue in paused chats and check the server log."
            self.phase = "failed"
            self.spaces.pause_for_restart(False)

    async def recover(self) -> None:
        """Run once at startup, before HTTP requests can race the handoff."""
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("turns"), list):
                raise ValueError("Unknown restart checkpoint format")
            remaining = list(data["turns"])
            for row in list(remaining):
                try:
                    await self._resume(row)
                except Exception:
                    self.recovery_errors += 1
                    logger.exception("Could not resume a saved restart turn")
                    continue
                remaining.remove(row)
                self._write(remaining)
            if not remaining:
                self.path.unlink(missing_ok=True)
            self._unrecovered = remaining
        except Exception:
            self.recovery_errors += 1
            self._checkpoint_broken = True
            logger.exception("Could not read or update restart checkpoint")

    async def _resume(self, row: dict) -> None:
        user_id, context_id = row["user_id"], row["context_id"]
        if not self.may_resume(user_id):
            return  # Deleted/disabled accounts cannot be revived by a checkpoint.
        async with self.spaces.use(user_id) as space:
            manager = space.context_manager()
            pending = manager.get_context_metadata(context_id).get("pending_agent_run") or {}
            if pending.get("turn_id") != row["turn_id"] or space.turns.is_claimed(context_id):
                return  # A newer turn already superseded this handoff.
            session = ChatSession(space, SilentSocket(), context_id)
            if row["mode"] == "continue":
                interruption = manager.pending_interruption(context_id)
                if interruption is None:
                    return
                if interruption.reason != StopReason.USER_STOP or interruption.in_flight:
                    raise ValueError("The saved interruption is not safe to resume automatically")
                if not await session._start(None, None, None, restart_resume=True):
                    raise RuntimeError("The saved turn could not be resumed")
            elif row["mode"] == "queue":
                queue = MessageQueue(manager, context_id)
                if (item := queue.next_queued()) is not None:
                    await session._start_queued(queue, item)
            else:
                raise ValueError("Unknown restart continuation mode")

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
