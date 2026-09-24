from types import SimpleNamespace

import pytest

from core.fallback_model import (
    AllModelsFailedError,
    FallbackModel,
    ModelCandidate,
)


class FakeModel:
    def __init__(self, *, response=None, response_error=None, events=(), stream_error=None):
        self.response = response
        self.response_error = response_error
        self.events = list(events)
        self.stream_error = stream_error
        self.response_settings = []
        self.stream_settings = []

    async def get_response(self, *args, **kwargs):
        settings = args[2] if len(args) >= 3 else kwargs["model_settings"]
        self.response_settings.append(settings)
        if self.response_error:
            raise self.response_error
        return self.response

    async def stream_response(self, *args, **kwargs):
        settings = args[2] if len(args) >= 3 else kwargs["model_settings"]
        self.stream_settings.append(settings)
        for event in self.events:
            yield event
        if self.stream_error:
            raise self.stream_error


def _candidate(key, model):
    return ModelCandidate(key=key, model=model, settings=SimpleNamespace(key=key))


@pytest.mark.asyncio
async def test_get_response_uses_models_left_to_right_and_their_settings():
    primary = FakeModel(response_error=RuntimeError("primary unavailable"))
    backup = FakeModel(response="ok")
    model = FallbackModel([
        _candidate("primary", primary),
        _candidate("backup", backup),
    ])

    result = await model.get_response(None, "input", SimpleNamespace(key="outer"))

    assert result == "ok"
    assert [settings.key for settings in primary.response_settings] == ["primary"]
    assert [settings.key for settings in backup.response_settings] == ["backup"]


@pytest.mark.asyncio
async def test_stream_discards_failed_partial_events_before_fallback():
    primary = FakeModel(events=["partial"], stream_error=RuntimeError("stream failed"))
    backup = FakeModel(events=["complete", "done"])
    model = FallbackModel([
        _candidate("primary", primary),
        _candidate("backup", backup),
    ])

    events = [event async for event in model.stream_response(
        system_instructions=None,
        input="input",
        model_settings=SimpleNamespace(key="outer"),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=None,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )]

    assert events == ["complete", "done"]
    assert [settings.key for settings in primary.stream_settings] == ["primary"]
    assert [settings.key for settings in backup.stream_settings] == ["backup"]


@pytest.mark.asyncio
async def test_all_models_failed_reports_each_candidate():
    model = FallbackModel([
        _candidate("primary", FakeModel(response_error=RuntimeError("limited"))),
        _candidate("backup", FakeModel(response_error=RuntimeError("offline"))),
    ])

    with pytest.raises(AllModelsFailedError, match="primary.*backup"):
        await model.get_response(None, "input", SimpleNamespace(key="outer"))
