from types import SimpleNamespace

import pytest

from core.fallback_model import (
    AllModelsFailedError,
    FallbackModel,
    ModelCandidate,
    ModelSelection,
    model_selection,
    selected_model_key,
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


# -- a refused route moves on; a refused request does not ----------------------

def _status_error(cls, code, *, message="Failed", body=None):
    import httpx

    return cls(
        message,
        response=httpx.Response(code, request=httpx.Request("POST", "https://api.example.test/v1")),
        body=body,
    )


def test_a_typed_route_status_is_a_fallback_reason():
    """401/402/403/404 name the route - this key, account or model - not the
    request: another provider may still serve it."""
    from openai import (
        APIStatusError,
        AuthenticationError,
        NotFoundError,
        PermissionDeniedError,
    )

    from core.fallback_model import can_fallback

    for error in (
        _status_error(AuthenticationError, 401),
        _status_error(APIStatusError, 402),
        _status_error(PermissionDeniedError, 403, message="permission denied"),
        _status_error(NotFoundError, 404),
    ):
        assert can_fallback(error) is True, error

    # A payment refusal is a dead route, not the person declining an action.
    assert can_fallback(_status_error(APIStatusError, 402, message="card declined")) is True


def test_a_typed_route_status_does_not_excuse_a_policy_refusal():
    """A provider that blocks the request itself says so in the body; the status
    it chose (403, 400) must not turn a refusal into a fallback."""
    from openai import BadRequestError, PermissionDeniedError

    from core.fallback_model import can_fallback

    blocked = {"error": {"type": "content_policy_violation", "message": "blocked by the safety system"}}
    refused = {"error": {"type": "invalid_request_error", "message": "content_filter triggered"}}

    assert can_fallback(_status_error(PermissionDeniedError, 403, body=blocked)) is False
    assert can_fallback(_status_error(BadRequestError, 400, body=refused)) is False


@pytest.mark.asyncio
async def test_a_refused_route_moves_the_request_to_the_reserve():
    """A revoked key (401), an unpaid account (402), a plan without the route
    (403) and a model that is not there (404) are that route's problem, not the
    request's: the chain must still run on the model behind it."""
    from openai import (
        APIStatusError,
        AuthenticationError,
        NotFoundError,
        PermissionDeniedError,
    )

    for error in (
        _status_error(AuthenticationError, 401),
        _status_error(APIStatusError, 402),
        _status_error(PermissionDeniedError, 403),
        _status_error(NotFoundError, 404),
    ):
        backup = FakeModel(response="reserve answered")
        model = FallbackModel([
            _candidate("kimi-k3.1-pro", FakeModel(response_error=error)),
            _candidate("gpt-6-luna", backup),
        ])

        assert await model.get_response(None, "input", SimpleNamespace(key="outer")) == "reserve answered"
        assert backup.response_settings


@pytest.mark.asyncio
async def test_a_refused_request_with_a_route_status_does_not_reach_the_reserve():
    from openai import PermissionDeniedError

    blocked = {"error": {"type": "content_policy_violation", "message": "blocked by the safety system"}}
    backup = FakeModel(response="must not answer")
    model = FallbackModel([
        _candidate("primary", FakeModel(response_error=_status_error(PermissionDeniedError, 403, body=blocked))),
        _candidate("backup", backup),
    ])

    with pytest.raises(PermissionDeniedError):
        await model.get_response(None, "input", SimpleNamespace(key="outer"))
    assert backup.response_settings == []


@pytest.mark.asyncio
async def test_a_failure_event_without_a_reason_falls_back_but_an_incomplete_does_not():
    """An unexplained ``response.failed`` is an outage of that backend; a
    ``response.incomplete`` is this model's own cut-off and stays final."""
    from types import SimpleNamespace as NS

    failed = NS(type="response.failed")
    backup = FakeModel(events=["complete"])
    model = FallbackModel([
        _candidate("primary", FakeModel(events=[failed])),
        _candidate("backup", backup),
    ])

    assert await _stream_events(model) == ["complete"]
    assert backup.stream_settings

    cut_short = NS(type="response.incomplete", response=NS(
        error=None, incomplete_details=NS(reason="max_output_tokens")
    ))
    backup = FakeModel(events=["replacement"])
    model = FallbackModel([
        _candidate("primary", FakeModel(events=[cut_short])),
        _candidate("backup", backup),
    ])

    with pytest.raises(Exception, match="max_output_tokens"):
        await _stream_events(model)
    assert backup.stream_settings == []


@pytest.mark.asyncio
async def test_a_shortened_chain_keeps_the_retry_of_a_transient_failure():
    """The wrapped error still names the models that were skipped, and the one
    model that failed keeps its own verdict: a 429 is worth another attempt."""
    from core.factory.failures import is_retriable

    transient = FakeModel(response_error=RuntimeError("rate limit exceeded"))
    model = FallbackModel(
        [_candidate("gpt-6-luna", transient)],
        skipped=[("kimi-k3.1-pro", "model 'kimi-k3.1-pro' has no usable credential")],
    )

    with pytest.raises(AllModelsFailedError) as failed:
        await model.get_response(None, "input", SimpleNamespace(key="outer"))

    assert "Not tried: kimi-k3.1-pro" in str(failed.value)
    assert is_retriable(failed.value) is True


@pytest.mark.asyncio
async def test_a_shortened_chain_does_not_retry_a_spent_account_or_a_refused_key():
    """Retrying does not clear a spent account or a credential the user has to
    fix, so those stay final even when the chain was shortened."""
    import httpx
    from openai import RateLimitError

    from core.credentials import CredentialError
    from core.factory.failures import is_retriable

    spent = RateLimitError(
        "Error code: 429 - {'error': {'type': 'exceeded_current_quota_error', "
        "'message': 'Your account is suspended due to insufficient balance, please recharge'}}",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.example.test/v1")),
        body=None,
    )
    revoked = CredentialError("Your kimi-code credential cannot be used right now. Reconnect it in the settings.")

    for error in (spent, revoked):
        model = FallbackModel(
            [_candidate("gpt-6-luna", FakeModel(response_error=error))],
            skipped=[("kimi-k3.1-pro", "model 'kimi-k3.1-pro' has no usable credential")],
        )
        with pytest.raises(AllModelsFailedError) as failed:
            await model.get_response(None, "input", SimpleNamespace(key="outer"))
        assert is_retriable(failed.value) is False, error


def test_the_wrapped_chain_failure_keeps_the_single_models_verdict():
    """is_retriable reads the one failure behind a shortened chain; a chain of
    several failures has no single verdict and stays final."""
    import httpx
    from openai import RateLimitError

    from core.credentials import CredentialError
    from core.factory.failures import is_retriable

    transient = RateLimitError(
        "rate limit reached for requests",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.example.test/v1")),
        body=None,
    )

    assert is_retriable(AllModelsFailedError("all failed. Not tried: kimi", cause=transient)) is True
    assert is_retriable(AllModelsFailedError("all failed. Not tried: kimi", cause=CredentialError("no key"))) is False
    assert is_retriable(AllModelsFailedError("all failed")) is False


# -- reporting the model that actually answers -----------------------------
#
# The web chat shows the model of the turn, including one reached by fallback.
# The fact is transient: it goes into a ModelSelection bound for the turn, not
# into the response text or anything stored.


@pytest.mark.asyncio
async def test_a_live_stream_reports_the_model_that_answers():
    primary = FakeModel(events=["first token", "last token"])
    backup = FakeModel(events=["replacement"])
    model = FallbackModel([
        _candidate("primary", primary),
        _candidate("backup", backup),
    ])

    seen = ModelSelection()
    keys = []
    with model_selection(seen):
        async for _event in model.stream_response(model_settings=None):
            keys.append(selected_model_key())

    assert keys == ["primary", "primary"]
    assert seen.key == "primary"
    assert not backup.stream_settings


@pytest.mark.asyncio
async def test_a_fallback_that_answers_reports_its_own_model_not_the_failed_one():
    primary = FakeModel(
        events=[SimpleNamespace(type="response.created")],
        stream_error=RuntimeError("connection reset by peer"),
    )
    backup = FakeModel(events=["complete"])
    model = FallbackModel([
        _candidate("primary", primary),
        _candidate("backup", backup),
    ])

    seen = ModelSelection()
    keys = []
    with model_selection(seen):
        async for _event in model.stream_response(model_settings=None):
            keys.append(selected_model_key())

    # The primary never committed content, so it is never named.
    assert keys == ["backup"]
    assert seen.key == "backup"


@pytest.mark.asyncio
async def test_a_candidate_that_only_emits_lifecycle_events_is_not_reported():
    class EndingAfterLifecycle(FakeModel):
        async def stream_response(self, *args, **kwargs):
            yield SimpleNamespace(type="response.created")

    model = FallbackModel([_candidate("silent", EndingAfterLifecycle())])

    seen = ModelSelection()
    with model_selection(seen):
        events = [event async for event in model.stream_response(model_settings=None)]

    assert [event.type for event in events] == ["response.created"]
    assert seen.key is None


@pytest.mark.asyncio
async def test_the_selection_binding_is_reset_when_the_turn_ends():
    model = FallbackModel([_candidate("primary", FakeModel(events=["token"]))])

    assert selected_model_key() is None
    with model_selection(ModelSelection()):
        await _stream_events(model)
        assert selected_model_key() == "primary"
    assert selected_model_key() is None


@pytest.mark.asyncio
async def test_the_consumer_task_sees_the_model_streamed_by_the_run_task():
    """The Agents SDK runs the model in a child task; the consumer reads the
    turn's selection in the task that started it. The object is shared by
    reference, so the child's record is visible to the parent."""
    import asyncio

    model = FallbackModel([_candidate("primary", FakeModel(events=["token"]))])

    async def run_stream():
        assert [event async for event in model.stream_response(model_settings=None)] == ["token"]

    seen = ModelSelection()
    with model_selection(seen):
        await asyncio.create_task(run_stream())
        assert selected_model_key() == "primary"

    assert seen.key == "primary"


@pytest.mark.asyncio
async def test_concurrent_turns_keep_their_own_selection():
    import asyncio

    async def turn(key):
        model = FallbackModel([_candidate(key, FakeModel(events=[f"{key} token"]))])
        seen = ModelSelection()
        with model_selection(seen):
            async for _event in model.stream_response(model_settings=None):
                await asyncio.sleep(0)
            return selected_model_key()

    assert await asyncio.gather(turn("alpha"), turn("beta")) == ["alpha", "beta"]
    # A concurrent turn's binding never leaks into this context.
    assert selected_model_key() is None
