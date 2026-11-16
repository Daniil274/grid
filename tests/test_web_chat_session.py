"""Commands must remain responsive while agent inference is suspended."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.context import ContextManager
from schemas import AgentExecution
from fastapi import WebSocketDisconnect

from web_chat.session import (
    MAX_REPLAY_NOTES,
    AgentTurn,
    ChatSession,
    chat_session,
    public_activity,
)
from web_chat.trace import ContextRef, StepKind
from web_chat.turns import TurnBoard


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def accept(self):
        pass

    async def receive_text(self):
        value = await self.incoming.get()
        if value is None:
            raise WebSocketDisconnect()
        return value

    async def send_json(self, value):
        await self.outgoing.put(value)

    async def event(self, kind):
        async with asyncio.timeout(2):
            while True:
                value = await self.outgoing.get()
                if value['type'] == kind:
                    return value


def space_for(run, *, request_stop=lambda context_id: False, continue_agent=None):
    """A space whose single system 's' holds one agent 'a' running *run*.

    ``request_stop`` is the factory's graceful Stop; by default the agent
    cannot stop gracefully, so Stop cancels the turn.
    """
    manager = ContextManager()
    manager.ensure_context('ctx')
    factory = SimpleNamespace(
        run_agent=run, request_stop=request_stop, continue_agent=continue_agent
    )

    async def resolve_turn(message, **kwargs):
        return SimpleNamespace(
            system=kwargs.get('system_key') or 's', agent=kwargs.get('agent_key') or 'a',
            config=None, factory=factory, routed_system=False,
            routed_agent=kwargs.get('agent_key') is None, routed=True, warning='',
        )

    registry = SimpleNamespace(
        agent_label=lambda system, agent: 'Agent',
        selection_is_valid=lambda system, agent: True,
        agent_issues=lambda system, agent: [],
    )
    return SimpleNamespace(
        context_manager=lambda: manager, workspace_path='.', workspace_label='.', user_id='test', admit_turn=lambda turn=None: None,
        workspace_label_of=lambda context_id: '.', registry_of=lambda context_id: registry,
        factory=factory, warm_agent=AsyncMock(), resolve_turn=resolve_turn,
        update_conversation_metadata=lambda *a, **kw: None,
        registry=registry, turns=TurnBoard(), deployment=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_stop_cancels_running_agent_without_waiting_for_output():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def run(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"action":"stop"}')
    assert (await socket.event('done'))['stopped'] is True
    assert cancelled.is_set()
    assert not space.turns.is_claimed('ctx')
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_stop_during_agent_warmup_and_next_turn():
    run = AsyncMock(return_value='Ready')
    space, socket = space_for(run), Socket()
    started = asyncio.Event()

    async def warm(agent_key, system_key=None, *, context_id=None):
        started.set()
        await asyncio.Event().wait()

    space.warm_agent = warm
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"first"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"action":"stop"}')
    first = await socket.event('done')
    assert first['stopped'] is True
    run.assert_not_awaited()
    space.warm_agent = AsyncMock()
    await socket.incoming.put('{"message":"second"}')
    assert (await socket.event('final_output'))['content'] == 'Ready'
    second = await socket.event('done')
    assert second['run_id'] != first['run_id']
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_error_is_terminal_and_disconnect_cleans_up():
    space, socket = space_for(AsyncMock(side_effect=ValueError('failed'))), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')
    assert (await socket.event('error'))['content'] == 'failed'
    await socket.event('done')
    assert not space.turns.is_claimed('ctx')
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_message_from_another_tab_waits_for_the_running_turn():
    """Turns never overlap: a second message is queued and sent when the first answers."""
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def run(**kwargs):
        seen.append(kwargs["message"])
        if len(seen) == 1:
            started.set()
            await release.wait()
        return "done"

    space = space_for(run)
    first, second = Socket(), Socket()
    tasks = [asyncio.create_task(chat_session(space, s, 'ctx')) for s in (first, second)]
    await first.incoming.put('{"message":"first"}')
    await asyncio.wait_for(started.wait(), 2)
    await second.incoming.put('{"message":"second"}')
    queued = await second.event('queue')
    assert [item["text"] for item in queued["items"]] == ["second"]
    assert queued["items"][0]["delivery"] == "after_turn"  # no decision model: the safe default
    release.set()
    await first.event('done')
    for _ in range(50):
        if seen == ["first", "second"]:
            break
        await asyncio.sleep(0.02)
    assert seen == ["first", "second"]
    for socket, task in zip((first, second), tasks):
        await socket.incoming.put(None)
        await task


@pytest.mark.asyncio
async def test_reload_keeps_the_turn_running_and_replays_it():
    """Closing the page detaches; a new socket catches up, then follows live."""
    from types import SimpleNamespace as NS

    from agents import RunItemStreamEvent

    started, release = asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()

    async def run(**kwargs):
        observer = kwargs['stream_observer']
        observer.handle_event(RunItemStreamEvent(name='tool_called', item=NS(
            raw_item=NS(name='read_file', call_id='c1', arguments='{}', type='function_call'))))
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return 'Finished.'

    space = space_for(run)
    first = Socket()
    first_task = asyncio.create_task(chat_session(space, first, 'ctx'))
    await first.incoming.put('{"message":"work"}')
    await asyncio.wait_for(started.wait(), 2)
    await first.incoming.put(None)  # the page reloads
    await first_task
    await asyncio.sleep(0)
    assert not cancelled.is_set()
    assert space.turns.get('ctx') is not None

    second = Socket()
    second_task = asyncio.create_task(chat_session(space, second, 'ctx'))
    await second.incoming.put('{"action":"attach"}')
    attached = await second.event('attached')
    assert attached['message'] == 'work'
    replayed = await second.event('step')
    assert replayed['step']['kind'] in {'routing', 'prepare', 'tool'}

    release.set()
    assert (await second.event('final_output'))['content'] == 'Finished.'
    await second.event('done')
    await asyncio.sleep(0)
    assert space.turns.get('ctx') is None
    await second.incoming.put(None)
    await second_task


@pytest.mark.asyncio
async def test_attach_without_a_running_turn_reports_it_is_over():
    space, socket = space_for(AsyncMock(return_value='x')), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"action":"attach"}')
    assert (await socket.event('done'))['detached'] is True
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_stop_from_a_reattached_page_cancels_the_turn():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def run(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    space = space_for(run)
    first, second = Socket(), Socket()
    first_task = asyncio.create_task(chat_session(space, first, 'ctx'))
    await first.incoming.put('{"message":"work"}')
    await asyncio.wait_for(started.wait(), 2)
    await first.incoming.put(None)
    await first_task

    second_task = asyncio.create_task(chat_session(space, second, 'ctx'))
    await second.incoming.put('{"action":"attach"}')
    await second.event('attached')
    await second.incoming.put('{"action":"stop"}')
    assert (await second.event('done'))['stopped'] is True
    assert cancelled.is_set()
    await second.incoming.put(None)
    await second_task


@pytest.mark.asyncio
async def test_turn_streams_trace_steps_and_answer_separately():
    """The client must receive thinking as trace steps, never as answer tokens."""
    from types import SimpleNamespace as NS

    from agents import RawResponsesStreamEvent, RunItemStreamEvent

    async def run(**kwargs):
        observer = kwargs['stream_observer']
        observer.handle_event(RawResponsesStreamEvent(
            data=NS(type='response.reasoning_text.delta', delta='Check the config first.')))
        observer.handle_event(RunItemStreamEvent(name='tool_called', item=NS(
            raw_item=NS(name='read_file', call_id='c1', arguments='{"path": "config.yaml"}', type='function_call'))))
        observer.handle_event(RunItemStreamEvent(name='tool_output', item=NS(
            raw_item=NS(call_id='c1', type='function_call_output'), output='settings: {}')))
        observer.handle_event(RawResponsesStreamEvent(
            data=NS(type='response.output_text.delta', delta='All set.')))
        return 'All set.'

    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')

    frames = []
    async with asyncio.timeout(2):
        while True:
            frames.append(await socket.outgoing.get())
            if frames[-1]['type'] == 'done':
                break

    assert [frame['content'] for frame in frames if frame['type'] == 'token'] == ['All set.']
    assert [frame['delta'] for frame in frames if frame['type'] == 'reasoning'] == ['Check the config first.']

    steps = {frame['step']['id']: frame['step'] for frame in frames if frame['type'] == 'step'}
    kinds = [step['kind'] for step in steps.values()]
    assert 'reasoning' in kinds and 'tool' in kinds and 'prepare' in kinds

    tool_step = next(step for step in steps.values() if step['kind'] == 'tool')
    assert tool_step['title'] == 'Read file'
    assert tool_step['body'] == 'settings: {}'
    assert [ref['label'] for ref in tool_step['refs']] == ['config.yaml']

    assert all(frame['run_id'] for frame in frames)
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_streamed_tokens_include_steps_that_the_final_answer_replaces():
    """Why the client speaks `final_output` and never the token stream.

    A run emits an assistant message before each tool call; only the last one
    becomes the result. Anything reading the tokens - the transcript before it
    settles, or speech - would carry narration the answer drops.
    """
    from types import SimpleNamespace as NS

    from agents import RawResponsesStreamEvent, RunItemStreamEvent

    async def run(**kwargs):
        observer = kwargs['stream_observer']
        observer.handle_event(RawResponsesStreamEvent(
            data=NS(type='response.output_text.delta', delta='Let me check the config.')))
        observer.handle_event(RunItemStreamEvent(name='tool_called', item=NS(
            raw_item=NS(name='read_file', call_id='c1', arguments='{}', type='function_call'))))
        observer.handle_event(RunItemStreamEvent(name='tool_output', item=NS(
            raw_item=NS(call_id='c1', type='function_call_output'), output='ok')))
        observer.handle_event(RawResponsesStreamEvent(
            data=NS(type='response.output_text.delta', delta='Port 8080 is set.')))
        return 'Port 8080 is set.'

    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"which port?"}')

    frames = []
    async with asyncio.timeout(2):
        while True:
            frames.append(await socket.outgoing.get())
            if frames[-1]['type'] == 'done':
                break

    tokens = [frame['content'] for frame in frames if frame['type'] == 'token']
    final = next(frame['content'] for frame in frames if frame['type'] == 'final_output')

    assert tokens == ['Let me check the config.', 'Port 8080 is set.']
    assert final == 'Port 8080 is set.'
    assert ''.join(tokens) != final  # the narration is not part of the answer

    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_tool_problems_are_announced_before_the_agent_runs_and_do_not_stop_it():
    from core.tool_check import ENVIRONMENT, ToolIssue

    started = []

    async def run(**kwargs):
        started.append(True)
        return 'done anyway'

    space, socket = space_for(run), Socket()
    issue = ToolIssue(ENVIRONMENT, 'program not on PATH: ffmpeg', agent='a', tool='video_probe', hint='Install FFmpeg')
    space.registry.agent_issues = lambda system, agent: [issue]
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')

    frames = []
    async with asyncio.timeout(2):
        while True:
            frames.append(await socket.outgoing.get())
            if frames[-1]['type'] == 'done':
                break

    kinds = [frame['type'] for frame in frames]
    notice = next(frame for frame in frames if frame['type'] == 'tool_issues')
    assert notice['summary'] == ["tool 'video_probe' (agent 'a'): program not on PATH: ffmpeg (fix: Install FFmpeg)"]
    assert kinds.index('tool_issues') < kinds.index('final_output')
    warning = next(frame['step'] for frame in frames
                   if frame['type'] == 'step' and frame['step']['tone'] == 'warn')
    assert 'ffmpeg' in warning['body']
    assert started == [True]
    await socket.incoming.put(None)
    await session


def record_interruption(manager, turn_id, reason='user_stop'):
    """What the factory stores when a turn ends early."""
    from core.interruption import Interruption

    record = Interruption(reason=reason, task='Open Notepad', agent='a')
    manager.append_message_to('ctx', 'user', 'Open Notepad')
    manager.append_message_to(
        'ctx', 'assistant', record.summary(),
        record.message_metadata(context_id='ctx', turn_id=turn_id),
    )
    return record


@pytest.mark.asyncio
async def test_first_stop_lets_the_step_finish_and_the_turn_says_how_it_ended():
    started, released = asyncio.Event(), asyncio.Event()
    stops = []
    holder = {}

    async def run(**kwargs):
        started.set()
        await released.wait()
        record = record_interruption(holder['manager'], kwargs['turn_id'])
        return record.summary()

    def request_stop(context_id):
        stops.append(context_id)
        released.set()
        return True

    space, socket = space_for(run, request_stop=request_stop), Socket()
    holder['manager'] = space.context_manager()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"Open Notepad"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"action":"stop"}')

    await socket.event('stopping')
    interrupted = await socket.event('interrupted')
    assert interrupted['interruption']['reason'] == 'user_stop'
    assert interrupted['interruption']['resumable'] is True
    assert (await socket.event('done'))['stopped'] is False  # it ended by itself
    assert stops == ['ctx']
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_second_stop_cancels_a_turn_that_is_still_finishing_its_step():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def run(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    space, socket = space_for(run, request_stop=lambda context_id: True), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"action":"stop"}')
    await socket.event('stopping')
    assert not cancelled.is_set()
    await socket.incoming.put('{"action":"stop"}')
    assert (await socket.event('done'))['stopped'] is True
    assert cancelled.is_set()
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_continue_resumes_with_the_agent_that_was_interrupted():
    continued = []

    async def continue_agent(agent_key, context_id, **kwargs):
        continued.append((agent_key, context_id, kwargs['turn_id']))
        return 'Notepad is open.'

    space, socket = space_for(AsyncMock(), continue_agent=continue_agent), Socket()
    manager = space.context_manager()
    record_interruption(manager, 'earlier-turn', reason='timeout')
    manager.update_context_metadata('ctx', {'routed_system': 's', 'routed_agent': 'a'})

    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"action":"continue"}')
    assert (await socket.event('final_output'))['content'] == 'Notepad is open.'
    done = await socket.event('done')
    assert continued == [('a', 'ctx', done['run_id'])]
    space.factory.run_agent.assert_not_awaited()
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_continue_with_nothing_to_resume_is_refused():
    space, socket = space_for(AsyncMock()), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"action":"continue"}')
    assert 'nothing to continue' in (await socket.event('error'))['content']
    await socket.event('done')
    assert not space.turns.is_claimed('ctx')
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_stop_now_cancels_at_once_even_when_the_agent_could_finish_its_step():
    started, cancelled = asyncio.Event(), asyncio.Event()
    stops = []

    async def run(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    space = space_for(run, request_stop=lambda context_id: stops.append(context_id) or True)
    socket = Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"action":"stop","now":true}')
    assert (await socket.event('done'))['stopped'] is True
    assert cancelled.is_set()
    assert stops == []
    await socket.incoming.put(None)
    await session


def png_data_url(size=(32, 24)):
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, "green").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


@pytest.mark.asyncio
async def test_attached_images_reach_the_agent_as_an_sdk_message(tmp_path):
    import json

    run = AsyncMock(return_value="A green square.")
    space, socket = space_for(run), Socket()
    space.conversation_workspace = lambda context_id: tmp_path
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put(json.dumps({"message": "what is this?", "images": [png_data_url()]}))
    assert (await socket.event("final_output"))["content"] == "A green square."
    await socket.event("done")

    sent = json.loads(run.await_args.kwargs["message"])
    # The words, then the chat's block naming the kept file by its workspace path.
    assert sent["content"][0]["type"] == "input_text"
    assert sent["content"][0]["text"].startswith("what is this?\n\nAttached files:\n- [")
    assert ' — workspace path: "uploads/image-' in sent["content"][0]["text"]
    assert sent["content"][1]["type"] == "input_image"
    assert sent["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_an_image_alone_is_a_message(tmp_path):
    import json

    run = AsyncMock(return_value="ok")
    space, socket = space_for(run), Socket()
    space.conversation_workspace = lambda context_id: tmp_path
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put(json.dumps({"images": [png_data_url()]}))
    await socket.event("done")
    sent = json.loads(run.await_args.kwargs["message"])
    assert [part["type"] for part in sent["content"]] == ["input_text", "input_image"]
    assert sent["content"][0]["text"].startswith("Attached files:\n- [")
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_refused_image_starts_no_turn():
    import json

    run = AsyncMock(return_value="unused")
    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put(json.dumps({"message": "look", "images": ["data:image/svg+xml;base64,PHN2Zz4="]}))
    assert "not supported" in (await socket.event("error"))["content"]
    await socket.event("done")
    run.assert_not_awaited()
    assert not space.turns.is_claimed('ctx')
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_an_edited_message_reaches_the_agent_as_a_new_version():
    import json

    run = AsyncMock(return_value="ok")
    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put(json.dumps({"message": "better", "edit_of": "slot-1"}))
    await socket.event("done")
    assert run.await_args.kwargs["edit_of"] == "slot-1"
    await socket.incoming.put(json.dumps({"message": "plain", "edit_of": ["not", "a", "slot"]}))
    await socket.event("done")
    assert run.await_args.kwargs["edit_of"] is None
    await socket.incoming.put(None)
    await session


def running_agent():
    """A run that waits for release; records every message it was given."""
    started, release, seen = asyncio.Event(), asyncio.Event(), []

    async def run(**kwargs):
        seen.append(kwargs["message"])
        if len(seen) == 1:
            started.set()
            await release.wait()
        return "done"

    return run, started, release, seen


async def wait_for(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_a_next_step_message_reaches_the_running_agent_and_leaves_the_queue():
    run, started, release, seen = running_agent()
    given = []

    def steer(context_id, message):
        given.append(message.text)
        message.on_delivered(message)  # the agent's next call reads it at once here
        return True

    space, socket = space_for(run), Socket()
    space.factory.steer = steer
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put('{"message":"refactor"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"message":"keep the API","delivery":"next_step"}')
    steered = await socket.event("steered")
    assert steered["text"] == "keep the API" and given == ["keep the API"]
    assert (await socket.event("queue"))["items"] == []
    release.set()
    await socket.event("done")
    await asyncio.sleep(0.05)
    assert seen == ["refactor"]  # nothing left to send after the turn
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_now_message_stops_the_turn_and_is_sent_next():
    run, started, release, seen = running_agent()
    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put('{"message":"deploy to staging"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"message":"no, to production","delivery":"now"}')
    assert (await socket.event("done"))["stopped"] is True
    await wait_for(lambda: seen == ["deploy to staging", "no, to production"])
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_after_a_stop_the_queue_waits_for_the_user():
    run, started, release, seen = running_agent()
    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put('{"message":"first"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"message":"later","delivery":"after_turn"}')
    queued = (await socket.event("queue"))["items"]
    await socket.incoming.put('{"action":"stop","now":true}')
    await socket.event("done")
    await asyncio.sleep(0.1)
    assert seen == ["first"]

    # The user sends the waiting message after all.
    await socket.incoming.put(json.dumps({"action": "send_queued", "id": queued[0]["id"]}))
    await wait_for(lambda: seen == ["first", "later"])
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_waiting_message_can_be_dropped():
    run, started, release, seen = running_agent()
    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put('{"message":"first"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"message":"never mind","delivery":"after_turn"}')
    item = (await socket.event("queue"))["items"][0]
    await socket.incoming.put(json.dumps({"action": "unqueue", "id": item["id"]}))
    assert (await socket.event("queue"))["items"] == []
    release.set()
    await socket.event("done")
    await asyncio.sleep(0.1)
    assert seen == ["first"]
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_cancelled_next_step_message_is_taken_back_from_the_agent():
    run, started, release, seen = running_agent()
    pending, withdrawn = {}, []

    def steer(context_id, message):
        pending[message.message_id] = message
        return True

    def withdraw_steer(context_id, message_id):
        withdrawn.append(message_id)
        return pending.pop(message_id, None) is not None

    space, socket = space_for(run), Socket()
    space.factory.steer = steer
    space.factory.withdraw_steer = withdraw_steer
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put('{"message":"refactor"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"message":"drop the tests","delivery":"next_step"}')
    item = (await socket.event("queue"))["items"][0]
    assert item["state"] == "steering"
    await socket.incoming.put(json.dumps({"action": "unqueue", "id": item["id"]}))
    assert (await socket.event("queue"))["items"] == []
    assert withdrawn == [item["id"]] and pending == {}
    release.set()
    await socket.event("done")
    await asyncio.sleep(0.1)
    assert seen == ["refactor"]
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_the_decision_model_chooses_when_the_user_did_not():
    run, started, release, seen = running_agent()
    space, socket = space_for(run), Socket()
    decide = AsyncMock(return_value=("now", "model"))
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    with patch("web_chat.session.decide_delivery", new=decide):
        await socket.incoming.put('{"message":"build it"}')
        await asyncio.wait_for(started.wait(), 2)
        await socket.incoming.put('{"message":"stop, wrong branch"}')
        delivery = await socket.event("delivery")
    assert (delivery["delivery"], delivery["decided_by"]) == ("now", "model")
    kwargs = decide.await_args.kwargs
    assert kwargs["message"] == "stop, wrong branch" and kwargs["task"] == "build it"
    await wait_for(lambda: seen == ["build it", "stop, wrong branch"])
    await socket.incoming.put(None)
    await session


def _completed_usage(tokens_in: int, tokens_out: int):
    """A ``response.completed`` stream event carrying the model's usage."""
    from agents import RawResponsesStreamEvent

    usage = SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out)
    return RawResponsesStreamEvent(
        data=SimpleNamespace(type='response.completed', response=SimpleNamespace(usage=usage))
    )


async def _drain_until(socket, kind):
    events = []
    async with asyncio.timeout(2):
        while True:
            value = await socket.outgoing.get()
            events.append(value)
            if value['type'] == kind:
                return events


@pytest.mark.asyncio
async def test_a_turn_past_its_token_budget_stops_and_is_counted():
    stopped, recorded = [], []

    async def run(**kwargs):
        kwargs['stream_observer'].handle_event(_completed_usage(80, 40))
        return 'the answer'

    space = space_for(run, request_stop=lambda context_id: stopped.append(context_id) or True)
    space.limits = SimpleNamespace(token_budget=lambda: 100)
    space.record_usage = lambda tokens_in, tokens_out: recorded.append((tokens_in, tokens_out))
    socket = Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')

    events = await _drain_until(socket, 'done')
    done = events[-1]
    steps = [event['step'] for event in events if event['type'] == 'step']

    # The turn flagged the budget and asked the factory to stop it.
    assert stopped == ['ctx']
    assert any(
        step['kind'] == 'error' and step['title'] == 'Token budget exceeded'
        for step in steps
    )
    # 80 + 40 = 120 > 100: the tokens are counted even as the turn stops.
    assert recorded == [(80, 40)]
    assert done['tokens_in'] == 80 and done['tokens_out'] == 40

    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_turn_inside_its_token_budget_runs_to_the_end():
    stopped, recorded = [], []

    async def run(**kwargs):
        kwargs['stream_observer'].handle_event(_completed_usage(40, 30))
        return 'the answer'

    space = space_for(run, request_stop=lambda context_id: stopped.append(context_id) or True)
    space.limits = SimpleNamespace(token_budget=lambda: 100)
    space.record_usage = lambda tokens_in, tokens_out: recorded.append((tokens_in, tokens_out))
    socket = Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')

    done = await socket.event('done')

    assert stopped == []
    assert recorded == [(40, 30)]
    assert done['tokens_in'] == 40 and done['tokens_out'] == 30

    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_the_done_frame_carries_the_last_prompt_size_of_the_turn():
    async def run(**kwargs):
        kwargs['stream_observer'].handle_event(_completed_usage(80, 40))
        kwargs['stream_observer'].handle_event(_completed_usage(2100, 20))
        return 'the answer'

    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')

    done = await socket.event('done')

    # The turn totals both calls; the context is what the model saw last.
    assert done['tokens_in'] == 2180 and done['tokens_out'] == 60
    assert done['context_tokens'] == 2100

    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_done_frame_without_usage_has_no_context_size():
    run = AsyncMock(return_value='Ready')
    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')

    done = await socket.event('done')

    # Absent, not zero: the UI shows nothing rather than "0".
    assert 'context_tokens' not in done

    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_stop_in_the_same_burst_as_the_message_frees_the_chat():
    """Message and Stops read before the turn's task took a step: the turn still
    ends as stopped, and the chat takes the next message instead of "busy"."""
    run = AsyncMock(return_value='Ready')
    space, socket = space_for(run), Socket()
    for frame in ('{"message":"first"}', '{"action":"stop"}', '{"action":"stop"}'):
        socket.incoming.put_nowait(frame)
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    assert (await socket.event('done'))['stopped'] is True
    assert not space.turns.is_claimed('ctx')
    await socket.incoming.put('{"message":"second"}')
    assert (await socket.event('final_output'))['content'] == 'Ready'
    await socket.event('done')
    await socket.incoming.put(None)
    await session


def test_a_late_release_never_frees_a_newer_claim():
    board = TurnBoard()
    first, second = object(), object()
    assert board.claim('ctx', first)
    board.release('ctx', first)
    assert board.claim('ctx', second)
    board.release('ctx', first)  # the old turn's cleanup, arriving late
    assert board.is_claimed('ctx')
    board.release('ctx', second)
    assert not board.is_claimed('ctx')


# -- the projects API -------------------------------------------------------------
#
# The projects routes mount on the real WebChatServer; the REST create/list
# cases live here with them (this module otherwise drives the WebSocket
# session). Everything is imported locally so the section stands on its own.


@pytest.fixture
def projects_space(tmp_path):
    """A dummy space with a real ContextManager and a real ProjectStore."""
    from core.context import ContextManager

    from tests.test_web_ui_routes import _DummySpace
    from web_chat.projects import ProjectStore

    dummy = _DummySpace()
    manager = ContextManager(persist_path=str(tmp_path / "conversations.json"))
    dummy._context_manager = manager
    dummy.conversations_path = tmp_path / "conversations.json"
    store = ProjectStore(tmp_path / "projects.json")
    dummy.project_store = store
    dummy.project_exists = store.exists

    def update(context_id, **updates):
        manager.update_context_metadata(context_id, {k: v for k, v in updates.items() if v is not None})

    def by_project():
        grouped = {}
        for view in manager.conversation_views():
            project_id = (view.get("metadata") or {}).get("project_id") or None
            grouped.setdefault(project_id, []).append(view["id"])
        return grouped

    dummy.update_conversation_metadata = update
    dummy.conversations_by_project = by_project
    return dummy


@pytest.fixture
def projects_client(projects_space):
    from fastapi.testclient import TestClient

    from tests.test_web_ui_routes import SAME_SITE, _server

    return TestClient(_server(projects_space).app, headers=SAME_SITE)


def test_create_list_and_patch_a_project(projects_client):
    created = projects_client.post("/api/projects", json={"title": "  Grid  ", "description": "the app"})
    assert created.status_code == 201
    project = created.json()
    assert project["title"] == "Grid" and project["parent_id"] is None and project["id"].startswith("prj-")

    listed = projects_client.get("/api/projects").json()["projects"]
    assert [item["id"] for item in listed] == [project["id"]]
    assert listed[0]["chat_count"] == 0

    child = projects_client.post("/api/projects", json={"title": "Chats", "parent_id": project["id"]}).json()
    assert child["parent_id"] == project["id"]

    patched = projects_client.patch(
        f"/api/projects/{project['id']}", json={"notes": "shared context", "archived": True}
    )
    assert patched.status_code == 200
    assert patched.json()["notes"] == "shared context" and patched.json()["archived"] is True

    assert projects_client.patch("/api/projects/prj-ghost", json={"title": "x"}).status_code == 404


def test_a_bad_parent_a_cycle_and_too_deep_are_refused(projects_client):
    from web_chat.projects import MAX_DEPTH

    assert projects_client.post("/api/projects", json={"title": "orphan", "parent_id": "prj-ghost"}).status_code == 400
    a = projects_client.post("/api/projects", json={"title": "a"}).json()
    b = projects_client.post("/api/projects", json={"title": "b", "parent_id": a["id"]}).json()
    c = projects_client.post("/api/projects", json={"title": "c", "parent_id": b["id"]}).json()
    assert MAX_DEPTH == 3
    assert projects_client.post("/api/projects", json={"title": "d", "parent_id": c["id"]}).status_code == 400
    assert projects_client.patch(f"/api/projects/{a['id']}", json={"parent_id": c["id"]}).status_code == 400
    assert projects_client.patch(f"/api/projects/{a['id']}", json={"parent_id": a["id"]}).status_code == 400


def test_a_chat_is_created_in_a_project_and_listed_with_it(projects_client):
    project = projects_client.post("/api/projects", json={"title": "Grid"}).json()

    created = projects_client.post("/api/chat/conversations", json={"project_id": project["id"]})
    assert created.status_code == 200
    assert created.json()["project_id"] == project["id"]

    rows = projects_client.get("/api/chat/conversations").json()
    assert len(rows) == 1 and rows[0]["project"] == project["id"]
    assert projects_client.get("/api/projects").json()["projects"][0]["chat_count"] == 1


def test_a_chat_cannot_be_created_in_an_unknown_project(projects_client):
    assert projects_client.post("/api/chat/conversations", json={"project_id": "prj-ghost"}).status_code == 404
    assert projects_client.get("/api/chat/conversations").json() == []


def test_a_chat_moves_between_projects_and_can_be_detached(projects_client):
    first = projects_client.post("/api/projects", json={"title": "one"}).json()
    second = projects_client.post("/api/projects", json={"title": "two"}).json()
    context_id = projects_client.post("/api/chat/conversations", json={"project_id": first["id"]}).json()["id"]

    moved = projects_client.put(f"/api/chat/conversations/{context_id}/project", json={"project_id": second["id"]})
    assert moved.status_code == 200 and moved.json()["project_id"] == second["id"]
    counts = {item["id"]: item["chat_count"] for item in projects_client.get("/api/projects").json()["projects"]}
    assert counts == {first["id"]: 0, second["id"]: 1}

    detached = projects_client.put(f"/api/chat/conversations/{context_id}/project", json={"project_id": None})
    assert detached.status_code == 200 and detached.json()["project_id"] is None
    assert projects_client.get("/api/chat/conversations").json()[0]["project"] is None


def test_setting_a_project_on_unknown_targets_is_404(projects_client):
    project = projects_client.post("/api/projects", json={"title": "one"}).json()
    context_id = projects_client.post("/api/chat/conversations", json={}).json()["id"]
    assert (
        projects_client.put(f"/api/chat/conversations/{context_id}/project", json={"project_id": "prj-ghost"}).status_code
        == 404
    )
    assert (
        projects_client.put("/api/chat/conversations/ctx-ghost/project", json={"project_id": project["id"]}).status_code
        == 404
    )


def test_deleting_a_project_reparents_its_chats_and_children(projects_client):
    root = projects_client.post("/api/projects", json={"title": "root"}).json()
    middle = projects_client.post("/api/projects", json={"title": "middle", "parent_id": root["id"]}).json()
    child = projects_client.post("/api/projects", json={"title": "child", "parent_id": middle["id"]}).json()
    context_id = projects_client.post("/api/chat/conversations", json={"project_id": middle["id"]}).json()["id"]

    deleted = projects_client.delete(f"/api/projects/{middle['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "moved_chats": 1, "moved_projects": 1}

    remaining = {item["id"]: item for item in projects_client.get("/api/projects").json()["projects"]}
    assert middle["id"] not in remaining
    assert remaining[child["id"]]["parent_id"] == root["id"]
    assert projects_client.get("/api/chat/conversations").json()[0]["project"] == root["id"]
    assert context_id  # the chat still exists, only its project changed

    assert projects_client.delete("/api/projects/prj-ghost").status_code == 404


@pytest.mark.asyncio
async def test_a_turn_that_changes_files_leaves_a_checkpoint_to_restore(tmp_path):
    from core.checkpoints import Checkpoints
    from web_chat.session import CHECKPOINTS_KEY

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "plan.md").write_text("v1")

    async def run(**kwargs):
        # Any tool: what counts is what the files look like afterwards.
        (workspace / "plan.md").write_text("v2")
        (workspace / "new.py").write_text("x = 1")
        return "Done"

    space, socket = space_for(run), Socket()
    space.checkpoints = Checkpoints(tmp_path / "checkpoints")
    space.conversation_workspace = lambda context_id: workspace
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))
    await socket.incoming.put('{"message":"edit the plan"}')
    event = await socket.event('checkpoint')
    assert event['files'] == 2
    assert sorted(change['path'] for change in event['changes']) == ['new.py', 'plan.md']
    await socket.event('done')

    kept = space.context_manager().get_context_metadata('ctx')[CHECKPOINTS_KEY][event['turn_id']]
    space.checkpoints.restore(workspace, kept['before'])
    assert (workspace / "plan.md").read_text() == "v1" and not (workspace / "new.py").exists()
    await socket.incoming.put(None)
    await session


def turn_with_a_trace():
    """A real AgentTurn over an empty conversation, for its public trace reads."""
    class Session:
        context_id = 'ctx'
        space = SimpleNamespace()

    manager = ContextManager()
    manager.ensure_context('ctx')
    session = Session()
    session.manager = manager
    return AgentTurn(session, 'hello', system_key='s', agent_key='a')


def test_recent_activity_is_a_safe_projection_of_the_turn_trace():
    turn = turn_with_a_trace()
    recorder = turn._recorder
    recorder.note(
        StepKind.REASONING, 'Thinking',
        subtitle='SECRET reasoning subtitle', body='SECRET reasoning body',
    )
    recorder.note(
        StepKind.TOOL, 'Read file',
        detail='SECRET detail',
        input_payload={'path': 'SECRET input'},
        result_payload={'raw_text': 'SECRET result'},
        refs=[
            ContextRef('file', 'a.py', 'project/src/a.py'),
            ContextRef('file', 'passwd', '/etc/passwd'),
            ContextRef('file', 'win', '..\\win.py'),
            ContextRef('file', 'foreign', 'user/notes.md'),
        ],
    )
    recorder.note(StepKind.MESSAGE, 'Narration', subtitle='  I will  read the file ')
    recorder.note(StepKind.PLAN, 'Plan · 1/1', plan={'tasks': [{'title': 'SECRET task'}]})

    activity = turn.recent_activity()

    assert set(activity) == {'items', 'total', 'truncated', 'contexts'}
    assert activity['total'] == 4 and activity['truncated'] is False
    assert activity['contexts'] == [{
        'id': 'root', 'parent_id': None, 'label': 'Main agent',
        'status': 'done', 'at_ms': None, 'duration_ms': None, 'total': 4,
    }]
    kinds = [entry['kind'] for entry in activity['items']]
    assert kinds == ['reasoning', 'tool', 'message', 'plan']
    for entry in activity['items']:
        assert set(entry) == {
            'id', 'kind', 'title', 'status', 'at_ms', 'duration_ms', 'summary', 'files',
            'context_id',
        }
        assert entry['context_id'] == 'root'
    reasoning, tool, message, plan = activity['items']
    assert reasoning['summary'] == ''
    assert tool['summary'] == ''
    assert tool['files'] == ['src/a.py']
    assert message['summary'] == 'I will read the file'
    assert plan['summary'] == ''
    # Not one secret of a body, a payload, a plan or a foreign path leaves.
    assert 'SECRET' not in json.dumps(activity)
    assert '/etc/passwd' not in json.dumps(activity)
    assert 'user/notes.md' not in json.dumps(activity)


def test_recent_activity_is_bounded_to_six_steps_and_four_files():
    turn = turn_with_a_trace()
    recorder = turn._recorder
    for index in range(7):
        recorder.note(StepKind.TOOL, f'Step {index}')
    recorder.note(
        StepKind.TOOL, 'x' * 300,
        refs=[ContextRef('file', f'f{index}.py', f'project/src/f{index}.py') for index in range(6)],
    )

    activity = turn.recent_activity()

    assert activity['total'] == 8
    assert activity['truncated'] is True
    assert [entry['title'] for entry in activity['items']][:-1] == [f'Step {index}' for index in range(2, 7)]
    assert activity['items'][-1]['title'] == 'x' * 120
    assert activity['items'][-1]['files'] == [f'src/f{index}.py' for index in range(4)]


def test_recent_activity_is_empty_without_steps():
    assert turn_with_a_trace().recent_activity() == {
        'items': [], 'total': 0, 'truncated': False, 'contexts': [],
    }


def test_recent_activity_keeps_nested_agent_contexts_and_their_events():
    turn = turn_with_a_trace()
    recorder = turn._recorder
    recorder.note(StepKind.TOOL, 'Root action')
    agent_a = recorder.open(
        StepKind.AGENT, 'Agent A',
        subtitle='SECRET A prompt', body='SECRET A reasoning',
        input_payload={'prompt': 'SECRET A input'},
        result_payload={'report': 'SECRET A report'},
    )
    recorder.note(
        StepKind.TOOL, 'A action', parent_id=agent_a.id,
        detail='SECRET A arguments', result_payload={'raw': 'SECRET A result'},
    )
    recorder.note(
        StepKind.MESSAGE, 'A says', parent_id=agent_a.id,
        subtitle='A public line', body='SECRET A message',
    )
    agent_b = recorder.open(
        StepKind.AGENT, 'Agent B', parent_id=agent_a.id,
        subtitle='SECRET B prompt', body='SECRET B reasoning',
    )
    recorder.note(
        StepKind.TOOL, 'B action', parent_id=agent_b.id,
        input_payload={'query': 'SECRET B input'},
        result_payload={'raw': 'SECRET B result'},
    )
    recorder.close(agent_b)
    recorder.close(agent_a)

    activity = turn.recent_activity()

    contexts = {context['id']: context for context in activity['contexts']}
    assert [context['id'] for context in activity['contexts']] == [
        'root', agent_a.id, agent_b.id,
    ]
    assert (contexts['root']['parent_id'], contexts['root']['label']) == (None, 'Main agent')
    assert contexts['root']['total'] == 2  # its own action and A's delegation row
    assert contexts[agent_a.id]['parent_id'] == 'root'
    assert contexts[agent_a.id]['label'] == 'Agent A'
    assert contexts[agent_a.id]['total'] == 3  # A's action, message and B's delegation
    assert contexts[agent_b.id]['parent_id'] == agent_a.id
    assert contexts[agent_b.id]['label'] == 'Agent B'
    assert contexts[agent_b.id]['total'] == 1
    assert contexts[agent_a.id]['status'] == contexts[agent_b.id]['status'] == 'done'

    owners = {entry['title']: entry['context_id'] for entry in activity['items']}
    assert owners == {
        'Root action': 'root',
        'Agent A': 'root',
        'A action': agent_a.id,
        'A says': agent_a.id,
        'Agent B': agent_a.id,
        'B action': agent_b.id,
    }
    for entry in activity['items']:
        assert set(entry) == {
            'id', 'kind', 'title', 'status', 'at_ms', 'duration_ms', 'summary',
            'files', 'context_id',
        }
    assert activity['items'][3]['summary'] == 'A public line'
    # No prompt, report, reasoning, body, plan or payload may leave a context.
    assert 'SECRET' not in json.dumps(activity)


def test_an_agent_context_survives_even_when_its_events_fell_out_of_the_six():
    turn = turn_with_a_trace()
    recorder = turn._recorder
    for index in range(7):
        recorder.note(StepKind.TOOL, f'Before {index}')
    old_agent = recorder.open(StepKind.AGENT, 'Old agent')
    recorder.note(StepKind.TOOL, 'Old work', parent_id=old_agent.id)
    recorder.close(old_agent)
    for index in range(6):
        recorder.note(StepKind.TOOL, f'After {index}')

    activity = turn.recent_activity()

    assert [entry['title'] for entry in activity['items']] == [
        f'After {index}' for index in range(6)
    ]
    contexts = {context['id']: context for context in activity['contexts']}
    assert old_agent.id in contexts
    assert contexts[old_agent.id]['total'] == 1
    assert contexts['root']['total'] == 14  # 13 direct actions and the delegation row
    assert all(entry['context_id'] == 'root' for entry in activity['items'])


def test_malformed_or_cyclic_agent_parents_fall_back_to_root():
    activity = public_activity([
        {'id': 'a', 'kind': 'agent', 'title': 'A', 'status': 'done', 'parent_id': 'b'},
        {'id': 'b', 'kind': 'agent', 'title': 'B', 'status': 'running', 'parent_id': 'a'},
        {'id': 'c', 'kind': 'agent', 'title': 'C', 'status': 'done', 'parent_id': 'ghost'},
        {'id': 'd', 'kind': 'agent', 'title': 'D', 'status': 'done', 'parent_id': 'd'},
        {'id': '/bad', 'kind': 'agent', 'title': 'Bad', 'status': 'done'},
        {'id': 't1', 'kind': 'tool', 'title': 'Root work', 'status': 'done',
         'parent_id': 'ghost'},
        {'id': 't2', 'kind': 'tool', 'title': 'A work', 'status': 'done',
         'parent_id': 'a'},
    ])

    contexts = {context['id']: context for context in activity['contexts']}
    assert set(contexts) == {'root', 'a', 'b', 'c', 'd'}
    for context_id in ('a', 'b', 'c', 'd'):
        assert contexts[context_id]['parent_id'] == 'root'
    # Every raw step belongs to a context, even one with an unusable id.
    assert contexts['root']['total'] == 6
    assert contexts['a']['total'] == 1
    assert {entry['id'] for entry in activity['items']} == {
        'a', 'b', 'c', 'd', 't1', 't2',
    }
    assert all(
        entry['context_id'] in contexts for entry in activity['items']
    )


# -- a chat's lazy map detail -----------------------------------------------------
#
# The detail route mounts with the map on the real WebChatServer, and it reads
# the real ContextManager: the message and run buckets, not a copy of them.


def test_the_map_detail_route_reads_a_real_chat_through_the_servers_own_app(projects_client, projects_space):
    project = projects_client.post("/api/projects", json={"title": "Grid"}).json()
    manager = projects_space.context_manager()
    manager.ensure_context("chat-1")
    manager.update_context_metadata("chat-1", {"project_id": project["id"], "title": "First chat"})
    manager.add_message("user", "a question " + "x" * 400, context_id="chat-1")
    manager.add_message(
        "assistant",
        "the answer",
        context_id="chat-1",
        metadata={
            "turn_id": "turn-1",
            "type": "agent_response",
            "trace": [
                {"id": "t-1", "kind": "tool", "title": "Read a file", "status": "done",
                 "at_ms": 10, "duration_ms": 2, "body": "SECRET"},
            ],
        },
    )
    execution = AgentExecution(
        agent_name="dev", input_message="SECRET", start_time=1.0, end_time=2.0,
        context_id="chat-1", turn_id="turn-1",
    )
    manager.add_execution(execution, context_id="chat-1")

    response = projects_client.get(f"/api/projects/{project['id']}/map/chats/chat-1")

    assert response.status_code == 200
    body = response.json()
    assert body["chat"] == {
        "id": "chat-1", "title": "First chat", "href": "/?conversation=chat-1",
    }
    assert [item["role"] for item in body["messages"]["items"]] == ["user", "assistant"]
    assert body["messages"]["items"][1]["turn_id"] == "turn-1"
    assert body["executions"]["items"][0]["id"] == "turn:turn-1"
    assert body["operations"]["items"][0]["execution_id"] == "turn:turn-1"
    assert "SECRET" not in response.text
    # A chat of another project is not found through this one.
    assert projects_client.get(f"/api/projects/{project['id']}/map/chats/ghost").status_code == 404


def test_persisting_a_turn_keeps_its_time_and_freezes_open_steps():
    turn = turn_with_a_trace()
    manager = turn._session.manager
    manager.append_message_to('ctx', 'assistant', 'done')
    recorder = turn._recorder
    running = recorder.open(StepKind.AGENT, 'coder')

    turn._persist_trace()

    stored = manager.get_context_messages('ctx')['messages'][-1]['metadata']
    assert stored['duration_ms'] == recorder.elapsed_ms
    trace = {step['id']: step for step in stored['trace']}
    frozen = trace[running.id]
    # Cut off mid-run: the interval is kept, the status stays honest.
    assert frozen['status'] == 'running'
    assert frozen['duration_ms'] is not None
    assert 0 <= frozen['duration_ms'] <= recorder.elapsed_ms


def test_persisting_a_turn_keeps_its_last_context_size():
    turn = turn_with_a_trace()
    manager = turn._session.manager
    manager.append_message_to('ctx', 'assistant', 'done')
    turn._recorder.record_usage(SimpleNamespace(input_tokens=700, output_tokens=50))

    turn._persist_trace()

    stored = manager.get_context_messages('ctx')['messages'][-1]['metadata']
    # History shows the same number the live done frame showed.
    assert stored['context_tokens'] == 700


def test_persisting_a_turn_without_usage_stores_no_context_size():
    turn = turn_with_a_trace()
    manager = turn._session.manager
    manager.append_message_to('ctx', 'assistant', 'done')

    turn._persist_trace()

    stored = manager.get_context_messages('ctx')['messages'][-1]['metadata']
    # Absent, never a zero: the reloaded chat shows nothing.
    assert 'context_tokens' not in stored


def test_a_turns_trace_is_stored_even_after_a_subagent_report():
    turn = turn_with_a_trace()
    manager = turn._session.manager
    manager.append_message_to('ctx', 'assistant', 'the answer', {'turn_id': turn.run_id})
    # A sub-agent report is appended after the answer: it is not this turn's record.
    manager.append_message_to('ctx', 'assistant', 'Tool result of Agent(coder)', {'kind': 'tool_result'})
    turn._recorder.note(StepKind.TOOL, 'Read file')

    turn._persist_trace()

    messages = manager.get_context_messages('ctx')['messages']
    report, answer = messages[-1], messages[-2]
    assert report['metadata'].get('kind') == 'tool_result'
    assert 'trace' not in report['metadata']
    assert answer['metadata']['duration_ms'] == turn._recorder.elapsed_ms
    assert answer['metadata']['trace'][-1]['title'] == 'Read file'


def test_a_trace_never_lands_on_another_turns_answer():
    first = turn_with_a_trace()
    manager = first._session.manager
    second = AgentTurn(first._session, 'again', system_key='s', agent_key='a')
    manager.append_message_to('ctx', 'assistant', 'first answer', {'turn_id': first.run_id})
    manager.append_message_to('ctx', 'assistant', 'second answer', {'turn_id': second.run_id})
    second._recorder.note(StepKind.TOOL, 'Read file')

    second._persist_trace()

    messages = manager.get_context_messages('ctx')['messages']
    assert 'trace' not in messages[-2]['metadata']
    assert 'duration_ms' not in messages[-2]['metadata']
    assert messages[-1]['metadata']['duration_ms'] == second._recorder.elapsed_ms


@pytest.mark.asyncio
async def test_switching_agent_keeps_the_conversation():
    """The picker changes who answers, never the conversation: both turns run
    in the same context, so a switch is a handover of one chat, not a new one."""
    run = AsyncMock(return_value='ok')
    space, socket = space_for(run), Socket()
    session = asyncio.create_task(chat_session(space, socket, 'ctx'))

    await socket.incoming.put(json.dumps({"message": "hello", "agent_key": "worker"}))
    await socket.event('final_output')
    await socket.event('done')
    await socket.incoming.put(json.dumps({"message": "again", "agent_key": "reviewer"}))
    await socket.event('final_output')
    await socket.event('done')
    await socket.incoming.put(None)
    await session

    calls = [call.kwargs for call in run.await_args_list]
    assert [call['agent_key'] for call in calls] == ['worker', 'reviewer']
    assert [call['context_id'] for call in calls] == ['ctx', 'ctx']


# -- the turn is visible from the moment its message is accepted -------------


@pytest.mark.asyncio
async def test_the_turn_is_registered_while_its_preamble_still_runs():
    """A GET from any tab sees the active turn while the preamble still runs.

    The claim, the registered task and the message precede the revision
    check, recovery and admission (web_chat.session._run_turn_preamble):
    a page that collapses and returns during that window does not lose its
    message to an empty active_turn, and no second turn claims the race.
    """
    in_preamble, preamble_done = asyncio.Event(), asyncio.Event()

    async def slow_revision_check(self, system_key):
        in_preamble.set()
        await preamble_done.wait()
        return []

    space, socket = space_for(AsyncMock()), Socket()
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    with patch.object(ChatSession, "_check_revision", slow_revision_check):
        await socket.incoming.put('{"message":"hello"}')
        await asyncio.wait_for(in_preamble.wait(), 2)
        active = space.turns.running("ctx")
        assert active is not None
        assert active[0].message == "hello"
        assert space.turns.is_claimed("ctx")
        preamble_done.set()
    await wait_for(lambda: not space.turns.is_claimed("ctx"))
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_refused_preamble_releases_the_conversation():
    """A turn the preamble refuses is unregistered and released at once."""
    space, socket = space_for(AsyncMock()), Socket()
    space.admit_turn = lambda turn=None: "The day's turn limit is reached"
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put('{"message":"hello"}')
    first = await asyncio.wait_for(socket.outgoing.get(), 2)
    second = await asyncio.wait_for(socket.outgoing.get(), 2)
    assert first == {"type": "error", "content": "The day's turn limit is reached"}
    assert second == {"type": "done"}
    # The refusal released the claim and the record: the conversation is not
    # left busy by a turn that never ran.
    await wait_for(lambda: not space.turns.is_claimed("ctx"))
    assert space.turns.running("ctx") is None
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_a_long_turn_never_loses_its_public_words():
    """The replay cap trims bookkeeping frames, never the agent's own words."""
    space, socket = space_for(AsyncMock()), Socket()
    session = ChatSession(space, socket, "ctx")
    turn = AgentTurn(session, "hello", system_key=None, agent_key=None, images=(), images_saved=False)
    turn._remember_replay({"type": "event", "event": {"kind": "message", "text": "said first"}})
    for index in range(MAX_REPLAY_NOTES + 20):
        turn._remember_replay({"type": "image", "content": f"image-{index}"})
    turn._remember_replay({"type": "event", "event": {"kind": "message", "text": "said last"}})
    frames = turn._snapshot_frames()
    # say_public words - and any interface the agent presented - survive the
    # churn of technical frames however long the turn runs: a page that comes
    # back late replays every one of them, in order.
    said = [frame["event"]["text"] for frame in frames if frame["type"] == "event"]
    assert said == ["said first", "said last"]
    assert len([frame for frame in frames if frame["type"] == "image"]) == MAX_REPLAY_NOTES
