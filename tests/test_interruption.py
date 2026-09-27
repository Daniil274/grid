"""Interrupted turns: every early end is recorded, and the next turn resumes it.

The SDK Runner is scripted (see test_run_agent); the factory, the context
manager and the SDK sessions are real.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from agents import RunItemStreamEvent
from agents.exceptions import MaxTurnsExceeded

from core.context import ContextManager
from core.interruption import (
    CONTINUATION_TYPE,
    CONTINUE_TEXT,
    CallLedger,
    Interruption,
    RunControl,
    StopReason,
    interruption_of,
)
from tests.test_run_agent import ScriptedRunner, factory, use  # noqa: F401 - fixture
from utils.exceptions import AgentError

OBSERVER = SimpleNamespace(handle_event=lambda event, agent_key=None: None)
RESUME_MARK = "[Resuming an interrupted turn]"


def stored(factory, context_id):
    return factory.context_manager.conversation_view(context_id)["messages"]


def last_record(factory, context_id):
    return interruption_of(stored(factory, context_id)[-1])


def called(call_id, tool="click", arguments='{"name": "OK"}'):
    raw = {"type": "function_call", "name": tool, "call_id": call_id, "arguments": arguments}
    return RunItemStreamEvent(name="tool_called", item=SimpleNamespace(type="tool_call_item", raw_item=raw))


def returned(call_id):
    raw = {"type": "function_call_output", "call_id": call_id, "output": "ok"}
    return RunItemStreamEvent(name="tool_output", item=SimpleNamespace(type="tool_call_output_item", raw_item=raw))


class FakeStream:
    """A streamed run: one list of events per step; honors cancel(after_turn).

    An awaitable factory in a step is awaited in place - a tool that takes time.
    Unhashable, like the SDK's RunResultStreaming dataclass.
    """

    __hash__ = None

    def __init__(self, *steps, final_output=None):
        self.steps = steps
        self.final_output = None
        self.new_items = []
        self._final = final_output
        self.cancel_mode = None

    def cancel(self, mode="immediate"):
        self.cancel_mode = mode

    async def stream_events(self):
        for index, step in enumerate(self.steps):
            for event in step:
                if callable(event):
                    await event()
                else:
                    yield event
            if index == len(self.steps) - 1:
                # The last step is the one that answers.
                self.final_output = self._final
            elif self.cancel_mode == "after_turn":
                return


class StreamingRunner:
    def __init__(self, *streams):
        self.streams = list(streams)
        self.calls = []

    def run_streamed(self, agent, run_input, *, context, max_turns, session, run_config=None):
        self.calls.append(SimpleNamespace(input=run_input, session=session, context=context))
        return self.streams.pop(0)


async def hang():
    await asyncio.Event().wait()


# -- every early end leaves a record ---------------------------------------


async def test_a_timeout_is_recorded_and_answered_with_its_summary(factory):
    factory.config.get_agent_timeout = lambda: 0.05
    with use(ScriptedRunner(hang)):
        answer = await factory.run_agent("worker", "Open Notepad")
    context_id = factory.context_manager.get_current_context_id()

    record = last_record(factory, context_id)
    assert record.reason is StopReason.TIMEOUT
    assert record.task == "Open Notepad"
    assert "ran out of time" in answer and "settings.agent_timeout" in answer
    # The record is the answer: no second assistant message.
    assert [m.role for m in stored(factory, context_id)] == ["user", "assistant"]


async def test_the_turn_limit_is_recorded(factory):
    with use(ScriptedRunner(MaxTurnsExceeded("max"))):
        answer = await factory.run_agent("worker", "Go")
    context_id = factory.context_manager.get_current_context_id()
    assert last_record(factory, context_id).reason is StopReason.MAX_TURNS
    assert "turn limit" in answer


async def test_a_failed_turn_is_recorded_before_the_error_propagates(factory):
    with use(ScriptedRunner(ValueError("schema mismatch"))):
        with pytest.raises(AgentError):
            await factory.run_agent("worker", "Go")
    context_id = factory.context_manager.get_current_context_id()
    record = last_record(factory, context_id)
    assert record.reason is StopReason.ERROR
    assert "schema mismatch" in record.detail


async def test_a_failure_before_the_request_is_stored_leaves_no_record(factory):
    factory.create_agent = AsyncMock(side_effect=RuntimeError("no such model"))
    context_id = factory.context_manager.start_new_context()
    with pytest.raises(RuntimeError):
        await factory.run_agent("worker", "Go", context_id=context_id)
    assert stored(factory, context_id) == []


async def test_graceful_stop_finishes_the_step_then_records_it(factory):
    context_id = factory.context_manager.start_new_context()
    at_tool, release = asyncio.Event(), asyncio.Event()

    async def tool_runs():
        at_tool.set()
        await release.wait()

    stream = FakeStream(
        [called("c1"), tool_runs, returned("c1")],
        [called("c2", tool="perceive"), returned("c2")],
        final_output="Done.",
    )
    runner = StreamingRunner(stream)
    with patch("agents.Runner", runner):
        turn = asyncio.create_task(
            factory.run_agent("worker", "Click OK", context_id=context_id, stream=True, stream_observer=OBSERVER)
        )
        await at_tool.wait()
        assert factory.request_stop(context_id) is True
        release.set()
        answer = await turn

    assert stream.cancel_mode == "after_turn"
    record = last_record(factory, context_id)
    assert record.reason is StopReason.USER_STOP
    # The step in progress finished; the next one never started.
    assert record.completed == ["click"]
    assert record.in_flight == []
    assert "stopped by the user" in answer
    assert factory.request_stop(context_id) is False  # the turn is over


async def test_a_stop_that_arrives_with_the_final_answer_changes_nothing(factory):
    context_id = factory.context_manager.start_new_context()
    stream = FakeStream([], final_output="All done.")
    stream.cancel("after_turn")
    with patch("agents.Runner", StreamingRunner(stream)):
        answer = await factory.run_agent(
            "worker", "Go", context_id=context_id, stream=True, stream_observer=OBSERVER
        )
    assert answer.startswith("All done.")
    assert last_record(factory, context_id) is None


async def test_a_cancelled_turn_records_the_call_it_was_running(factory):
    context_id = factory.context_manager.start_new_context()
    at_tool = asyncio.Event()

    async def tool_hangs():
        at_tool.set()
        await hang()

    stream = FakeStream([called("c1", arguments='{"x": 10, "y": 20}'), tool_hangs])
    with patch("agents.Runner", StreamingRunner(stream)):
        turn = asyncio.create_task(
            factory.run_agent("worker", "Click", context_id=context_id, stream=True, stream_observer=OBSERVER)
        )
        await at_tool.wait()
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    record = last_record(factory, context_id)
    assert record.reason is StopReason.USER_STOP
    assert record.in_flight == [{"tool": "click", "arguments": '{"x": 10, "y": 20}'}]


# -- resuming ----------------------------------------------------------------


async def test_continue_resumes_the_same_request_once(factory):
    factory.config.get_agent_timeout = lambda: 0.05
    with use(ScriptedRunner(hang)):
        await factory.run_agent("worker", "Open Notepad")
    context_id = factory.context_manager.get_current_context_id()
    factory.config.get_agent_timeout = lambda: 30

    runner = ScriptedRunner("Notepad is open.")
    with use(runner):
        answer = await factory.continue_agent("worker", context_id)

    assert answer.startswith("Notepad is open.")
    sent = runner.calls[0].input
    assert sent.startswith(RESUME_MARK)
    assert "ran out of time" in sent and "Continue the same request" in sent
    # Same session: the model sees everything the stopped run completed.
    assert runner.calls[0].session is factory.sessions[("worker", context_id)]

    messages = stored(factory, context_id)
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1].metadata["resumed"] is True
    assert messages[2].content == CONTINUE_TEXT
    assert messages[2].metadata["type"] == CONTINUATION_TYPE

    with use(ScriptedRunner("unused")), pytest.raises(AgentError):
        await factory.continue_agent("worker", context_id)


async def test_continue_without_an_interruption_is_refused_and_stores_nothing(factory):
    with use(ScriptedRunner("Hi.")):
        await factory.run_agent("worker", "Hello")
    context_id = factory.context_manager.get_current_context_id()
    before = len(stored(factory, context_id))
    with use(ScriptedRunner("unused")), pytest.raises(AgentError):
        await factory.continue_agent("worker", context_id)
    assert len(stored(factory, context_id)) == before


async def test_a_typed_message_resumes_with_the_calls_in_flight(factory):
    context_id = factory.context_manager.start_new_context()
    at_tool = asyncio.Event()

    async def tool_hangs():
        at_tool.set()
        await hang()

    with patch("agents.Runner", StreamingRunner(FakeStream([called("c1"), tool_hangs]))):
        turn = asyncio.create_task(
            factory.run_agent("worker", "Click OK", context_id=context_id, stream=True, stream_observer=OBSERVER)
        )
        await at_tool.wait()
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    runner = ScriptedRunner("Checked and clicked.")
    with use(runner):
        await factory.run_agent("worker", "go on, but check first", context_id=context_id)

    sent = runner.calls[0].input
    assert sent.startswith(RESUME_MARK)
    assert 'click({"name": "OK"})' in sent
    assert "Check the current state" in sent
    assert sent.endswith("go on, but check first")
    messages = stored(factory, context_id)
    assert messages[-2].content == "go on, but check first"
    assert messages[-3].metadata["resumed"] is True


async def test_only_the_interrupted_agent_resumes_its_turn(factory):
    factory.config.get_agent_timeout = lambda: 0.05
    with use(ScriptedRunner(hang)):
        await factory.run_agent("worker", "Open Notepad")
    context_id = factory.context_manager.get_current_context_id()
    factory.config.get_agent_timeout = lambda: 30

    with use(ScriptedRunner("unused")), pytest.raises(AgentError):
        await factory.continue_agent("reviewer", context_id)

    runner = ScriptedRunner("Reviewed.")
    with use(runner):
        await factory.run_agent("reviewer", "Review it", context_id=context_id)
    assert not str(runner.calls[0].input).startswith(RESUME_MARK)
    # Left behind: the conversation no longer ends with it.
    assert factory.context_manager.pending_interruption(context_id) is None


async def test_a_retry_after_a_stored_step_does_not_resend_the_request(factory):
    inputs = []

    class Runner:
        async def run(self, agent, run_input, *, context, max_turns, session, run_config=None):
            inputs.append(run_input)
            if len(inputs) == 1:
                # What the SDK does before a provider error cuts the run.
                await session.add_items([{"role": "user", "content": str(run_input)}])
                raise httpx.ConnectError("connection reset")
            return SimpleNamespace(final_output="Recovered.", new_items=[])

    with use(Runner()), patch("asyncio.sleep", new=AsyncMock()):
        answer = await factory.run_agent("worker", "Build the report")

    assert answer.startswith("Recovered.")
    assert inputs[0] == "Build the report"
    assert inputs[1].startswith(RESUME_MARK) and "connection reset" in inputs[1]


async def test_a_retry_before_anything_was_stored_sends_the_request_again(factory):
    runner = ScriptedRunner(httpx.ConnectError("reset"), "Recovered.")
    with use(runner), patch("asyncio.sleep", new=AsyncMock()):
        await factory.run_agent("worker", "Build the report")
    assert [call.input for call in runner.calls] == ["Build the report", "Build the report"]


# -- a process that ended mid-turn -------------------------------------------


def abandoned(manager: ContextManager, *, ends_with_answer=False):
    context_id = manager.start_new_context()
    manager.append_message_to(context_id, "user", "Open Notepad")
    if ends_with_answer:
        manager.append_message_to(context_id, "assistant", "Opened.")
    manager.update_context_metadata(
        context_id,
        {
            "pending_agent_run": {
                "status": "running",
                "agent": "worker",
                "task": "Open Notepad",
                "turn_id": "t1",
                "tool_events": [
                    {"event_type": "tool_called", "tool_name": "hotkey", "call_id": "c1", "arguments": "win+r"},
                    {"event_type": "tool_called", "tool_name": "perceive", "call_id": "c0", "arguments": "{}"},
                    {"event_type": "tool_output", "tool_name": "perceive", "call_id": "c0"},
                ],
            }
        },
    )
    return context_id


def test_an_abandoned_turn_is_recovered_once():
    manager = ContextManager()
    context_id = abandoned(manager)

    record = manager.recover_abandoned_turn(context_id)

    assert record.reason is StopReason.CRASH
    assert record.task == "Open Notepad"
    assert record.in_flight == [{"tool": "hotkey", "arguments": "win+r"}]
    assert record.completed == ["perceive"]
    assert manager.pending_interruption(context_id) is not None
    assert manager.recover_abandoned_turn(context_id) is None


def test_a_turn_that_stored_its_answer_is_not_recovered():
    manager = ContextManager()
    context_id = abandoned(manager, ends_with_answer=True)
    assert manager.recover_abandoned_turn(context_id) is None
    assert manager.pending_interruption(context_id) is None
    assert manager.get_context_metadata(context_id)["pending_agent_run"]["status"] == "interrupted"


async def test_the_next_turn_resumes_an_abandoned_one(factory):
    context_id = abandoned(factory.context_manager)
    runner = ScriptedRunner("Done.")
    with use(runner):
        await factory.run_agent("worker", "continue", context_id=context_id)
    assert "runtime stopped" in runner.calls[0].input
    assert "hotkey(win+r)" in runner.calls[0].input


# -- building blocks -----------------------------------------------------------


def test_a_record_round_trips_and_stays_bounded():
    record = Interruption(
        reason=StopReason.ERROR,
        task="x" * 20000,
        agent="worker",
        detail="boom",
        in_flight=[{"tool": "click", "arguments": "a" * 5000}] * 50,
    )
    restored = Interruption.from_dict(record.to_dict())
    assert restored == record
    assert len(record.task) <= 8000
    assert len(record.in_flight) == 20
    assert len(record.in_flight[0]["arguments"]) <= 300
    assert Interruption.from_dict({"reason": "nonsense"}) is None
    assert Interruption.from_dict("not a record") is None


def test_the_ledger_closes_calls_by_id_or_by_tool():
    ledger = CallLedger()
    ledger.called("a", "click", "{}")
    ledger.called(None, "hotkey", "enter")
    ledger.called("b", "click", "{}")
    ledger.returned(None, "hotkey")
    ledger.returned("b", None)
    assert ledger.in_flight == [{"tool": "click", "arguments": "{}"}]
    assert ledger.completed == ["hotkey", "click"]


async def test_the_control_stops_runs_attached_before_and_after_the_request():
    control = RunControl()
    early, late = FakeStream(), FakeStream()
    control.attach(early)
    waiting = asyncio.create_task(control.wait(60))
    await asyncio.sleep(0)
    control.request_stop()
    assert await asyncio.wait_for(waiting, 1) is True
    control.attach(late)
    assert early.cancel_mode == late.cancel_mode == "after_turn"


# -- end to end: the web chat over the real factory ----------------------------


async def test_web_stop_then_continue_over_the_real_factory(factory):
    """Stop mid-step in the chat, then Continue: the same session, nothing lost."""
    from tests.test_web_chat_session import Socket
    from web_chat.session import chat_session
    from web_chat.turns import TurnBoard

    manager = factory.context_manager
    context_id = manager.start_new_context()

    async def resolve_turn(message, **kwargs):
        return SimpleNamespace(
            system=kwargs.get("system_key") or "s", agent=kwargs.get("agent_key") or "worker",
            factory=factory, routed=True, warning="",
        )

    space = SimpleNamespace(
        context_manager=lambda: manager, workspace_path=".", workspace_label=".", user_id="test", factory=factory, admit_turn=lambda: None,
        warm_agent=AsyncMock(), resolve_turn=resolve_turn,
        update_conversation_metadata=lambda ctx, **updates: manager.update_context_metadata(
            ctx, {key: value for key, value in updates.items() if value is not None}
        ),
        registry=SimpleNamespace(
            agent_label=lambda system, agent: "Worker",
            selection_is_valid=lambda system, agent: True,
            agent_issues=lambda system, agent: [],
        ),
        turns=TurnBoard(),
    )

    at_tool, release = asyncio.Event(), asyncio.Event()

    async def tool_runs():
        at_tool.set()
        await release.wait()

    runner = StreamingRunner(
        FakeStream([called("c1"), tool_runs, returned("c1")], [called("c2"), returned("c2")], final_output="unused"),
        FakeStream([], final_output="Clicked OK."),
    )
    socket = Socket()
    with patch("agents.Runner", runner):
        session = asyncio.create_task(chat_session(space, socket, context_id))
        await socket.incoming.put('{"message":"Click OK"}')
        await asyncio.wait_for(at_tool.wait(), 2)
        await socket.incoming.put('{"action":"stop"}')
        await socket.event("stopping")
        release.set()
        interrupted = await socket.event("interrupted")
        assert interrupted["interruption"]["reason"] == "user_stop"
        assert interrupted["interruption"]["completed"] == ["click"]
        assert interrupted["interruption"]["resumable"] is True
        await socket.event("done")

        await socket.incoming.put('{"action":"continue"}')
        assert (await socket.event("final_output"))["content"].startswith("Clicked OK.")
        await socket.event("done")
        await socket.incoming.put(None)
        await session

    first, second = runner.calls
    assert second.session is first.session
    assert second.input.startswith(RESUME_MARK)
    messages = stored(factory, context_id)
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1].metadata["resumed"] is True
    assert messages[1].metadata["trace"]  # the stopped turn keeps its timeline
    assert messages[2].metadata["type"] == CONTINUATION_TYPE
    assert manager.pending_interruption(context_id) is None


async def test_a_call_in_progress_is_on_disk_for_a_restarted_process(factory):
    """The process dies while a tool runs: a fresh manager reading the file recovers it."""
    context_id = factory.context_manager.start_new_context()
    at_tool = asyncio.Event()

    async def tool_hangs():
        at_tool.set()
        await hang()

    stream = FakeStream([called("c1", tool="hotkey", arguments='{"keys": "win+r"}'), tool_hangs])
    with patch("agents.Runner", StreamingRunner(stream)):
        turn = asyncio.create_task(
            factory.run_agent("worker", "Open Run", context_id=context_id, stream=True, stream_observer=OBSERVER)
        )
        await at_tool.wait()
        # What a new process sees: only the file.
        restarted = ContextManager(persist_path=str(factory.context_manager.persist_path))
        record = restarted.recover_abandoned_turn(context_id)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    assert record.reason is StopReason.CRASH
    assert record.task == "Open Run"
    assert record.in_flight == [{"tool": "hotkey", "arguments": '{"keys": "win+r"}'}]


def test_a_turn_that_failed_before_records_existed_can_be_continued():
    manager = ContextManager()
    context_id = manager.start_new_context()
    manager.append_message_to(context_id, "user", "Open Notepad")
    manager.update_context_metadata(context_id, {"pending_agent_run": {
        "status": "failed", "agent": "worker",
        "last_error": "Error code: 400 - Too many images in request: 31 > 30",
    }})

    record = manager.recover_abandoned_turn(context_id)

    assert record.reason is StopReason.ERROR
    assert "Too many images" in record.detail
    assert record.task == "Open Notepad"
    assert manager.pending_interruption(context_id) is not None
