"""Commands must remain responsive while agent inference is suspended."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

from web_chat.session import chat_session


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


def server_for(run):
    """A server whose single system 's' holds one agent 'a' running *run*."""
    manager = SimpleNamespace(_lock=threading.RLock(), _contexts={'ctx': {'conversation': []}}, persist_path=None)
    factory = SimpleNamespace(run_agent=run)

    async def resolve_turn(message, **kwargs):
        return SimpleNamespace(
            system=kwargs.get('system_key') or 's', agent=kwargs.get('agent_key') or 'a',
            config=None, factory=factory, routed_system=False,
            routed_agent=kwargs.get('agent_key') is None, routed=True, warning='',
        )

    runtime = SimpleNamespace(
        context_manager=lambda: manager, workspace_path='.', user_id='test',
        factory=factory, warm_agent=AsyncMock(), resolve_turn=resolve_turn,
        update_conversation_metadata=lambda *a, **kw: None,
    )
    return SimpleNamespace(
        runtime=runtime, _active_chat_contexts=set(), _chat_turns={},
        agent_label=lambda system, agent: 'Agent',
        selection_is_valid=lambda system, agent: True,
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

    server, socket = server_for(run), Socket()
    session = asyncio.create_task(chat_session(server, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"action":"stop"}')
    assert (await socket.event('done'))['stopped'] is True
    assert cancelled.is_set()
    assert not server._active_chat_contexts
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_stop_during_agent_warmup_and_next_turn():
    run = AsyncMock(return_value='Ready')
    server, socket = server_for(run), Socket()
    started = asyncio.Event()

    async def warm(agent_key, system_key=None):
        started.set()
        await asyncio.Event().wait()

    server.runtime.warm_agent = warm
    session = asyncio.create_task(chat_session(server, socket, 'ctx'))
    await socket.incoming.put('{"message":"first"}')
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put('{"action":"stop"}')
    first = await socket.event('done')
    assert first['stopped'] is True
    run.assert_not_awaited()
    server.runtime.warm_agent = AsyncMock()
    await socket.incoming.put('{"message":"second"}')
    assert (await socket.event('final_output'))['content'] == 'Ready'
    second = await socket.event('done')
    assert second['run_id'] != first['run_id']
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_error_is_terminal_and_disconnect_cleans_up():
    server, socket = server_for(AsyncMock(side_effect=ValueError('failed'))), Socket()
    session = asyncio.create_task(chat_session(server, socket, 'ctx'))
    await socket.incoming.put('{"message":"hello"}')
    assert (await socket.event('error'))['content'] == 'failed'
    await socket.event('done')
    assert not server._active_chat_contexts
    await socket.incoming.put(None)
    await session


@pytest.mark.asyncio
async def test_second_socket_cannot_overlap_a_running_turn():
    started, release = asyncio.Event(), asyncio.Event()

    async def run(**kwargs):
        started.set()
        await release.wait()
        return 'done'

    server = server_for(run)
    first, second = Socket(), Socket()
    tasks = [asyncio.create_task(chat_session(server, s, 'ctx')) for s in (first, second)]
    await first.incoming.put('{"message":"first"}')
    await asyncio.wait_for(started.wait(), 2)
    await second.incoming.put('{"message":"second"}')
    await second.event('busy')
    release.set()
    await first.event('done')
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

    server = server_for(run)
    first = Socket()
    first_task = asyncio.create_task(chat_session(server, first, 'ctx'))
    await first.incoming.put('{"message":"work"}')
    await asyncio.wait_for(started.wait(), 2)
    await first.incoming.put(None)  # the page reloads
    await first_task
    await asyncio.sleep(0)
    assert not cancelled.is_set()
    assert 'ctx' in server._chat_turns

    second = Socket()
    second_task = asyncio.create_task(chat_session(server, second, 'ctx'))
    await second.incoming.put('{"action":"attach"}')
    attached = await second.event('attached')
    assert attached['message'] == 'work'
    replayed = await second.event('step')
    assert replayed['step']['kind'] in {'routing', 'prepare', 'tool'}

    release.set()
    assert (await second.event('final_output'))['content'] == 'Finished.'
    await second.event('done')
    await asyncio.sleep(0)
    assert 'ctx' not in server._chat_turns
    await second.incoming.put(None)
    await second_task


@pytest.mark.asyncio
async def test_attach_without_a_running_turn_reports_it_is_over():
    server, socket = server_for(AsyncMock(return_value='x')), Socket()
    session = asyncio.create_task(chat_session(server, socket, 'ctx'))
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

    server = server_for(run)
    first, second = Socket(), Socket()
    first_task = asyncio.create_task(chat_session(server, first, 'ctx'))
    await first.incoming.put('{"message":"work"}')
    await asyncio.wait_for(started.wait(), 2)
    await first.incoming.put(None)
    await first_task

    second_task = asyncio.create_task(chat_session(server, second, 'ctx'))
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

    server, socket = server_for(run), Socket()
    session = asyncio.create_task(chat_session(server, socket, 'ctx'))
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
    assert tool_step['title'] == 'read_file'
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

    server, socket = server_for(run), Socket()
    session = asyncio.create_task(chat_session(server, socket, 'ctx'))
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
