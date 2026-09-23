import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

import agents
from agents.stream_events import RawResponsesStreamEvent

from core.agent_factory import AgentFactory
from core.config import Config
from core import agent_factory as agent_factory_module
from core.agent_factory import ConsoleStreamObserver
from core.tracing.config import ConsoleSpanExporter


class DummyRawDelta:
    def __init__(self, delta: str):
        self.delta = delta
        self.type = 'response.output_text.delta'


class DummyStream:
    """Mimics Runner.run_streamed return with .stream_events() and .final_output"""
    def __init__(self, events, final_output=None):
        self._events = events
        self.final_output = final_output

    async def stream_events(self):
        for e in self._events:
            await asyncio.sleep(0)  # yield control
            yield e


def _writer_collector():
    calls = []

    def writer(message, end="\n", flush=False):
        calls.append((message, end, flush))

    return calls, writer


@pytest.mark.asyncio
async def test_streaming_uses_buffer_when_final_output_empty(tmp_path):
    # Minimal config file
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
settings:
  default_agent: "test_agent"
  max_history: 5
  max_turns: 2
  agent_timeout: 30
  working_directory: "."
  config_directory: "."
  allow_path_override: true
  mcp_enabled: false
  agent_logging:
    enabled: false
providers:
  openai:
    name: "openai"
    base_url: "https://api.openai.com/v1"
models:
  gpt-4:
    name: "gpt-4"
    provider: "openai"
prompt_templates:
  base: |
    You are a helpful assistant.
agents:
  test_agent:
    name: "Test Agent"
    model: "gpt-4"
    tools: []
    base_prompt: "base"
    description: "Test agent"
  
""",
        encoding="utf-8",
    )

    factory = AgentFactory(Config(str(config_path)))

    # Dummy agent object
    dummy_agent = SimpleNamespace(name="Test Agent")
    with patch.object(factory, "create_agent", new=AsyncMock(return_value=dummy_agent)):
        # Build RawResponsesStreamEvent instances
        events = [
            RawResponsesStreamEvent(data=DummyRawDelta("Hello ")),
            RawResponsesStreamEvent(data=DummyRawDelta("World")),
        ]

        # Mock Runner.run_streamed to return dummy stream with empty final_output
        dummy_stream = DummyStream(events=events, final_output=None)

        class DummyRunner:
            @staticmethod
            def run_streamed(agent, message, context, max_turns, session):
                return dummy_stream

        with patch.object(agents, "Runner", DummyRunner):
            result = await factory.run_agent("test_agent", "hi", stream=True)
            assert result.startswith("Hello World")
            assert "ctx-" in result


@pytest.mark.asyncio
async def test_streaming_prefers_final_output_over_buffer(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
settings:
  default_agent: "test_agent"
  max_history: 5
  max_turns: 2
  agent_timeout: 30
  working_directory: "."
  config_directory: "."
  allow_path_override: true
  mcp_enabled: false
  agent_logging:
    enabled: false
providers:
  openai:
    name: "openai"
    base_url: "https://api.openai.com/v1"
models:
  gpt-4:
    name: "gpt-4"
    provider: "openai"
prompt_templates:
  base: |
    You are a helpful assistant.
agents:
  test_agent:
    name: "Test Agent"
    model: "gpt-4"
    tools: []
    base_prompt: "base"
    description: "Test agent"
  
""",
        encoding="utf-8",
    )

    factory = AgentFactory(Config(str(config_path)))
    dummy_agent = SimpleNamespace(name="Test Agent")

    with patch.object(factory, "create_agent", new=AsyncMock(return_value=dummy_agent)):
        events = [RawResponsesStreamEvent(data=DummyRawDelta("buffered text that should be ignored"))]
        dummy_stream = DummyStream(events=events, final_output="FINAL")

        class DummyRunner:
            @staticmethod
            def run_streamed(agent, message, context, max_turns, session):
                return dummy_stream

        with patch.object(agents, "Runner", DummyRunner):
            result = await factory.run_agent("test_agent", "hi", stream=True)
            assert result.startswith("FINAL")
            assert "ctx-" in result


def test_console_stream_observer_can_buffer_text_without_printing():
    calls, writer = _writer_collector()
    observer = ConsoleStreamObserver(output_writer=writer, render_text_deltas=False)

    event = RawResponsesStreamEvent(data=DummyRawDelta("Hello buffered world"))
    fragment = observer.handle_event(event)

    assert fragment == "Hello buffered world"
    assert calls == []


def test_console_stream_observer_formats_tool_calls(monkeypatch):
    calls, writer = _writer_collector()
    observer = ConsoleStreamObserver(output_writer=writer)

    class DummyRunItemStreamEvent:
        def __init__(self, name, item):
            self.name = name
            self.item = item

    monkeypatch.setattr(agent_factory_module, "RunItemStreamEvent", DummyRunItemStreamEvent)

    raw_item = SimpleNamespace(
        name="search_code",
        arguments={"query": "todo", "limit": 3},
        server_label="fs",
    )
    item = SimpleNamespace(raw_item=raw_item)
    observer.handle_event(DummyRunItemStreamEvent("tool_called", item))

    rendered = "".join(message for message, _, _ in calls)
    assert "[tool] fs.search_code" in rendered
    assert "query=todo" in rendered


def test_console_stream_observer_passes_agent_and_duration_to_renderer(monkeypatch):
    rendered = []

    class FakeRenderer:
        def print_tool_call(self, tool_name, args_preview="", *, agent_name=None, duration=None):
            rendered.append(("call", tool_name, args_preview, agent_name, duration))

        def print_tool_output(self, tool_name, output_preview, *, agent_name=None, duration=None):
            rendered.append(("output", tool_name, output_preview, agent_name, duration))

    observer = ConsoleStreamObserver(renderer=FakeRenderer())

    class DummyRunItemStreamEvent:
        def __init__(self, name, item):
            self.name = name
            self.item = item

    monkeypatch.setattr(agent_factory_module, "RunItemStreamEvent", DummyRunItemStreamEvent)

    raw_item = SimpleNamespace(
        name="grep_tool",
        arguments={"pattern": "volume"},
        server_label=None,
    )
    item = SimpleNamespace(raw_item=raw_item, output="ok")

    observer.handle_event(DummyRunItemStreamEvent("tool_called", item), agent_key="executor")
    assert rendered[0] == ("call", "grep_tool", "pattern=volume", "executor", None)

    observer.handle_event(DummyRunItemStreamEvent("tool_output", item), agent_key="executor")

    assert rendered[1][0:4] == ("output", "grep_tool", "ok", "executor")
    assert rendered[1][4]


def test_console_stream_observer_matches_tool_output_by_call_id(monkeypatch):
    rendered = []

    class FakeRenderer:
        def print_tool_call(self, tool_name, args_preview="", *, agent_name=None, duration=None):
            rendered.append(("call", tool_name, args_preview, agent_name, duration))

        def print_tool_output(self, tool_name, output_preview, *, agent_name=None, duration=None):
            rendered.append(("output", tool_name, output_preview, agent_name, duration))

    observer = ConsoleStreamObserver(renderer=FakeRenderer())

    class DummyRunItemStreamEvent:
        def __init__(self, name, item):
            self.name = name
            self.item = item

    monkeypatch.setattr(agent_factory_module, "RunItemStreamEvent", DummyRunItemStreamEvent)

    call_raw = SimpleNamespace(
        name="glob_tool",
        arguments={"pattern": "**/*"},
        call_id="call_1",
    )
    output_raw = SimpleNamespace(
        type="function_call_output",
        call_id="call_1",
        output="Found 50 results",
    )

    observer.handle_event(
        DummyRunItemStreamEvent("tool_called", SimpleNamespace(raw_item=call_raw)),
        agent_key="coordinator",
    )
    assert rendered[0] == ("call", "glob_tool", "pattern=**/*", "coordinator", None)

    observer.handle_event(
        DummyRunItemStreamEvent("tool_output", SimpleNamespace(raw_item=output_raw)),
        agent_key="coordinator",
    )

    assert rendered[1][0:4] == ("output", "glob_tool", "Found 50 results", "coordinator")
    assert rendered[1][4]


def test_console_span_exporter_suppresses_compact_function_lines(capsys):
    exporter = ConsoleSpanExporter("INFO")

    exporter._print_span(
        {
            "span_data": {"type": "function", "name": "grep_tool"},
            "started_at": "2026-05-19T18:00:00Z",
            "ended_at": "2026-05-19T18:00:00.123Z",
        }
    )

    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_run_agent_object_simple_streams_dynamic_agent_tool_calls(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
settings:
  default_agent: "test_agent"
  max_history: 5
  max_turns: 2
  agent_timeout: 30
  working_directory: "."
  config_directory: "."
  allow_path_override: true
  mcp_enabled: false
  agent_logging:
    enabled: false
providers:
  openai:
    name: "openai"
    base_url: "https://api.openai.com/v1"
models:
  gpt-4:
    name: "gpt-4"
    provider: "openai"
prompt_templates:
  base: |
    You are a helpful assistant.
agents:
  test_agent:
    name: "Test Agent"
    model: "gpt-4"
    tools: []
    base_prompt: "base"
    description: "Test agent"
""",
        encoding="utf-8",
    )

    seen = []

    class FakeObserver:
        def handle_event(self, event, *, agent_key=None):
            seen.append((getattr(event, "name", ""), agent_key))
            return None

    class DummyRunItemStreamEvent:
        def __init__(self, name, item):
            self.name = name
            self.item = item

    monkeypatch.setattr(agent_factory_module, "RunItemStreamEvent", DummyRunItemStreamEvent)

    raw_item = SimpleNamespace(name="glob_tool", arguments={"pattern": "**/*"}, call_id="call_1")
    events = [DummyRunItemStreamEvent("tool_called", SimpleNamespace(raw_item=raw_item))]
    dummy_stream = DummyStream(events=events, final_output="done")

    class DummyRunner:
        @staticmethod
        def run_streamed(**kwargs):
            return dummy_stream

    factory = AgentFactory(Config(str(config_path)), stream_observer=FakeObserver())
    monkeypatch.setattr(agents, "Runner", DummyRunner)

    result = await factory.run_agent_object_simple(SimpleNamespace(name="executor-123"), "task")

    assert result == "done"
    assert ("tool_called", "executor-123") in seen

    # A caller's view takes the run instead: the dynamic agent reports into the
    # trace of the turn that launched it, not the factory's console.
    seen.clear()
    caller_seen = []

    class CallerObserver:
        def handle_event(self, event, *, agent_key=None):
            caller_seen.append((getattr(event, "name", ""), agent_key))
            return None

    result = await factory.run_agent_object_simple(
        SimpleNamespace(name="executor-123"), "task", stream_observer=CallerObserver()
    )

    assert result == "done"
    assert caller_seen == [("tool_called", "executor-123")]
    assert seen == []


@pytest.mark.asyncio
async def test_sub_agent_runs_under_a_delegated_policy_state(tmp_path, monkeypatch):
    from agents.tool_context import ToolContext
    from core.action_policy import ActionRunState

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
settings:
  default_agent: "test_agent"
  max_turns: 2
  working_directory: "."
  config_directory: "."
  mcp_enabled: false
  agent_logging:
    enabled: false
providers:
  openai:
    name: "openai"
    base_url: "https://api.openai.com/v1"
models:
  gpt-4:
    name: "gpt-4"
    provider: "openai"
prompt_templates:
  base: |
    You are a helpful assistant.
agents:
  test_agent:
    name: "Test Agent"
    model: "gpt-4"
    tools: []
    base_prompt: "base"
    description: "Test agent"
""",
        encoding="utf-8",
    )
    started = {}

    class DummyRunner:
        @staticmethod
        def run_streamed(**kwargs):
            started.update(kwargs)
            return DummyStream(events=[], final_output="report")

    monkeypatch.setattr(agents, "Runner", DummyRunner)
    factory = AgentFactory(Config(str(config_path)))
    tool = factory._create_context_aware_agent_tool(
        "test_agent", SimpleNamespace(name="Test Agent"), "call_test_agent", "Delegate"
    )
    caller = ActionRunState(task="Review the diff")
    ctx = ToolContext(
        context=SimpleNamespace(action_state=caller, action_depth=0),
        tool_name="call_test_agent",
        tool_call_id="call-1",
        tool_arguments='{"input": "Check core/"}',
    )

    await tool.on_invoke_tool(ctx, '{"input": "Check core/"}')

    state = started["context"].action_state
    # The caller's task and trajectory; its request is context, not authority.
    assert state.parent is caller
    assert state.task == "Review the diff"
    assert state.chain is caller.chain
    assert state.delegation == {"tool": "call_test_agent", "request": "Check core/"}
    assert started["context"].action_depth == 1
