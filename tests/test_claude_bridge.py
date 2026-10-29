"""The Claude subscription bridge: auth hardening, the CLI protocol, the adapter.

No Claude Code CLI is installed here, so the tests speak the documented
``stream-json`` protocol to a fake process and check the translation, the
command line, the per-request subscription check, and the refusal to run
anywhere but an explicit local single-user server.
"""

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agents import AgentOutputSchema, ModelSettings, function_tool
from agents.models.interface import ModelTracing
from openai.types.shared import Reasoning

from core.claude_cli import (
    ClaudeCliError,
    ClaudeCodeRunner,
    ClaudeEvent,
    auth_method,
    cli_effort,
    claude_environment,
    is_subscription,
    parse_stream_line,
    require_subscription,
)
from core.claude_model import BRIDGE_PREAMBLE, ClaudeCodeModel
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
    # ``--tools`` leaves MCP alone; the deny rule removes those tools from the
    # context entirely, and is scoped so the internal StructuredOutput survives.
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__*"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--include-partial-messages" not in argv  # only plain streaming asks
    # The prompt is never on the command line.
    assert argv[-1] != ""


def test_auth_status_passes_no_flags_the_subcommand_rejects():
    spawner = _Spawner()
    spawner.auth_processes.append(_FakeProcess(stdout_data=b'{"authMethod": "claude.ai"}'))
    runner = _runner(spawner)
    assert asyncio.run(runner.auth_status()) == "claude.ai"
    argv = spawner.auth_calls[0]["argv"]
    assert argv[1:] == ["auth", "status"]


def test_a_refused_auth_status_reports_the_cli_stderr_not_a_logout():
    spawner = _Spawner()
    spawner.auth_processes.append(
        _FakeProcess(stderr=b"error: unknown option '--setting-sources'\n", returncode=1)
    )
    runner = _runner(spawner)
    with pytest.raises(ClaudeCliError, match="unknown option"):
        asyncio.run(runner.auth_status())


def test_a_signed_out_cli_is_reported_as_no_login():
    spawner = _Spawner()
    spawner.auth_processes.append(
        _FakeProcess(stdout_data=b'{"loggedIn": false, "authMethod": "none"}', returncode=1)
    )
    runner = _runner(spawner)
    assert asyncio.run(runner.auth_status()) == "none"


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
        self.consumed = 0

    async def stream(self, prompt, *, json_schema=None, partials=False, system_prompt=None, effort=None, images=()):
        self.calls.append({"prompt": prompt, "json_schema": json_schema, "partials": partials,
                           "system": system_prompt, "effort": effort, "images": list(images)})
        for event in self.events:
            self.consumed += 1
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


def test_a_native_tool_call_without_a_tool_result_is_only_a_proposal():
    # An ``assistant`` tool_use is a suggestion; unless the CLI answers it with a
    # tool_result for that id, nothing ran and the turn must not be refused.
    events = [
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
            {"type": "text", "text": "Hello"},
        ]}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": "Hello"}),
    ]
    model, _ = _model(events)
    assert _text(asyncio.run(_get(model))) == "Hello"


def test_a_native_tool_call_the_cli_answered_goes_back_to_the_agent():
    # A tool_result carrying the call's id means the CLI answered that call.
    # The turn must not die on it: the call goes back to the agent as an
    # ordinary tool call, whose answer tells the model the tool is not there.
    events = [
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]}}),
        ClaudeEvent("user", "", {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": "a.txt"},
        ]}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": ""}),
    ]
    model, _ = _model(events)
    calls = _calls(asyncio.run(_get(model)))
    assert [(call.name, call.arguments) for call in calls] == [("Bash", '{"command": "ls"}')]


def test_a_tool_result_for_another_call_does_not_convict_this_one():
    events = [
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]}}),
        ClaudeEvent("user", "", {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "other", "is_error": False, "content": "a.txt"},
        ]}}),
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Hello"},
        ]}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": "Hello"}),
    ]
    model, _ = _model(events)
    assert _text(asyncio.run(_get(model))) == "Hello"


def test_an_escaped_native_call_does_not_end_the_turn():
    """ctx-7dc8d1e0: the model asked for a tool by name and the whole turn died.

    The user got "The turn did not finish ... ran a tool it was not given"
    instead of an answer. The call must reach the agent as a tool call, whose
    answer (the SDK's unknown-tool error) lets the model continue.
    """
    from agents import Agent, Runner

    from core import sdk_patches

    sdk_patches.install()  # a run answers a tool it does not know instead of dying
    first = [
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/workspace"}},
        ]}}),
        ClaudeEvent("user", "", {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": "a.txt"},
        ]}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": ""}),
    ]
    answer = "В папке только служебные файлы."
    second = [
        ClaudeEvent("stream_event", "", {"event": {"type": "content_block_delta",
                                                   "delta": {"type": "text_delta", "text": answer}}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": answer}),
    ]
    runner = _ScriptedRunner([first, second])
    agent = Agent(name="engineer", model=ClaudeCodeModel(runner, "claude-sonnet-5"))

    result = asyncio.run(Runner.run(agent, "Что лежит в папке?", max_turns=3))

    assert result.final_output == answer
    assert runner.calls == 2  # the model got a second turn, not a dead run
    answers = [
        item.raw_item["output"]
        for item in result.new_items
        if item.type == "tool_call_output_item"
    ]
    assert "Tool 'Read' is not available" in answers[0]


class _ScriptedRunner:
    """A runner that plays a different batch of events on each call."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    async def stream(self, prompt, *, json_schema=None, partials=False, system_prompt=None,
                     effort=None, images=()):
        batch = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        for event in batch:
            yield event


def test_init_with_tools_other_than_the_schema_tool_is_refused_before_generation():
    # The CLI says which tools it holds at ``init``; anything but the internal
    # StructuredOutput tool means the lockdown did not take, so the turn fails
    # on the first event instead of generating.
    events = [
        ClaudeEvent("system", "init", {"type": "system", "subtype": "init", "tools": ["Bash", "mcp__x__y"]}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": ""}),
    ]
    model, runner = _model(events)
    with pytest.raises(ClaudeCliError, match=r"holds tools this bridge did not give it \(Bash, mcp__x__y\)"):
        asyncio.run(_get(model))
    assert runner.consumed == 1  # stopped at init, before the rest of the run


def test_init_announcing_only_the_schema_tool_is_accepted_on_a_schema_turn():
    events = [
        ClaudeEvent("system", "init", {"type": "system", "subtype": "init", "tools": ["StructuredOutput"]}),
        _structured_output_tool_event(),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": "", "structured_output": {"answer": "42"}}),
    ]
    model, _ = _model(events)
    response = asyncio.run(_get(model, output_schema=AgentOutputSchema(_Out)))
    assert _text(response) == '{"answer": "42"}'


def test_init_announcing_the_schema_tool_on_a_plain_turn_is_refused():
    events = [
        ClaudeEvent("system", "init", {"type": "system", "subtype": "init", "tools": ["StructuredOutput"]}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": ""}),
    ]
    model, _ = _model(events)
    with pytest.raises(ClaudeCliError, match="holds tools this bridge did not give it"):
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


def test_the_cli_structured_output_tool_result_is_handed_back_in_plain_mode():
    events = [
        _structured_output_tool_event(),
        ClaudeEvent("user", "", {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "s1", "is_error": False,
             "content": "Structured output provided successfully"},
        ]}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": ""}),
    ]
    model, runner = _model(events)
    calls = _calls(asyncio.run(_get(model)))
    assert [call.name for call in calls] == ["StructuredOutput"]
    # Plain mode never asked for a schema, so nothing is exempted.
    assert runner.calls[0]["json_schema"] is None


def test_grid_instructions_replace_the_cli_system_prompt():
    events = [ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": ""})]
    model, runner = _model(events)
    asyncio.run(_get(model, input=({"role": "user", "content": "hello there"},)))
    call = runner.calls[0]
    assert call["system"].startswith(BRIDGE_PREAMBLE)
    assert "You are a test." in call["system"]
    # The conversation is the prompt; the instructions are not repeated in it.
    assert "hello there" in call["prompt"] and "You are a test." not in call["prompt"]


def test_thinking_summaries_stream_as_reasoning_before_the_answer():
    def delta(kind, **fields):
        return ClaudeEvent("stream_event", "", {"event": {"type": "content_block_delta",
                                                          "delta": {"type": kind, **fields}}})
    events = [
        delta("thinking_delta", thinking="", estimated_tokens=50),  # withheld text: nothing to show
        delta("thinking_delta", thinking="Checking the sums."),
        delta("text_delta", text="42"),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "42"}),
    ]
    model, _ = _model(events)
    kinds = [getattr(e, "type", "") for e in asyncio.run(_stream(model))]
    reasoning = [e.delta for e in asyncio.run(_stream(model)) if getattr(e, "type", "") == "response.reasoning_summary_text.delta"]
    assert reasoning == ["Checking the sums."]
    assert kinds.index("response.reasoning_summary_text.delta") < kinds.index("response.output_text.delta")
    assert _text(asyncio.run(_get(model))) == "42"


def test_the_reasoning_effort_becomes_the_cli_effort():
    events = [ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": ""})]
    model, runner = _model(events)
    asyncio.run(model.get_response(
        "You are a test.", [{"role": "user", "content": "hi"}], ModelSettings(reasoning=Reasoning(effort="minimal")),
        [], None, [], ModelTracing.DISABLED,
    ))
    assert runner.calls[0]["effort"] == "low"
    assert [cli_effort(v) for v in ("high", "xhigh", "medium", "bogus", None)] == ["high", "xhigh", "medium", None, None]


def test_a_cli_error_result_is_reported_in_the_cli_words():
    events = [ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": True,
                                                "result": "Claude AI usage limit reached|1792882200"})]
    model, _ = _model(events)
    with pytest.raises(ClaudeCliError, match="usage limit reached"):
        asyncio.run(_get(model, tools=[grid_echo]))


def test_the_command_takes_the_system_prompt_file_and_effort():
    argv = ClaudeCodeRunner(binary="claude").command(system_prompt_file="/tmp/s.md", effort="high")
    assert argv[argv.index("--system-prompt-file") + 1] == "/tmp/s.md"
    assert argv[argv.index("--effort") + 1] == "high"
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--thinking-display") + 1] == "summarized"


def test_the_system_prompt_file_is_private_and_removed_after_the_run():
    seen = {}
    spawner = _Spawner()

    async def spawn(argv, *, cwd, env):
        if "--system-prompt-file" in argv:
            path = argv[argv.index("--system-prompt-file") + 1]
            seen["path"] = path
            seen["mode"] = os.stat(path).st_mode & 0o777
            with open(path, encoding="utf-8") as handle:  # noqa: ASYNC230 - a test's tiny file
                seen["text"] = handle.read()
        return await spawner(argv, cwd=cwd, env=env)

    spawner.generation_processes.append(_FakeProcess(stdout_lines=[b'{"type":"result","subtype":"success"}']))
    runner = ClaudeCodeRunner(binary=sys.executable, spawn=spawn)
    asyncio.run(_collect(runner.stream("hi", system_prompt="Секрет оператора")))
    assert seen["text"] == "Секрет оператора" and seen["mode"] == 0o600
    assert not os.path.exists(seen["path"])


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


def test_a_grid_tool_called_by_name_that_the_cli_refused_does_not_end_the_turn():
    # What Sonnet does through Claude Code 2.1.310: it calls the Grid tool as a
    # native tool_use first, the CLI answers "No such tool available" (nothing
    # runs), and then it answers with the envelope.
    events = [
        ClaudeEvent("system", "init", {"type": "system", "subtype": "init", "tools": ["StructuredOutput"]}),
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "grid_echo", "input": {"x": 1}},
        ]}}),
        ClaudeEvent("user", "", {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "<tool_use_error>Error: No such tool available: grid_echo</tool_use_error>"},
        ]}}),
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "s1", "name": "StructuredOutput", "input": {
                "message": "", "tool_calls": [{"name": "grid_echo", "arguments": {"x": 1}}]}},
        ]}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                          "result": "",
                                          "structured_output": {"message": "", "tool_calls": [
                                              {"name": "grid_echo", "arguments": {"x": 1}},
                                          ]}}),
    ]
    model, _ = _model(events)
    response = asyncio.run(_get(model, tools=[grid_echo]))
    assert [call.name for call in _calls(response)] == ["grid_echo"]


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
    assert call["partials"] is True  # the thinking summaries stream on tool turns too
    assert call["json_schema"]["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == [
        "grid_echo",
        "hand_off_to_expert",
    ]
    # The system prompt carries each tool's name and parameter schema for the model.
    assert "grid_echo" in call["system"] and '"x"' in call["system"]

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


def test_images_go_to_the_cli_as_image_blocks_marked_in_the_prompt():
    events = [ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "ok"})]
    model, runner = _model(events)
    history = [
        {"role": "user", "content": [
            {"type": "input_text", "text": "what is this?"},
            {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo=", "detail": "auto"},
        ]},
        {"type": "function_call", "name": "screenshot", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": [
            {"type": "image_url", "image_url": {"url": "https://example.test/screen.jpg"}},
        ]},
    ]
    asyncio.run(_get(model, input=history))
    call = runner.calls[0]
    assert "what is this?\n[image 1]" in call["prompt"] and "[image 2]" in call["prompt"]
    assert call["images"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
        {"type": "image", "source": {"type": "url", "url": "https://example.test/screen.jpg"}},
    ]


def test_an_image_the_cli_cannot_take_is_refused_rather_than_dropped():
    events = [ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "ok"})]
    model, _ = _model(events)
    for part, reason in (
        ({"type": "input_image", "image_url": "data:image/tiff;base64,AAAA"}, "image/tiff"),
        ({"type": "input_image", "file_id": "file-1"}, "file_id"),
        ({"type": "input_file", "file_data": "x"}, "cannot send input_file"),
    ):
        with pytest.raises(ClaudeCliError, match=reason):
            asyncio.run(_get(model, input=[{"role": "user", "content": [part]}]))


def test_images_make_the_prompt_one_stream_json_user_message():
    import json

    spawner = _Spawner()
    process = _FakeProcess(stdout_lines=[b'{"type":"result","subtype":"success"}'])
    spawner.generation_processes.append(process)
    runner = _runner(spawner)
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    asyncio.run(_collect(runner.stream("look [image 1]", images=[image])))
    argv = spawner.generation_calls[0]["argv"]
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    message = json.loads(process.stdin.data.decode("utf-8"))
    assert message == {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "look [image 1]"}, image]}}
    assert "--input-format" not in runner.command()  # a text-only turn stays plain stdin


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


def test_a_summary_request_on_a_cli_model_runs_through_the_cli():
    from core.claude_model import ClaudeCodeChatClient

    provider = SimpleNamespace(auth="claude", claude_binary=None)
    model_config = SimpleNamespace(name="sonnet", provider="claude-code", response_timeout=None, max_tokens=None)
    config = SimpleNamespace(
        get_model=lambda key: model_config,
        get_provider=lambda key: provider,
        get_working_directory=lambda: "/tmp/ws",
    )
    client, model, options = ModelProvider(config, None, None).model_request("claude-sonnet")
    assert isinstance(client, ClaudeCodeChatClient) and model == "sonnet" and options is None
    assert client._runner.working_directory == "/tmp/ws"


def test_the_cli_chat_client_answers_like_chat_completions():
    from core.claude_model import ClaudeCodeChatClient

    runner = _FakeRunner([
        ClaudeEvent("assistant", "", {"type": "assistant", "message": {"content": [{"type": "text", "text": "Summary."}]}}),
        ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False, "result": "Summary."}),
    ])
    response = asyncio.run(ClaudeCodeChatClient(runner).chat.completions.create(
        model="sonnet", max_tokens=10, temperature=0.0,
        messages=[{"role": "system", "content": "Summarize."}, {"role": "user", "content": "a long talk"},
                  {"role": "user", "content": "Write the summary."}],
    ))
    assert response.choices[0].message.content == "Summary."
    call = runner.calls[0]
    assert call["system"].startswith(BRIDGE_PREAMBLE) and call["system"].endswith("Summarize.")
    assert "a long talk" in call["prompt"] and "Summarize." not in call["prompt"]


def test_the_cli_chat_client_reports_a_cli_error():
    from core.claude_model import ClaudeCodeChatClient

    runner = _FakeRunner([ClaudeEvent("result", "success", {"type": "result", "subtype": "success",
                                                            "is_error": True, "result": "Prompt is too long"})])
    with pytest.raises(ClaudeCliError, match="Prompt is too long"):
        asyncio.run(ClaudeCodeChatClient(runner).chat.completions.create(model="sonnet", messages=[{"role": "user", "content": "x"}]))


# -- the local single-user boundary ------------------------------------------

def _claude_provider():
    return SimpleNamespace(name="claude-code", base_url="cli://claude", auth="claude", api_key=None, api_key_env=None)


def test_a_remote_user_may_not_spend_the_host_cli_login():
    plans = Plans(lambda: GridConfig(user_limits={"turns_per_day": 1}))
    provider = _claude_provider()
    remote = UserCredentials("u1", lambda: plans.resolve(admin=True), plans, None, environ={})
    assert remote.available(provider) is False
    assert "only to admins and on the local, single-user server" in remote.problem(provider)
    with pytest.raises(CredentialError, match="only to admins and on the local, single-user"):
        asyncio.run(remote.credential(provider))


def test_the_factory_asks_whether_this_user_may_spend_the_cli_login():
    # sdk_model used to build the CLI model before any credential check, so on
    # a server with accounts every user could spend the operator's login.
    from core.model_access import ModelAccess

    plans = Plans(lambda: GridConfig(user_limits={"turns_per_day": 1}))
    provider = _claude_provider()
    config = SimpleNamespace(get_provider=lambda key: provider)
    remote = UserCredentials("u1", lambda: plans.resolve(admin=False), plans, None, environ={})
    with pytest.raises(CredentialError, match="only to admins"):
        ModelAccess(remote).check_cli_login(config, "claude-code")
    admin = UserCredentials("u2", lambda: plans.resolve(admin=True), plans, None, environ={}, local_cli_login=True)
    ModelAccess(admin).check_cli_login(config, "claude-code")
    ModelAccess().check_cli_login(config, "claude-code")  # a local run: the operator's own CLI
    ModelAccess(on_spend=lambda event: None).check_cli_login(config, "claude-code")  # metered, still the operator's

    model_config = SimpleNamespace(name="sonnet", provider="claude-code", response_timeout=None, max_tokens=None)
    grid = SimpleNamespace(get_model=lambda key: model_config, get_provider=lambda key: provider,
                           get_working_directory=lambda: "/tmp/ws")
    factory = ModelProvider(grid, SimpleNamespace(access=ModelAccess(remote)), None)
    with pytest.raises(CredentialError, match="only to admins"):
        factory.sdk_model("claude-sonnet")
    with pytest.raises(CredentialError, match="only to admins"):
        factory.model_request("claude-sonnet")


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


def test_a_binary_being_reinstalled_is_waited_for(tmp_path):
    binary = tmp_path / "claude"
    runner = ClaudeCodeRunner(binary=str(binary))

    async def reinstall_then_check():
        async def reinstall():
            await asyncio.sleep(0.3)
            binary.write_text("#!/bin/sh\n")
        task = asyncio.ensure_future(reinstall())
        found = await runner._wait_available(timeout=5)
        await task
        return found

    assert asyncio.run(reinstall_then_check()) is True
    assert asyncio.run(ClaudeCodeRunner(binary=str(tmp_path / "gone"))._wait_available(timeout=0.2)) is False


# -- streaming the envelope message on tool turns ------------------------------

from core.claude_model import _MessageField  # noqa: E402


def _feed_all(fragments):
    field = _MessageField()
    seen = []
    for fragment in fragments:
        text = field.feed(fragment)
        if text is not None:
            seen.append(text)
    return seen


def test_the_message_field_is_read_as_it_arrives():
    seen = _feed_all(['{"mes', 'sage": "Hel', 'lo, ', 'world", "tool_calls": []}'])
    assert seen == ["Hel", "Hello, ", "Hello, world"]


def test_the_message_field_holds_back_a_torn_escape():
    seen = _feed_all(['{"message": "a\\', 'nb \\u04', '1f"}'])
    assert seen[-1] == "a\nb П"
    assert all(not text.endswith("\\") for text in seen)


def test_a_nested_message_key_is_not_the_envelope_message():
    seen = _feed_all([
        '{"tool_calls": [{"name": "x", "arguments": {"message": "inner"}}], ',
        '"message": "outer"}',
    ])
    assert seen == ["outer"]


def _envelope_events(fragments, message, *, index=0):
    events = [
        ClaudeEvent("system", "init", {"type": "system", "subtype": "init", "tools": ["StructuredOutput"]}),
        ClaudeEvent("stream_event", "", {"event": {"type": "content_block_start", "index": index,
                                                   "content_block": {"type": "tool_use", "id": "s1",
                                                                     "name": "StructuredOutput", "input": {}}}}),
    ]
    for fragment in fragments:
        events.append(ClaudeEvent("stream_event", "", {"event": {
            "type": "content_block_delta", "index": index,
            "delta": {"type": "input_json_delta", "partial_json": fragment}}}))
    events.append(ClaudeEvent("stream_event", "", {"event": {"type": "content_block_stop", "index": index}}))
    envelope = {"message": message, "tool_calls": [{"name": "grid_echo", "arguments": {"x": 1}}]}
    events.append(ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                                    "result": "", "structured_output": envelope}))
    return events


def _text_deltas_of(events):
    return [e.delta for e in events if getattr(e, "type", "") == "response.output_text.delta"]


def test_a_tool_turn_streams_its_message_before_the_result():
    fragments = ['{"message": "Let me ', 'check.", "tool_calls": [{"name": "grid_echo", ', '"arguments": {"x": 1}}]}']
    model, _ = _model(_envelope_events(fragments, "Let me check."))
    events = asyncio.run(_stream(model, tools=[grid_echo]))
    assert _text_deltas_of(events) == ["Let me ", "check."]

    model, _ = _model(_envelope_events(fragments, "Let me check."))
    response = asyncio.run(_get(model, tools=[grid_echo]))
    assert _text(response) == "Let me check."  # streamed once, not repeated by the result
    assert [c.name for c in _calls(response)] == ["grid_echo"]


def test_a_validated_message_that_differs_from_the_draft_follows_it():
    fragments = ['{"message": "Draft", "tool_calls": []}']
    model, _ = _model(_envelope_events(fragments, "Final"))
    response = asyncio.run(_get(model, tools=[grid_echo]))
    assert _text(response) == "Draft\n\nFinal"


def test_a_structured_final_turn_does_not_stream_the_message():
    class Final(BaseModel):
        answer: str

    fragments = ['{"message": "hi", "tool_calls": [], "final": {"answer": "done"}}']
    events = _envelope_events(fragments, "hi")
    events[-1] = ClaudeEvent("result", "success", {"type": "result", "subtype": "success", "is_error": False,
                                                   "result": "", "structured_output": {
                                                       "message": "hi", "tool_calls": [], "final": {"answer": "done"}}})
    model, _ = _model(events)
    response = asyncio.run(_get(model, tools=[grid_echo], output_schema=AgentOutputSchema(Final)))
    assert _text(response) == '{"answer": "done"}'


# -- metering: a CLI run counts in the user's usage --------------------------

def test_the_cli_usage_reads_as_chat_completions_usage():
    from core.claude_cli import chat_usage
    from core.pricing import TokenUsage

    usage = chat_usage({"input_tokens": 3, "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 600,
                        "output_tokens": 50, "output_tokens_details": {"thinking_tokens": 20}})
    tokens = TokenUsage.from_payload(usage)
    assert (tokens.input, tokens.cached, tokens.cache_write, tokens.output, tokens.reasoning) == (4603, 4000, 600, 50, 20)


def test_each_run_reports_its_usage_to_the_meter():
    reported = []
    spawner = _Spawner()
    spawner.generation_processes.append(_FakeProcess(stdout_lines=[
        b'{"type":"result","subtype":"success","usage":{"input_tokens":10,"output_tokens":5}}',
    ]))
    runner = ClaudeCodeRunner(binary=sys.executable, spawn=spawner, model="claude-sonnet-5-5",
                              on_usage=lambda model, usage: reported.append((model, usage["prompt_tokens"], usage["completion_tokens"])))
    asyncio.run(_collect(runner.stream("hi")))
    assert reported == [("claude-sonnet-5-5", 10, 5)]


def test_a_failing_meter_never_breaks_the_run():
    spawner = _Spawner()
    spawner.generation_processes.append(_FakeProcess(stdout_lines=[
        b'{"type":"result","subtype":"success","usage":{"input_tokens":1,"output_tokens":1}}',
    ]))

    def broken(model, usage):
        raise RuntimeError("ledger down")

    runner = ClaudeCodeRunner(binary=sys.executable, spawn=spawner, on_usage=broken)
    assert [e.kind for e in asyncio.run(_collect(runner.stream("hi")))] == ["result"]


def test_a_cli_run_is_booked_as_a_subscription_call_of_the_user():
    from core.model_access import ModelAccess

    events = []
    provider = SimpleNamespace(auth="claude", claude_binary=None, base_url="cli://claude", price_source=None)
    model_config = SimpleNamespace(name="claude-sonnet-5-5", provider="claude-code", response_timeout=None, max_tokens=None)
    grid = SimpleNamespace(get_model=lambda key: model_config, get_provider=lambda key: provider,
                           get_working_directory=lambda: "/tmp/ws", config=SimpleNamespace(models={}))
    access = ModelAccess(on_spend=events.append)
    runner = ModelProvider(grid, SimpleNamespace(access=access), None).claude_runner(model_config, provider)
    runner._report_usage({"usage": {"input_tokens": 100, "output_tokens": 20}})
    [event] = events
    assert (event.provider, event.model, event.usage.input, event.usage.output) == ("claude-code", "claude-sonnet-5-5", 100, 20)
    assert event.subscription and not event.charged and event.source == "claude"
