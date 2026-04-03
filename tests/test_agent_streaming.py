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
