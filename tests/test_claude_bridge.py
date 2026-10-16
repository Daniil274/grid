"""The Claude subscription bridge: auth hardening, the CLI protocol, the adapter.

No Claude Code CLI is installed here, so the tests speak the documented
``stream-json`` protocol to a fake process and check the translation, the
command line, the per-request subscription check, and the refusal to run
anywhere but an explicit local single-user server.
"""

import asyncio
import sys
import time
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agents import AgentOutputSchema, ModelSettings, function_tool
from agents.models.interface import ModelTracing

from core.claude_cli import (
    ClaudeCliError,
    ClaudeCodeRunner,
    ClaudeEvent,
    auth_method,
    claude_environment,
    is_subscription,
    parse_stream_line,
    require_subscription,
)
from core.claude_model import ClaudeCodeModel
from core.credentials import CredentialError
from core.factory.models import ModelProvider
from schemas.schemas import GridConfig, ProviderConfig
from web_chat.entitlements import Plans, UserCredentials

# -- fake process ------------------------------------------------------------

class _FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, chunk):
        self.data += chunk

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _FakeStream:
    def __init__(self, *, data=b"", lines=(), hang=False):
        self._data = data
        self._lines = list(lines)
        self._hang = hang

    async def read(self):
        return self._data

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line
            if self._hang:
                await asyncio.Event().wait()
        return gen()


class _FakeProcess:
    def __init__(self, *, stdout_data=b"", stdout_lines=(), stderr=b"", returncode=0, hang=False):
        self._stdout_data = stdout_data
        self._stderr_data = stderr
        self.stdout = _FakeStream(data=stdout_data, lines=stdout_lines, hang=hang)
        self.stderr = _FakeStream(data=stderr, lines=stderr.splitlines(keepends=True))
        self.stdin = _FakeStdin()
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self._hang = hang
        self._exited = asyncio.Event()
        if not hang:
            self._exited.set()

    async def communicate(self, input=None):
        self._exited.set()
        return self._stdout_data, self._stderr_data

    async def wait(self):
        if self._hang:
            await self._exited.wait()
        return self.returncode

    def terminate(self):
        self.terminated = True
        self._exited.set()

    def kill(self):
        self.killed = True
        self._exited.set()


class _Spawner:
    """A fake ``spawn``: auth calls and generation calls, in order."""

    def __init__(self):
        self.calls = []
        self.auth_processes = []
        self.generation_processes = []

    async def __call__(self, argv, *, cwd, env):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        if "auth" in argv:
            if self.auth_processes:
                return self.auth_processes.pop(0)
            return _FakeProcess(stdout_data=b'{"authMethod": "claude.ai"}')
        return self.generation_processes.pop(0)

    @property
    def auth_calls(self):
        return [c for c in self.calls if "auth" in c["argv"]]

    @property
    def generation_calls(self):
        return [c for c in self.calls if "auth" not in c["argv"]]


def _runner(spawner, **kwargs):
    return ClaudeCodeRunner(binary=sys.executable, spawn=spawner, **kwargs)


async def _collect(iterator):
    return [event async for event in iterator]


# -- environment and auth ----------------------------------------------------

def test_credentials_are_stripped_from_the_child_environment():
    env = claude_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "ANTHROPIC_BASE_URL": "https://gateway.test",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_ACCESS_KEY_ID": "AKIA",
            "AWS_PROFILE": "prod",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/sa.json",
            "AZURE_OPENAI_API_KEY": "az",
            "USER": "u",
        }
    )
    assert env == {"PATH": "/usr/bin", "HOME": "/home/u", "USER": "u"}


def test_a_subscription_login_is_accepted_and_anything_else_is_refused():
    assert is_subscription("claude.ai") and is_subscription("oauth_token")
    assert require_subscription("claude.ai") == "claude.ai"
    for method in ("api_key", "api_key_helper", "third_party"):
        with pytest.raises(ClaudeCliError, match="not a Claude subscription"):
            require_subscription(method)
    with pytest.raises(ClaudeCliError, match="no authentication method"):
        require_subscription(None)


def test_auth_method_reads_only_a_real_method():
    assert auth_method('{"authMethod": "claude.ai", "loggedIn": true}') == "claude.ai"
    assert auth_method({"authMethod": "api_key"}) == "api_key"
    assert auth_method("not json") is None
    assert auth_method({"loggedIn": False}) is None


def test_empty_lines_are_skipped_and_garbage_is_refused():
    assert parse_stream_line("  \n") is None
    assert parse_stream_line('{"type": "result"}') == {"type": "result"}
    with pytest.raises(ClaudeCliError, match="not JSON"):
        parse_stream_line("Warning: something")


# -- the command line --------------------------------------------------------

def test_the_command_disables_tools_mcp_and_host_settings():
    runner = ClaudeCodeRunner(binary="claude", model="sonnet")
    argv = runner.command()
    assert argv[0] == "claude" and "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--include-partial-messages" not in argv  # only plain streaming asks
    # The prompt is never on the command line.
    assert argv[-1] != ""


def test_auth_status_disables_host_settings_like_generation():
    spawner = _Spawner()
    spawner.auth_processes.append(_FakeProcess(stdout_data=b'{"authMethod": "claude.ai"}'))
    runner = _runner(spawner)
    assert asyncio.run(runner.auth_status()) == "claude.ai"
    argv = spawner.auth_calls[0]["argv"]
    assert argv[1:3] == ["auth", "status"]
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_a_json_schema_and_partials_reach_the_command():
    runner = ClaudeCodeRunner(binary="claude")
    schema = {"type": "object", "properties": {"message": {"type": "string"}}}
    argv = runner.command(json_schema=schema, partials=True)
    assert argv[argv.index("--include-partial-messages")] == "--include-partial-messages"
    encoded = argv[argv.index("--json-schema") + 1]
    assert '"message"' in encoded and "\n" not in encoded


def test_the_prompt_is_written_to_stdin_not_argv():
    spawner = _Spawner()
    spawner.generation_processes.append(_FakeProcess(stdout_lines=[b'{"type":"result","subtype":"success"}']))
    runner = _runner(spawner)
    events = asyncio.run(_collect(runner.stream("a very long prompt")))
    assert [e.kind for e in events] == ["result"]
    generation = spawner.generation_calls[0]
    assert "a very long prompt" not in generation["argv"]


def test_a_nonzero_exit_reports_the_cli_stderr():
    spawner = _Spawner()
    spawner.generation_processes.append(_FakeProcess(stdout_lines=[], stderr=b"Error: invalid flag", returncode=2))
    runner = _runner(spawner)
    with pytest.raises(ClaudeCliError, match="invalid flag"):
        asyncio.run(_collect(runner.stream("hi")))


def test_auth_status_reads_the_method_the_cli_reports():
    spawner = _Spawner()
    spawner.auth_processes.append(_FakeProcess(stdout_data=b'{"authMethod": "claude.ai"}', returncode=0))
    runner = _runner(spawner)
    assert asyncio.run(runner.auth_status()) == "claude.ai"


# -- the subscription is checked before every request ------------------------

def test_auth_runs_before_every_generation_and_strips_the_environment():
    spawner = _Spawner()
    spawner.auth_processes.append(_FakeProcess(stdout_data=b'{"authMethod": "claude.ai"}'))
    spawner.generation_processes.append(_FakeProcess(stdout_lines=[b'{"type":"result","subtype":"success"}']))
    runner = _runner(spawner, environ={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-secret"})
    asyncio.run(_collect(runner.stream("hi")))

    assert spawner.auth_calls and spawner.generation_calls
    first, second = spawner.calls[0], spawner.calls[1]
    assert first["argv"][1:3] == ["auth", "status"]
    assert "auth" not in second["argv"]
    assert "ANTHROPIC_API_KEY" not in second["env"]


def test_an_api_key_login_refuses_before_any_generation_spawn():
    spawner = _Spawner()
    spawner.auth_processes.append(_FakeProcess(stdout_data=b'{"authMethod": "api_key"}'))
    runner = _runner(spawner)
    with pytest.raises(ClaudeCliError, match="not a Claude subscription"):
        asyncio.run(_collect(runner.stream("hi")))
    assert len(spawner.generation_calls) == 0


def test_a_cancelled_consumer_terminates_the_process_instead_of_waiting():
    spawner = _Spawner()
    spawner.auth_processes.append(_FakeProcess(stdout_data=b'{"authMethod": "claude.ai"}'))
    process = _FakeProcess(stdout_lines=[b'{"type":"system","subtype":"init"}'], hang=True, returncode=None)
    spawner.generation_processes.append(process)
    runner = _runner(spawner)

    async def consume_one_then_stop():
        stream = runner.stream("hi")
        first = await stream.__anext__()
        assert first.kind == "system"
        await stream.aclose()  # the consumer stops reading

    asyncio.run(consume_one_then_stop())
    assert process.terminated
    assert not process.killed


# -- the model adapter -------------------------------------------------------

class _FakeRunner:
    """A runner that yields prepared events and records what the model asked."""

    def __init__(self, events):
        self.events = events
        self.calls = []

    async def stream(self, prompt, *, json_schema=None, partials=False):
        self.calls.append({"prompt": prompt, "json_schema": json_schema, "partials": partials})
        for event in self.events:
            yield event


def _model(events):
    runner = _FakeRunner(events)
    return ClaudeCodeModel(runner, "claude-sonnet-5"), runner


async def _get(model, *, input=({"role": "user", "content": "hi"},), tools=(), handoffs=(), output_schema=None):
    return await model.get_response(
        "You are a test.", list(input), ModelSettings(), list(tools), output_schema,
        list(handoffs), ModelTracing.DISABLED,
    )


async def _stream(model, *, input=({"role": "user", "content": "hi"},), tools=(), handoffs=(), output_schema=None):
    return [
        event
        async for event in model.stream_response(
            "You are a test.", list(input), ModelSettings(), list(tools), output_schema,
            list(handoffs), ModelTracing.DISABLED,
        )
    ]


def _text(response):
    parts = []
    for item in response.output:
        if getattr(item, "type", "") == "message":
            for block in item.content:
                parts.append(getattr(block, "text", ""))
    return "".join(parts)


def _calls(response):
    return [item for item in response.output if getattr(item, "type", "") == "function_call"]


def test_text_streams_as_deltas_without_duplicating_the_completed_block():
    events = [
        ClaudeEvent("system", "init", {"type": "system", "subtype": "init"}),
        ClaudeEvent("stream_event", "", {"event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}}),
        ClaudeEvent("stream_event", "", {"event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}}),
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "Hello"}),
    ]
    model, runner = _model(events)
    asyncio.run(_stream(model))
    assert runner.calls[0]["partials"] is True and runner.calls[0]["json_schema"] is None

    response = asyncio.run(_get(model))
    assert _text(response) == "Hello"  # not "HelloHello"


def test_an_unexpected_native_tool_call_is_refused():
    events = [
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": ""}),
    ]
    model, _ = _model(events)
    with pytest.raises(ClaudeCliError, match="ran a tool"):
        asyncio.run(_get(model))


class _Out(BaseModel):
    answer: str


def _structured_output_tool_event():
    """The assistant message Claude Code emits while satisfying ``--json-schema``."""
    return ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "s1", "name": "StructuredOutput", "input": {"answer": "42"}},
    ]}})


def test_the_cli_structured_output_tool_is_exempt_when_a_schema_was_asked():
    # Realistic shape: the internal StructuredOutput tool_use precedes the result
    # that carries the validated value. Without the exemption this raised.
    events = [
        _structured_output_tool_event(),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": "", "structured_output": {"answer": "42"}}),
    ]
    model, runner = _model(events)
    response = asyncio.run(_get(model, output_schema=AgentOutputSchema(_Out)))
    assert runner.calls[0]["json_schema"] is not None
    assert _text(response) == '{"answer": "42"}'


def test_the_cli_structured_output_tool_input_is_used_when_the_result_omits_it():
    events = [
        _structured_output_tool_event(),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": ""}),
    ]
    model, _ = _model(events)
    response = asyncio.run(_get(model, output_schema=AgentOutputSchema(_Out)))
    assert _text(response) == '{"answer": "42"}'


def test_the_cli_structured_output_tool_is_still_refused_in_plain_mode():
    events = [
        _structured_output_tool_event(),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": ""}),
    ]
    model, runner = _model(events)
    with pytest.raises(ClaudeCliError, match="ran a tool"):
        asyncio.run(_get(model))
    # Plain mode never asked for a schema, so nothing is exempted.
    assert runner.calls[0]["json_schema"] is None


def test_usage_is_taken_from_the_result():
    events = [
        ClaudeEvent("stream_event", "", {"event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "hi",
                                          "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 5}}),
    ]
    model, _ = _model(events)
    response = asyncio.run(_get(model))
    assert (response.usage.input_tokens, response.usage.output_tokens, response.usage.total_tokens) == (15, 3, 18)


# -- tools through the structured envelope -----------------------------------

@function_tool
def grid_echo(x: int) -> str:
    """Echo x back."""
    return str(x)


def _stub_handoff():
    return SimpleNamespace(
        tool_name="hand_off_to_expert",
        tool_description="Hand the talk to the expert agent.",
        input_json_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    )


def test_the_tool_schema_is_visible_to_the_cli_and_the_envelope_offers_it():
    events = [
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": "",
                                          "structured_output": {"message": "one sec", "tool_calls": [
                                              {"name": "grid_echo", "arguments": {"x": 1}},
                                          ]}}),
    ]
    model, runner = _model(events)
    response = asyncio.run(_get(model, tools=[grid_echo], handoffs=[_stub_handoff()]))

    call = runner.calls[0]
    assert call["partials"] is False
    assert call["json_schema"]["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == [
        "grid_echo",
        "hand_off_to_expert",
    ]
    # The prompt carries each tool's name and parameter schema for the model.
    assert "grid_echo" in call["prompt"] and '"x"' in call["prompt"]

    calls = _calls(response)
    assert [(c.name, c.arguments) for c in calls] == [("grid_echo", '{"x": 1}')]
    assert "one sec" in _text(response)


def test_the_envelope_carries_a_structured_final_answer_when_asked():
    class Final(BaseModel):
        answer: str

    events = [
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": "",
                                          "structured_output": {"message": "", "tool_calls": [],
                                                                "final": {"answer": "done"}}}),
    ]
    model, runner = _model(events)
    response = asyncio.run(_get(model, tools=[grid_echo], output_schema=AgentOutputSchema(Final)))
    call = runner.calls[0]
    assert call["json_schema"]["properties"]["final"]["properties"]["answer"]["type"] == "string"
    assert _text(response) == '{"answer": "done"}' and _calls(response) == []


def test_a_missing_envelope_is_refused_not_silently_ignored():
    events = [
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "text"}),
    ]
    model, _ = _model(events)
    with pytest.raises(ClaudeCliError, match="tool envelope"):
        asyncio.run(_get(model, tools=[grid_echo]))


# -- structured output without tools -----------------------------------------

def test_output_schema_becomes_the_cli_json_schema_and_its_json_is_the_answer():
    class Final(BaseModel):
        answer: str

    events = [
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": "", "structured_output": {"answer": "42"}}),
    ]
    model, runner = _model(events)
    response = asyncio.run(_get(model, output_schema=AgentOutputSchema(Final)))
    schema = runner.calls[0]["json_schema"]
    assert schema["properties"]["answer"]["type"] == "string"
    assert "tool_calls" not in schema.get("properties", {})
    assert _text(response) == '{"answer": "42"}'


def test_a_success_result_without_structured_output_is_refused():
    events = [
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "plain"}),
    ]
    model, _ = _model(events)
    with pytest.raises(ClaudeCliError, match="without structured output"):
        asyncio.run(_get(model, output_schema=AgentOutputSchema(_Final)))


class _Final(BaseModel):
    answer: str


# -- history and unsupported content -----------------------------------------

def test_prior_tool_calls_and_results_survive_in_the_prompt():
    events = [ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "ok"})]
    model, runner = _model(events)
    history = [
        {"role": "user", "content": "what is 2+2?"},
        {"type": "function_call", "name": "grid_echo", "arguments": '{"x": 4}', "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "4"},
        {"role": "user", "content": "thanks"},
    ]
    asyncio.run(_get(model, input=history))
    prompt = runner.calls[0]["prompt"]
    assert "grid_echo" in prompt and '{"x": 4}' in prompt
    assert "tool_result (call_id=c1)" in prompt and "4" in prompt
    assert "what is 2+2?" in prompt and "thanks" in prompt


def test_an_image_input_is_refused_rather_than_dropped():
    events = [ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "ok"})]
    model, _ = _model(events)
    history = [{"role": "user", "content": [
        {"type": "input_text", "text": "what is this?"},
        {"type": "input_image", "image_url": "https://example.test/cat.png"},
    ]}]
    with pytest.raises(ClaudeCliError, match="cannot send input_image"):
        asyncio.run(_get(model, input=history))


def test_a_hosted_tool_is_refused_rather_than_dropped():
    events = [ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "ok"})]
    model, _ = _model(events)
    with pytest.raises(ClaudeCliError, match="function tools only"):
        asyncio.run(_get(model, tools=[SimpleNamespace(name="web_search")]))


# -- configuration -----------------------------------------------------------

def test_the_schema_accepts_a_claude_provider():
    provider = ProviderConfig(name="claude-code", base_url="cli://claude", auth="claude", claude_binary="/usr/bin/claude")
    assert (provider.auth, provider.claude_binary) == ("claude", "/usr/bin/claude")


def test_the_factory_builds_the_cli_model_and_holds_no_key():
    provider = SimpleNamespace(auth="claude", claude_binary=None)
    model_config = SimpleNamespace(name="sonnet", provider="claude-code", response_timeout=None, max_tokens=None)
    config = SimpleNamespace(
        get_model=lambda key: model_config,
        get_provider=lambda key: provider,
        get_working_directory=lambda: "/tmp/ws",
    )
    factory = ModelProvider(config, None, None)
    model, returned = factory.sdk_model("claude-sonnet")
    assert returned is model_config
    assert isinstance(model, ClaudeCodeModel)
    assert model._runner.working_directory == "/tmp/ws"
    assert model._runner.command()[model._runner.command().index("--tools") + 1] == ""


def test_the_factory_bounds_the_cli_model_like_other_plan_models():
    from core.bounded_model import BoundedModel

    provider = SimpleNamespace(auth="claude", claude_binary=None)
    model_config = SimpleNamespace(name="sonnet", provider="claude-code", response_timeout=12.0, max_tokens=100)
    config = SimpleNamespace(
        get_model=lambda key: model_config,
        get_provider=lambda key: provider,
        get_working_directory=lambda: "/tmp/ws",
    )
    model, _ = ModelProvider(config, None, None).sdk_model("claude-sonnet")
    assert isinstance(model, BoundedModel)
    assert isinstance(model.inner, ClaudeCodeModel)


# -- the local single-user boundary ------------------------------------------

def _claude_provider():
    return SimpleNamespace(name="claude-code", base_url="cli://claude", auth="claude", api_key=None, api_key_env=None)


def test_a_remote_user_may_not_spend_the_host_cli_login():
    plans = Plans(lambda: GridConfig(user_limits={"turns_per_day": 1}))
    provider = _claude_provider()
    remote = UserCredentials("u1", lambda: plans.resolve(admin=True), plans, None, environ={})
    assert remote.available(provider) is False
    assert "local, single-user server" in remote.problem(provider)
    with pytest.raises(CredentialError, match="local, single-user server"):
        asyncio.run(remote.credential(provider))


def test_the_explicit_local_single_user_may_use_the_cli_login():
    plans = Plans(lambda: GridConfig(user_limits={"turns_per_day": 1}))
    provider = _claude_provider()
    local = UserCredentials("u1", lambda: plans.resolve(admin=True), plans, None, environ={}, local_cli_login=True)
    assert local.available(provider) is True
    credential = asyncio.run(local.credential(provider))
    assert credential.subscription and not credential.charged


# -- real subprocesses (no Claude CLI): the process-lifecycle guarantees ------

_AUTH_CODE = "import sys; sys.stdout.write('{\"authMethod\": \"claude.ai\"}')"


def _real_spawn(generation_code, record):
    """A spawn that runs a real Python child in place of the CLI binary."""

    async def spawn(argv, *, cwd, env):
        code = _AUTH_CODE if "auth" in argv else generation_code
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        record.append(process)
        return process

    return spawn


def test_a_normal_stdout_eof_waits_for_a_delayed_zero_exit():
    # Close stdout (EOF to the reader) but stay alive briefly before exiting 0.
    # The old code terminated on EOF and killed this successful process.
    code = (
        "import sys, time, json\n"
        "sys.stdout.write(json.dumps({'type': 'result', 'subtype': 'success', "
        "'is_error': False, 'result': 'ok'}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdout.close()\n"
        "time.sleep(0.4)\n"
    )
    procs = []
    runner = ClaudeCodeRunner(binary=sys.executable, spawn=_real_spawn(code, procs))
    started = time.monotonic()
    events = asyncio.run(_collect(runner.stream("hi")))
    elapsed = time.monotonic() - started

    assert [event.kind for event in events] == ["result"]
    assert elapsed >= 0.3  # waited for the real exit rather than racing it
    assert procs[-1].returncode == 0  # exited normally, not SIGTERM'd


def test_a_cancelled_consumer_kills_and_reaps_a_real_child():
    code = (
        "import sys, time, json\n"
        "sys.stdout.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    procs = []
    runner = ClaudeCodeRunner(binary=sys.executable, spawn=_real_spawn(code, procs))

    async def scenario():
        stream = runner.stream("hi")
        assert (await stream.__anext__()).kind == "system"
        task = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert procs[-1].returncode is not None  # terminated and reaped, not orphaned


def test_a_stdin_write_error_still_cleans_up_a_real_child():
    code = "import time\ntime.sleep(30)\n"
    procs = []

    class _BrokenStdin:
        def write(self, data):
            raise BrokenPipeError("stdin is gone")

        async def drain(self):
            raise BrokenPipeError("stdin is gone")

        def close(self):
            pass

    async def spawn(argv, *, cwd, env):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _AUTH_CODE if "auth" in argv else code,
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        procs.append(child)
        if "auth" not in argv:
            child.stdin = _BrokenStdin()  # the drain can fail before any output
        return child

    runner = ClaudeCodeRunner(binary=sys.executable, spawn=spawn)
    with pytest.raises(BrokenPipeError):
        asyncio.run(_collect(runner.stream("hi")))
    assert procs[-1].returncode is not None  # the failed write did not orphan it
