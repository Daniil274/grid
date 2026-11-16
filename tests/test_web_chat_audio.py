"""End-to-end websocket coverage for browser audio ingress."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

try:
    from test_web_chat_session import Socket, space_for
except ModuleNotFoundError:
    from tests.test_web_chat_session import Socket, space_for
from web_chat.audio_ingress import AudioAttachment
from web_chat.session import chat_session

# RIFF/WAVE PCM header accepted by the production signature validator.
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"


def audio_url():
    return AudioAttachment(WAV, "audio/wav").data_url


async def start_turn(space, *, system="s", agent="a", audio=None):
    socket = Socket()
    task = asyncio.create_task(chat_session(space, socket, "ctx"))
    payload = {"message": "caption", "system_key": system, "agent_key": agent}
    if audio is not None:
        payload["audio"] = audio
    await socket.incoming.put(json.dumps(payload))
    return socket, task


async def finish(socket, task):
    await socket.event("done")
    await socket.incoming.put(None)
    await task


@pytest.mark.asyncio
async def test_explicit_audio_native_runtime_skips_asr(monkeypatch):
    run = AsyncMock(return_value="heard")
    space = space_for(run)
    space.factory.create_agent = AsyncMock(return_value=object())
    space.deployment.voice_service = SimpleNamespace(transcribe=AsyncMock())
    monkeypatch.setattr("web_chat.session.runtime_accepts_audio", lambda runtime, audio: True)
    socket, task = await start_turn(space, audio=audio_url())
    assert (await socket.event("final_output"))["content"] == "heard"
    run.assert_awaited_once()
    assert "data:audio/wav;base64," in run.await_args.kwargs["message"]
    space.deployment.voice_service.transcribe.assert_not_awaited()
    await finish(socket, task)


@pytest.mark.asyncio
async def test_explicit_text_runtime_transcribes_once(monkeypatch):
    run = AsyncMock(return_value="ok")
    space = space_for(run)
    space.factory.create_agent = AsyncMock(return_value=object())
    transcribe = AsyncMock(return_value={"text": "spoken words"})
    space.deployment.voice_service = SimpleNamespace(transcribe=transcribe)
    monkeypatch.setattr("web_chat.session.runtime_accepts_audio", lambda runtime, audio: False)
    socket, task = await start_turn(space, audio=audio_url())
    await socket.event("final_output")
    transcribe.assert_awaited_once_with(WAV)
    assert run.await_args.kwargs["message"] == "caption\n\nspoken words"
    await finish(socket, task)


@pytest.mark.asyncio
async def test_auto_route_transcribes_before_resolution(monkeypatch):
    run = AsyncMock(return_value="ok")
    space = space_for(run)
    space.factory.create_agent = AsyncMock(return_value=object())
    transcribe = AsyncMock(return_value={"text": "route words"})
    space.deployment.voice_service = SimpleNamespace(transcribe=transcribe)
    monkeypatch.setattr("web_chat.session.runtime_accepts_audio", lambda runtime, audio: False)
    original = space.resolve_turn
    order = []
    async def resolve(message, **kwargs):
        order.append(("resolve", message))
        return await original(message, **kwargs)
    async def speech(*args):
        order.append(("asr", None))
        return await __import__("web_chat.audio_ingress", fromlist=["transcribe_audio"]).transcribe_audio(*args)
    monkeypatch.setattr("web_chat.session.transcribe_audio", speech)
    space.resolve_turn = resolve
    socket, task = await start_turn(space, system=None, agent=None, audio=audio_url())
    await socket.event("final_output")
    assert [item[0] for item in order] == ["asr", "resolve"]
    assert order[1][1] == "caption\n\nroute words"
    transcribe.assert_awaited_once()
    await finish(socket, task)


@pytest.mark.asyncio
async def test_missing_asr_errors_without_running_agent(monkeypatch):
    run = AsyncMock(return_value="should not happen")
    space = space_for(run)
    space.factory.create_agent = AsyncMock(return_value=object())
    space.deployment.voice_service = None
    monkeypatch.setattr("web_chat.session.runtime_accepts_audio", lambda runtime, audio: False)
    socket, task = await start_turn(space, audio=audio_url())
    error = await socket.event("error")
    assert "speech recognition is unavailable" in error["content"]
    run.assert_not_awaited()
    await finish(socket, task)


@pytest.mark.asyncio
async def test_invalid_mime_is_rejected_without_running_agent():
    run = AsyncMock(return_value="should not happen")
    space = space_for(run)
    socket, task = await start_turn(space, audio="data:audio/not-real;base64,AAAA")
    error = await socket.event("error")
    assert "Unsupported audio content type" in error["content"]
    run.assert_not_awaited()
    await finish(socket, task)


@pytest.mark.asyncio
async def test_busy_audio_refusal_preserves_current_run(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def run(**kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return "first finished"
    space = space_for(run)
    space.factory.create_agent = AsyncMock(return_value=object())
    monkeypatch.setattr("web_chat.session.runtime_accepts_audio", lambda runtime, audio: True)
    first, task = await start_turn(space, audio=audio_url())
    await asyncio.wait_for(started.wait(), 2)
    # A competing audio submission is refused while the first turn owns ctx.
    second, second_task = await start_turn(space, audio=audio_url())
    error = await second.event("busy")
    assert "busy" in error["content"].lower() or "running" in error["content"].lower()
    assert not task.done()
    release.set()
    assert (await first.event("final_output"))["content"] == "first finished"
    await finish(first, task)
    await second.incoming.put(None)
    await second_task
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_explicit_native_selection_exercises_real_fallback_candidates(monkeypatch):
    from core.fallback_model import FallbackModel, ModelCandidate
    from core.vision_model import VisionChatCompletionsModel

    class CandidateModel(VisionChatCompletionsModel):
        async def get_response(self, *args, **kwargs):
            return "response"
    candidate = ModelCandidate("vision", CandidateModel("model", SimpleNamespace(base_url=SimpleNamespace(host="openrouter.ai"))), {}, modalities=frozenset({"audio"}))
    runtime = SimpleNamespace(model=FallbackModel([candidate]))
    run = AsyncMock(return_value="ok")
    space = space_for(run)
    space.factory.create_agent = AsyncMock(return_value=runtime)
    space.deployment.voice_service = SimpleNamespace(transcribe=AsyncMock())
    socket, task = await start_turn(space, audio=audio_url())
    await socket.event("final_output")
    space.deployment.voice_service.transcribe.assert_not_awaited()
    assert "data:audio/wav;base64," in run.await_args.kwargs["message"]
    await finish(socket, task)


@pytest.mark.asyncio
async def test_explicit_text_agent_transcribes_once(monkeypatch):
    run = AsyncMock(return_value="ok")
    space = space_for(run)
    space.factory.create_agent = AsyncMock(return_value=object())
    transcribe = AsyncMock(return_value={"text": "speech"})
    space.deployment.voice_service = SimpleNamespace(transcribe=transcribe)
    monkeypatch.setattr("web_chat.session.runtime_accepts_audio", lambda runtime, audio: False)
    socket, task = await start_turn(space, audio=audio_url())
    await socket.event("final_output")
    assert run.await_args.kwargs["message"] == "caption\n\nspeech"
    transcribe.assert_awaited_once()
    await finish(socket, task)
