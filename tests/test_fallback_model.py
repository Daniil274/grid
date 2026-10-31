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
        self.last_kwargs = {}

    async def get_response(self, *args, **kwargs):
        settings = args[2] if len(args) >= 3 else kwargs["model_settings"]
        self.response_settings.append(settings)
        self.last_kwargs = kwargs
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
async def test_stream_discards_initial_lifecycle_events_before_fallback():
    primary = FakeModel(events=[SimpleNamespace(type="response.created")], stream_error=RuntimeError("connection reset by peer"))
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


async def test_stream_delivers_content_before_provider_finishes():
    import asyncio
    release = asyncio.Event()

    class SlowModel(FakeModel):
        async def stream_response(self, *args, **kwargs):
            yield "first token"
            await release.wait()
            yield "last token"

    model = FallbackModel([_candidate("primary", SlowModel())])
    stream = model.stream_response(model_settings=None)
    try:
        assert await asyncio.wait_for(anext(stream), 1) == "first token"
        release.set()
        assert await anext(stream) == "last token"
    finally:
        await stream.aclose()


async def test_failure_after_content_does_not_mix_candidates():
    backup = FakeModel(events=["replacement"])
    model = FallbackModel([
        _candidate("primary", FakeModel(events=["partial"], stream_error=RuntimeError("broken"))),
        _candidate("backup", backup),
    ])
    stream = model.stream_response(model_settings=None)
    assert await anext(stream) == "partial"
    with pytest.raises(RuntimeError, match="broken"):
        await anext(stream)
    assert not backup.stream_settings


@pytest.mark.asyncio
async def test_all_models_failed_reports_each_candidate():
    model = FallbackModel([
        _candidate("primary", FakeModel(response_error=RuntimeError("rate limit exceeded"))),
        _candidate("backup", FakeModel(response_error=RuntimeError("upstream unavailable"))),
    ])

    with pytest.raises(AllModelsFailedError, match="primary.*backup"):
        await model.get_response(None, "input", SimpleNamespace(key="outer"))


@pytest.mark.asyncio
async def test_a_credential_refused_while_signing_is_reported_as_itself_not_as_a_connection_error():
    from openai import APIConnectionError

    from core.credentials import CredentialError, credential_error_in

    def connection_error(reason):
        # What the OpenAI SDK raises when httpx's auth flow raised CredentialError.
        try:
            try:
                raise CredentialError(reason)
            except CredentialError as cause:
                raise APIConnectionError(request=None) from cause
        except APIConnectionError as error:
            return error

    first = FakeModel(response_error=connection_error("Your opencode-go credential cannot be used right now. Reconnect it in the settings."))
    second = FakeModel(response_error=connection_error("Sign in with ChatGPT in the settings to use it."))
    model = FallbackModel([_candidate("glm-flash", first), _candidate("gpt-6-luna", second)])

    with pytest.raises(AllModelsFailedError) as failed:
        await model.get_response(None, "input", SimpleNamespace(key="outer"))

    assert "Connection error" not in str(failed.value)
    assert "glm-flash: CredentialError: Your opencode-go credential" in str(failed.value)
    assert "gpt-6-luna: CredentialError: Sign in with ChatGPT" in str(failed.value)
    assert credential_error_in(RuntimeError("plain")) is None


# -- only technical failures may move a request to another model -----------------

def test_policy_refusal_is_never_a_fallback_reason():
    from core.fallback_model import can_fallback

    for error in (
        RuntimeError("policy_deny: write to outside the workspace"),
        RuntimeError("Action declined by the user"),
        RuntimeError("content_filter triggered: safety"),
        RuntimeError("You are not allowed to run this command"),
        # An unrecognised error fails closed: it may be a refusal, not an outage.
        RuntimeError("unexpected parser state"),
    ):
        assert can_fallback(error) is False, str(error)


def test_technical_unavailability_is_a_fallback_reason():
    import asyncio

    import httpx
    from openai import APIConnectionError, APIStatusError, RateLimitError

    from core.fallback_model import can_fallback

    def response(code):
        return httpx.Response(code, request=httpx.Request("POST", "https://example.test/v1"))

    assert can_fallback(APIStatusError("x", response=response(429), body=None)) is True
    assert can_fallback(APIStatusError("x", response=response(503), body=None)) is True
    assert can_fallback(APIStatusError("x", response=response(400), body=None)) is False
    assert can_fallback(asyncio.TimeoutError()) is True
    assert can_fallback(httpx.ConnectError("connection error")) is True
    assert can_fallback(APIConnectionError(request=None)) is True
    # CLI backends report quota exhaustion as text.
    assert can_fallback(RuntimeError("You've hit your limit \u00b7 resets 3pm")) is True
    assert can_fallback(RuntimeError("usage limit reached for this subscription")) is True
    assert can_fallback(RateLimitError("x", response=response(429), body=None)) is True
    assert can_fallback(RuntimeError("The Claude Code CLI exited with status 1.")) is True
    assert can_fallback(RuntimeError("Claude Code CLI 'claude' was not found on PATH.")) is True


@pytest.mark.asyncio
async def test_a_policy_refusal_stops_the_chain_and_the_backup_is_not_called():
    backup = FakeModel(response="would leak past the policy")
    model = FallbackModel([
        _candidate("primary", FakeModel(response_error=RuntimeError("policy_deny: rm -rf"))),
        _candidate("backup", backup),
    ])

    with pytest.raises(RuntimeError, match="policy_deny"):
        await model.get_response(None, "input", SimpleNamespace(key="outer"))

    assert backup.response_settings == []


@pytest.mark.asyncio
async def test_a_policy_refusal_in_a_stream_is_propagated_not_retried():
    backup = FakeModel(events=["replacement"])
    model = FallbackModel([
        _candidate("primary", FakeModel(events=[SimpleNamespace(type="response.created")],
                                        stream_error=RuntimeError("policy_filter: blocked by the chat filter"))),
        _candidate("backup", backup),
    ])

    stream = model.stream_response(model_settings=None)
    with pytest.raises(RuntimeError, match="policy_filter"):
        await anext(stream)
    assert backup.stream_settings == []


async def _stream_events(model, **kwargs):
    return [event async for event in model.stream_response(model_settings=None, **kwargs)]


@pytest.mark.asyncio
async def test_a_quota_limit_moves_to_the_next_model_with_the_same_request():
    primary = FakeModel(response_error=RuntimeError("rate limit exceeded: try again later"))
    backup = FakeModel(response="ok")
    model = FallbackModel([
        _candidate("opus-5-5", primary),
        _candidate("gpt-6-luna", backup),
    ])
    tools = [SimpleNamespace(name="bash_tool")]
    settings = SimpleNamespace(key="outer", parallel_tool_calls=True)

    result = await model.get_response(
        SimpleNamespace(model="opus-5-5"),
        [{"role": "user", "content": "work"}],
        settings,
        tools=tools,
        previous_response_id="resp-1",
        conversation_id="chat-of-tenant-a",
    )

    assert result == "ok"
    # Each model gets its own settings, as the SDK expects.
    assert [s.key for s in primary.response_settings] == ["opus-5-5"]
    # Same tools, tenant context and response chain: only the model changed.
    assert backup.last_kwargs["tools"] is tools
    assert backup.last_kwargs["previous_response_id"] == "resp-1"
    assert backup.last_kwargs["conversation_id"] == "chat-of-tenant-a"
    assert [s.key for s in backup.response_settings] == ["gpt-6-luna"]


@pytest.mark.asyncio
async def test_a_provider_failure_event_is_retried_and_its_code_is_reported():
    from types import SimpleNamespace as NS

    failed = NS(type="response.failed", response=NS(error=NS(code="server_error", message="upstream unavailable")))
    backup = FakeModel(events=["complete"])
    model = FallbackModel([
        _candidate("primary", FakeModel(events=[failed])),
        _candidate("backup", backup),
    ])

    assert await _stream_events(model) == ["complete"]
    assert backup.stream_settings


@pytest.mark.asyncio
async def test_a_refusal_reported_as_a_failure_event_is_not_retried():
    from types import SimpleNamespace as NS

    refused = NS(type="response.failed", response=NS(error=NS(code="content_policy", message="content_filter")))
    backup = FakeModel(events=["replacement"])
    model = FallbackModel([
        _candidate("primary", FakeModel(events=[refused])),
        _candidate("backup", backup),
    ])

    with pytest.raises(Exception, match="content_policy"):
        await _stream_events(model)
    assert backup.stream_settings == []


@pytest.mark.asyncio
async def test_a_non_technical_error_is_final_even_if_the_primary_is_down():
    backup = FakeModel(response="ok")
    model = FallbackModel([
        _candidate("primary", FakeModel(response_error=ValueError("malformed tool call from the caller"))),
        _candidate("backup", backup),
    ])
    with pytest.raises(ValueError):
        await model.get_response(None, "input", SimpleNamespace(key="outer"))
    assert backup.response_settings == []


@pytest.mark.asyncio
async def test_a_shortened_chain_names_the_models_it_never_tried():
    """The failure of the last candidate must not read as "this model is broken"
    when the chain was already cut: a model skipped for want of a key is the
    reason the run died, and the person cannot fix what is not named."""
    only = FakeModel(response_error=RuntimeError(
        "The ChatGPT user has reached their Subscription Sharing usage limit."
    ))
    model = FallbackModel(
        [_candidate("gpt-6-luna", only)],
        skipped=[
            ("kimi-k3.1-pro", "model 'kimi-k3.1-pro' has no usable credential (provider 'kimi')"),
            ("deepseek-flash-latest", "not in this user's plan"),
        ],
    )

    with pytest.raises(AllModelsFailedError) as failed:
        await model.get_response(None, "input", SimpleNamespace(key="outer"))

    message = str(failed.value)
    assert "Subscription Sharing usage limit" in message
    assert "Not tried: kimi-k3.1-pro" in message and "MOONSHOT" not in message
    assert "deepseek-flash-latest (not in this user's plan)" in message


@pytest.mark.asyncio
async def test_one_failing_model_and_nothing_skipped_keeps_the_provider_error_as_is():
    model = FallbackModel([_candidate("primary", FakeModel(response_error=RuntimeError("rate limit exceeded")))])

    with pytest.raises(RuntimeError, match="rate limit exceeded"):
        await model.get_response(None, "input", SimpleNamespace(key="outer"))


def test_a_status_the_backend_names_in_words_is_an_outage_not_a_refusal():
    """A backend may carry its status in the text instead of the exception type;
    the chain must move on for a 5xx or 429 whichever way it is reported, while
    a policy refusal stays fail-closed."""
    from core.fallback_model import can_fallback

    assert can_fallback(RuntimeError("Upstream 503"))
    assert can_fallback(RuntimeError("Error code: 429 - too many requests"))
    assert not can_fallback(RuntimeError("policy_deny: write to outside the workspace"))
