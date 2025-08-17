import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

import agents
from agents.stream_events import RawResponsesStreamEvent

from core.agent_factory import AgentFactory
from core.config import Config


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
            assert result == "Hello World"


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
            assert result == "FINAL" 