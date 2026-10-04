"""A turn that stopped before its answer, and how the agent picks it back up.

Every way a top-level turn can stop early leaves the same record: the user's
Stop, settings.agent_timeout, settings.max_turns, a model or provider error, and
the process dying mid-turn. The record is an assistant message in the
conversation whose metadata holds an :class:`Interruption`.

What survives a stop:

- The agent's SDK session keeps every step that finished - each model response
  together with the tool calls it made and their results. Only the step in
  progress is lost.
- The record adds what the session cannot know: why the turn stopped, and which
  tool calls had started without returning (they may or may not have taken
  effect).

Resuming: the next turn of a conversation that ends in an unresumed record
continues it, whether the user pressed Continue or typed a message. The model
gets :meth:`Interruption.resume_input` - the reason, the calls in flight, and
either "continue the same request" or the user's new message. The record is
then marked resumed, so it is continued at most once.

A Continue is not a new request: the policy gate keeps judging actions against
the task the user gave when the turn started (:attr:`Interruption.task`).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Optional

from core.steering import Steering

#: ``metadata["type"]`` of the assistant message that records an interruption.
INTERRUPTED_TYPE = "agent_interrupted"
#: ``metadata["type"]`` of the stored user entry of a Continue without text.
CONTINUATION_TYPE = "continuation"
#: ``metadata["type"]`` of the display-only marker a chat thread shows where its
#: context was compacted. It lives only in the visible conversation log - the
#: model's session never sees it - and is not part of the dialogue: an
#: interruption before it is still the one Continue resumes.
COMPACTED_TYPE = "context_compacted"
#: What a Continue without text stores as the user's message.
CONTINUE_TEXT = "Continue."

# Bounds on what the record keeps; it is stored with the conversation and sent
# to the model, so nothing in it may grow without limit.
_TASK_CHARS = 8000
_DETAIL_CHARS = 700
_LAST_TEXT_CHARS = 1500
_ARGUMENT_CHARS = 300
_MAX_CALLS = 20


class StopReason(str, Enum):
    USER_STOP = "user_stop"
    TIMEOUT = "timeout"
    MAX_TURNS = "max_turns"
    ERROR = "error"
    CRASH = "crash"


#: The message of a task cancellation that is the runtime going down, not the
#: user's Stop: ``task.cancel(SHUTDOWN)``. A bare cancel is the user's hard Stop.
SHUTDOWN = "grid:shutdown"


def cancel_reason(cancelled: BaseException) -> StopReason:
    """Why a turn's task was cancelled: :data:`SHUTDOWN` or the user's Stop."""
    return StopReason.CRASH if SHUTDOWN in cancelled.args else StopReason.USER_STOP


_REASON_TEXT = {
    StopReason.USER_STOP: "it was stopped by the user",
    StopReason.TIMEOUT: "it ran out of time",
    StopReason.MAX_TURNS: "it reached the turn limit",
    StopReason.ERROR: "it stopped on an error",
    StopReason.CRASH: "the runtime stopped while it was running",
}


def _clip(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


@dataclass
class Interruption:
    """Why a turn stopped and what was in progress; JSON-safe for storage."""

    reason: StopReason
    task: str
    agent: str
    detail: str = ""
    #: Tool calls that started and never returned: ``{"tool", "arguments"}``.
    in_flight: list[dict[str, str]] = field(default_factory=list)
    #: Names of the tool calls that finished before the stop, in order.
    completed: list[str] = field(default_factory=list)
    last_text: str = ""
    at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self) -> None:
        self.reason = StopReason(self.reason)
        self.task = _clip(self.task, _TASK_CHARS)
        self.detail = _clip(self.detail, _DETAIL_CHARS)
        self.last_text = _clip(self.last_text, _LAST_TEXT_CHARS)
        self.in_flight = [
            {
                "tool": _clip(call.get("tool") or "tool", 100),
                "arguments": _clip(call.get("arguments"), _ARGUMENT_CHARS),
            }
            for call in self.in_flight[:_MAX_CALLS]
            if isinstance(call, dict)
        ]
        self.completed = [_clip(name, 100) for name in self.completed if name]

    # -- storage -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reason"] = self.reason.value
        return data

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Interruption"]:
        """The stored record, or None when *data* is not one."""
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                reason=StopReason(data["reason"]),
                task=str(data.get("task") or ""),
                agent=str(data.get("agent") or ""),
                detail=str(data.get("detail") or ""),
                in_flight=list(data.get("in_flight") or []),
                completed=[str(name) for name in data.get("completed") or []],
                last_text=str(data.get("last_text") or ""),
                at=str(data.get("at") or ""),
            )
        except (KeyError, ValueError, TypeError):
            return None

    def message_metadata(self, *, context_id: str, turn_id: Optional[str]) -> dict[str, Any]:
        """Metadata of the assistant message that records this interruption."""
        return {
            "type": INTERRUPTED_TYPE,
            "context_id": context_id,
            "agent": self.agent,
            "turn_id": turn_id,
            "interruption": self.to_dict(),
            "resumed": False,
        }

    # -- text --------------------------------------------------------------
    def _reason_sentence(self) -> str:
        sentence = _REASON_TEXT[self.reason]
        return f"{sentence}: {self.detail}" if self.detail else sentence

    def _call_lines(self) -> list[str]:
        return [
            f"- {call['tool']}({call['arguments']})" if call["arguments"] else f"- {call['tool']}()"
            for call in self.in_flight
        ]

    def summary(self) -> str:
        """What the conversation shows: stored as the message text."""
        lines = [f"The turn did not finish - {self._reason_sentence()}."]
        if self.completed:
            names = ", ".join(dict.fromkeys(self.completed))
            lines.append(
                f"It made {len(self.completed)} tool call(s) before stopping: {names}. "
                "Their effects may already be in place."
            )
        if self.in_flight:
            lines.append("In progress when it stopped (result unknown):")
            lines.extend(self._call_lines())
        if self.last_text:
            lines.append(f"Its last message:\n{self.last_text}")
        lines.append("Continue, or send a message, to pick up from here.")
        return "\n".join(lines)

    def resume_input(self, user_text: Optional[str] = None) -> str:
        """The model's input for the turn that resumes this one.

        ``user_text`` is what the user typed instead of pressing Continue; None
        means Continue.
        """
        lines = [
            "[Resuming an interrupted turn]",
            f"Your previous run did not finish - {self._reason_sentence()}.",
            "Everything you completed before that is in the conversation above: "
            "your messages, tool calls and their results.",
        ]
        if self.in_flight:
            lines.append(
                "These tool calls had started when the run stopped. Their results are "
                "unknown - they may or may not have taken effect:"
            )
            lines.extend(self._call_lines())
            lines.append("Check the current state before repeating any of them.")
        if user_text is None:
            lines.append(
                "Continue the same request from where you stopped. Do not start over "
                "and do not redo steps that already succeeded."
            )
        else:
            lines.append(
                "The user now writes the message below. If it asks you to go on, "
                "continue from where you stopped without redoing finished steps; "
                "otherwise follow it."
            )
            lines.append("")
            lines.append(user_text)
        return "\n".join(lines)


def interruption_of(message: Any) -> Optional[Interruption]:
    """The interruption a stored message records, or None."""
    metadata = getattr(message, "metadata", None) or {}
    if metadata.get("type") != INTERRUPTED_TYPE:
        return None
    return Interruption.from_dict(metadata.get("interruption"))


def is_resumable(message: Any) -> bool:
    """A recorded interruption that no later turn has continued yet."""
    metadata = getattr(message, "metadata", None) or {}
    return interruption_of(message) is not None and not metadata.get("resumed")


class StopRequested(Exception):
    """A run ended by the user's graceful Stop before it had an answer."""

    def __str__(self) -> str:
        return "stopped by the user"


class CallLedger:
    """Tool calls of one run, as the stream reports them.

    The SDK reports ``tool_called`` when the model has finished writing a call,
    before it runs, and ``tool_output`` after it returns; a call with the first
    and not the second was running when the run stopped.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict[str, str]] = {}
        self._completed: list[str] = []
        self._unnamed = 0

    def called(self, call_id: Optional[str], tool: Optional[str], arguments: Any) -> None:
        if not call_id:
            self._unnamed += 1
            call_id = f"unnamed-{self._unnamed}"
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, default=str)
        self._open[call_id] = {"tool": tool or "tool", "arguments": arguments}

    def returned(self, call_id: Optional[str], tool: Optional[str]) -> None:
        call = self._open.pop(call_id, None) if call_id else None
        if call is None and tool:
            # No id to match: close the oldest open call of the same tool.
            for key, candidate in self._open.items():
                if candidate["tool"] == tool:
                    call = self._open.pop(key)
                    break
        self._completed.append((call or {}).get("tool") or tool or "tool")

    def forget_open(self) -> None:
        """Drop the open calls once they were reported, before a rerun."""
        self._open.clear()

    @property
    def in_flight(self) -> list[dict[str, str]]:
        return list(self._open.values())

    @property
    def completed(self) -> list[str]:
        return list(self._completed)


class RunControl:
    """The stop switch of one top-level turn and every sub-agent run inside it.

    :meth:`request_stop` is the graceful stop: each streamed run finishes the
    step it is on - the model's response and the tool calls it made - saves it
    to its session and ends before the next step. A run attached after the
    request stops at the end of its first step. The hard stop is cancelling
    the task that runs the turn; it needs nothing from here.
    """

    def __init__(self) -> None:
        # A list compared by identity: SDK results are unhashable dataclasses.
        self._runs: list[Any] = []
        self._stopped = asyncio.Event()
        # Messages the user sends to this turn while it runs (core.steering).
        self.steering = Steering()
        # The turn's policy state: a message the user adds mid-turn joins its
        # trusted task, as the user's own words (AgentFactory.steer).
        self.action_state: Any = None

    @property
    def stop_requested(self) -> bool:
        return self._stopped.is_set()

    def attach(self, streamed_result: Any) -> None:
        self._runs.append(streamed_result)
        if self.stop_requested:
            streamed_result.cancel(mode="after_turn")

    def detach(self, streamed_result: Any) -> None:
        self._runs = [run for run in self._runs if run is not streamed_result]

    def request_stop(self) -> None:
        self._stopped.set()
        for run in list(self._runs):
            run.cancel(mode="after_turn")

    async def wait(self, seconds: float) -> bool:
        """Sleep up to *seconds*, ending early on a stop. True if stopped."""
        if self.stop_requested:
            return True
        sleeper = asyncio.ensure_future(asyncio.sleep(seconds))
        stopper = asyncio.ensure_future(self._stopped.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, stopper):
                task.cancel()
        return self.stop_requested


def completed_names(items: Iterable[Any]) -> list[str]:
    """Tool names of the calls among SDK run items (non-streamed runs)."""
    from core.run_stream import tool_event_info  # run_stream imports the SDK

    return [
        tool_event_info(item).get("tool_name") or "tool"
        for item in items
        if getattr(item, "type", "") == "tool_call_item"
    ]
