"""Behavior of AgentFactory.run_agent: one conversation turn, end to end.

The SDK Runner is replaced by a scripted fake; everything else - context,
sessions, instructions, retries - is the real factory.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import agents
import httpx
import pytest
import yaml
from agents import SQLiteSession

from core.agent_factory import TOOL_CALL_CORRECTION, AgentFactory
from core.config import Config


@pytest.fixture
def factory(tmp_path):
    config = {
        "settings": {
            "default_agent": "worker",
            "max_turns": 5,
            "agent_timeout": 30,
            "working_directory": str(tmp_path / "work"),
            "logs_directory": str(tmp_path / "logs"),
            "mcp_enabled": False,
            "agent_logging": {"enabled": False},
        },
        "providers": {"p": {"name": "p", "base_url": "https://llm.test/v1", "api_key": "test"}},
        "models": {"m": {"name": "m", "provider": "p", "context_window": 100000}},
        "prompt_templates": {"base": "You help."},
        "agents": {
            "worker": {"name": "Worker", "model": "m", "tools": [], "base_prompt": "base"},
            "reviewer": {"name": "Reviewer", "model": "m", "tools": [], "base_prompt": "base"},
        },
    }
    (tmp_path / "work").mkdir()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    factory = AgentFactory(Config(str(path)), tracing_level=None)
    sessions = {}
    factory._get_agent_session = lambda agent_key, context_id: sessions.setdefault(
        (agent_key, context_id), SQLiteSession(f"{agent_key}-{context_id}")
    )
    factory.sessions = sessions
    return factory


class ScriptedRunner:
    """Answers each run with the next scripted reply; records what it was given."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def run(self, agent, run_input, *, context, max_turns, session):
        self.calls.append(SimpleNamespace(agent=agent, input=run_input, session=session))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return await reply()
        if session is not None:
            # What the SDK does: the turn becomes part of the agent's session.
            await session.add_items(
                [{"role": "user", "content": str(run_input)}, {"role": "assistant", "content": reply}]
            )
        return SimpleNamespace(final_output=reply, new_items=[])


def use(runner):
    return patch.object(agents, "Runner", runner)


async def test_the_answer_ends_with_its_context_id_once(factory):
    runner = ScriptedRunner("Done.")
    with use(runner):
        answer = await factory.run_agent("worker", "Fix it")

    context_id = factory.context_manager.get_current_context_id()
    assert answer == f"Done.\n\nContext ID: {context_id}"

    runner = ScriptedRunner(f"Again.\n\nContext ID: {context_id}")
    with use(runner):
        answer = await factory.run_agent("worker", "More", context_id=context_id)
    assert answer.count("Context ID:") == 1


async def test_history_reaches_the_model_through_one_channel(factory):
    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Start")
    context_id = factory.context_manager.get_current_context_id()

    built = []
    original = factory._build_agent_instructions

    def spy(agent_key, context_path=None, include_conversation_context=True):
        built.append((agent_key, include_conversation_context))
        return original(agent_key, context_path, include_conversation_context=include_conversation_context)

    factory._build_agent_instructions = spy
    with use(ScriptedRunner("Second.", "Review.")):
        # The worker's own session holds the conversation: no transcript.
        await factory.run_agent("worker", "Continue", context_id=context_id)
        # The reviewer joins it with an empty session: it gets the transcript.
        await factory.run_agent("reviewer", "Review it", context_id=context_id)

    # The last build per agent is the one its run used (creating an agent builds too).
    assert dict(built) == {"worker": False, "reviewer": True}


async def test_a_transient_provider_error_is_retried(factory):
    runner = ScriptedRunner(httpx.ConnectError("reset"), "Recovered.")
    with use(runner), patch("asyncio.sleep", new=AsyncMock()) as sleep:
        answer = await factory.run_agent("worker", "Go")

    assert answer.startswith("Recovered.")
    assert len(runner.calls) == 2
    sleep.assert_awaited_once()


async def test_the_agent_timeout_ends_the_run_without_repeating_it(factory):
    async def hang():
        await asyncio.Event().wait()

    runner = ScriptedRunner(hang)
    factory.config.get_agent_timeout = lambda: 0.05
    with use(runner):
        answer = await factory.run_agent("worker", "Go")

    assert "no answer within settings.agent_timeout" in answer
    assert len(runner.calls) == 1


async def test_an_empty_answer_says_so(factory):
    with use(ScriptedRunner("")):
        answer = await factory.run_agent("worker", "Go")
    assert "finished without a written report" in answer


async def test_text_tool_calls_are_retried_with_a_correction(factory):
    observer = SimpleNamespace(handle_event=lambda event, agent_key=None: None)
    runner = ScriptedRunner(
        "<tool_call><function=file_read><parameter=path>a</parameter></function></tool_call>",
        "Read it.",
    )
    with use(runner):
        answer = await factory.run_agent("worker", "Read a", stream_observer=observer)

    assert answer.startswith("Read it.")
    assert TOOL_CALL_CORRECTION in str(runner.calls[1].input)
    assert runner.calls[1].session is runner.calls[0].session


async def test_images_switch_the_turn_to_explicit_history(factory):
    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Hello")
    context_id = factory.context_manager.get_current_context_id()
    image_message = (
        '{"role": "user", "content": [{"type": "input_text", "text": "What is this?"},'
        ' {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}'
    )

    runner = ScriptedRunner("A cat.")
    with use(runner):
        await factory.run_agent("worker", image_message, context_id=context_id)

    call = runner.calls[0]
    assert call.session is None
    assert [m["role"] for m in call.input] == ["user", "assistant", "user"]
    assert call.input[-1]["content"][1]["type"] == "input_image"
    assert factory.context_manager.history_has_images()


async def test_plain_json_text_is_a_message_not_sdk_input(factory):
    runner = ScriptedRunner("Parsed.")
    with use(runner):
        await factory.run_agent("worker", '{"a": 1}')
    assert runner.calls[0].input == '{"a": 1}'


async def test_compaction_replaces_history_and_the_agents_session(factory):
    from core.compact import CompactMessage

    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Start")
    context_id = factory.context_manager.get_current_context_id()
    summary = CompactMessage(role="user", content="Summary: the user started a task.")
    outcome = {
        "was_compacted": True,
        "consecutive_failures": 0,
        "compaction_result": SimpleNamespace(
            compacted_messages=[summary], tokens_before=90000, tokens_after=100
        ),
    }
    over_threshold = SimpleNamespace(is_above_auto_compact_threshold=True)
    runner = ScriptedRunner("Continued.")
    with use(runner), patch(
        "core.agent_factory.calculate_token_warning_state", return_value=over_threshold
    ), patch("core.agent_factory.auto_compact_if_needed", new=AsyncMock(return_value=outcome)), patch.object(
        factory, "_get_compact_client_and_model", return_value=(object(), "m")
    ):
        await factory.run_agent("worker", "Go on", context_id=context_id)

    session_items = await runner.calls[0].session.get_items()
    assert session_items[0]["content"] == "Summary: the user started a task."
    history = factory.context_manager.conversation_snapshot()
    assert history[0].content == "Summary: the user started a task."


async def test_a_context_overflow_trims_history_and_retries_once(factory):
    from core.compact.reactive_compact import ReactiveCompactResult, ReactiveCompactStatus

    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Start")
    context_id = factory.context_manager.get_current_context_id()
    trimmed = ReactiveCompactResult(status=ReactiveCompactStatus.TRIMMED, messages=[])
    runner = ScriptedRunner(
        RuntimeError("context_length_exceeded"),
        RuntimeError("context_length_exceeded"),
        "unused",
    )
    with use(runner), patch(
        "core.agent_factory.reactive_compact_on_prompt_too_long",
        new=AsyncMock(return_value=trimmed),
    ) as trim:
        with pytest.raises(Exception, match="context_length_exceeded"):
            await factory.run_agent("worker", "Go on", context_id=context_id)

    trim.assert_awaited_once()
    assert len(runner.calls) == 2


def test_the_model_whitelist_allows_only_listed_models(factory):
    assert factory._is_model_allowed("anything")  # no list: every model
    factory.config.config.settings.allowed_models = ["m"]
    assert factory._is_model_allowed("m")
    assert not factory._is_model_allowed("other")
