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
from core.compact import COMPACTED_TYPE


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

    async def run(self, agent, run_input, *, context, max_turns, session, run_config=None):
        self.calls.append(
            SimpleNamespace(agent=agent, input=run_input, session=session, run_config=run_config)
        )
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
    original = factory.instructions_builder.assemble_model_context

    def spy(agent_key, context_path=None, **kwargs):
        built.append((agent_key, kwargs["include_conversation_context"]))
        return original(agent_key, context_path, **kwargs)

    factory.instructions_builder.assemble_model_context = spy
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
    factory.config.get_agent_timeout = lambda agent_key=None: 0.05
    with use(runner):
        answer = await factory.run_agent("worker", "Go")

    assert "no answer within the agent timeout" in answer
    assert len(runner.calls) == 1


async def test_an_empty_answer_says_so(factory):
    with use(ScriptedRunner("", "")):
        answer = await factory.run_agent("worker", "Go")
    assert "Model returned no final written report" in answer
    assert factory.context_manager.pending_interruption(factory.context_manager.get_current_context_id())


def cut_off(output_tokens):
    """A run whose last response filled its output budget with thinking."""
    async def reply():
        usage = SimpleNamespace(output_tokens=output_tokens)
        return SimpleNamespace(final_output="", new_items=[], raw_responses=[SimpleNamespace(usage=usage)])

    return reply


async def test_an_answer_spent_on_thinking_names_the_limit(factory):
    limit = factory.config.get_model("m").max_tokens
    runner = ScriptedRunner(cut_off(limit), cut_off(limit))
    with use(runner):
        answer = await factory.run_agent("worker", "Design the system")

    assert len(runner.calls) == 2  # retried once, like any missing answer
    assert f"answer budget (max_tokens {limit}) on reasoning" in answer
    assert "raise the model's max_tokens or lower its reasoning effort" in answer


async def test_a_short_empty_answer_is_not_called_a_spent_budget(factory):
    with use(ScriptedRunner(cut_off(12), cut_off(12))):
        answer = await factory.run_agent("worker", "Go")
    assert "Model returned no final written report" in answer
    assert "answer budget" not in answer


async def test_empty_answer_recovers_in_same_session(factory):
    runner = ScriptedRunner("", "Verified result.")
    with use(runner):
        answer = await factory.run_agent("worker", "Go")
    assert answer.startswith("Verified result.")
    assert runner.calls[0].session is runner.calls[1].session
    assert "do not repeat completed actions" in runner.calls[1].input
    messages = factory.context_manager.conversation_snapshot()
    assert len([m for m in messages if m.role == "user"]) == 1


async def test_missing_terminal_response_recovers_with_saved_tool_results(factory):
    from agents.exceptions import ModelBehaviorError

    class InterruptedRunner(ScriptedRunner):
        async def run(self, agent, run_input, **kwargs):
            if not self.calls:
                await kwargs["session"].add_items([
                    {"type": "function_call", "call_id": "write1", "name": "write_file", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "write1", "output": "saved result.pdf"},
                ])
            else:
                saved = await kwargs["session"].get_items()
                assert any(item.get("output") == "saved result.pdf" for item in saved)
            return await super().run(agent, run_input, **kwargs)

    runner = InterruptedRunner(ModelBehaviorError("Model did not produce a final response!"), "Created result.pdf")
    with use(runner):
        answer = await factory.run_agent("worker", "Create PDF")
    assert answer.startswith("Created result.pdf")
    assert len(runner.calls) == 2
    assert runner.calls[0].session is runner.calls[1].session


async def test_missing_terminal_response_recovery_is_bounded(factory):
    from agents.exceptions import ModelBehaviorError
    runner = ScriptedRunner(*[ModelBehaviorError("Model did not produce a final response!") for _ in range(2)])
    with use(runner):
        answer = await factory.run_agent("worker", "Go")
    assert "Model did not produce a final response!" in answer
    assert len(runner.calls) == 2


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


async def test_images_reach_the_model_through_the_agents_session(factory):
    """The session keeps tool calls between turns; images no longer bypass it."""
    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Hello")
    context_id = factory.context_manager.get_current_context_id()
    image_message = (
        '{"role": "user", "content": [{"type": "input_text", "text": "What is this?"},'
        ' {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}'
    )

    runner = ScriptedRunner("A cat.", "Still a cat.")
    with use(runner):
        await factory.run_agent("worker", image_message, context_id=context_id)
        await factory.run_agent("worker", "And now?", context_id=context_id)

    first, second = runner.calls
    assert first.session is factory.sessions[("worker", context_id)]
    # Only the new message: the session supplies the history.
    assert [m["role"] for m in first.input] == ["user"]
    assert first.input[0]["content"][1]["type"] == "input_image"
    assert second.session is first.session
    assert second.input == "And now?"


async def test_reasoning_replay_models_keep_their_session(factory):
    """preserve_reasoning_content no longer drops the tool history between turns."""
    factory.config.get_model("m").preserve_reasoning_content = True
    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Start")
    context_id = factory.context_manager.get_current_context_id()

    runner = ScriptedRunner("Second.")
    with use(runner):
        await factory.run_agent("worker", "Go on", context_id=context_id)

    assert runner.calls[0].session is factory.sessions[("worker", context_id)]
    assert runner.calls[0].input == "Go on"


async def test_plain_json_text_is_a_message_not_sdk_input(factory):
    runner = ScriptedRunner("Parsed.")
    with use(runner):
        await factory.run_agent("worker", '{"a": 1}')
    assert runner.calls[0].input == '{"a": 1}'


def summarized(text="Summary: the user started a task."):
    """What compact_conversation returns when the summary succeeds."""
    from core.compact import CompactMessage

    return SimpleNamespace(
        success=lambda: True,
        summary_messages=[CompactMessage(role="user", content=text)],
        user_display_message=None, error_message=None, status=SimpleNamespace(value="success"),
    )


async def test_a_session_past_the_threshold_is_summarized_and_the_chat_is_kept(factory):
    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Start")
    context_id = factory.context_manager.get_current_context_id()
    session = factory.sessions[("worker", context_id)]
    # A long tool result makes the session large.
    await session.add_items([
        {"type": "function_call", "call_id": "c1", "name": "file_read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "x" * 400_000},
    ])
    chat_before = [m.content for m in factory.context_manager.conversation_snapshot()]

    compact = AsyncMock(return_value=summarized())
    runner = ScriptedRunner("Continued.")
    shown = []
    observer = SimpleNamespace(handle_compaction=lambda before, after: shown.append((before, after)))
    with use(runner), patch("core.factory.sessions.compact_conversation", new=compact), patch.object(
        factory.models, "compact_client_and_model", return_value=(object(), "m")
    ):
        await factory.run_agent("worker", "Go on", context_id=context_id, stream_observer=observer)

    transcript = compact.await_args.kwargs["messages"]
    assert any("[Result of file_read:" in m.get_text() for m in transcript)
    session_items = await runner.calls[0].session.get_items()
    assert session_items[0]["content"] == "Summary: the user started a task."
    # The visible chat is not compacted: it only grows.
    chat_after = [m.content for m in factory.context_manager.conversation_snapshot()]
    assert chat_after[: len(chat_before)] == chat_before
    # Where the session was rewritten, the chat gets a display-only marker so the
    # thread does not silently skip a stretch of history.
    markers = [
        m
        for m in factory.context_manager.conversation_snapshot()
        if (m.metadata or {}).get("type") == COMPACTED_TYPE
    ]
    assert len(markers) == 1
    assert markers[0].role == "assistant"
    assert markers[0].metadata["tokens_before"] > markers[0].metadata["tokens_after"]
    # The running turn shows it too, not only the thread once reloaded.
    assert shown == [(markers[0].metadata["tokens_before"], markers[0].metadata["tokens_after"])]


async def test_a_session_within_the_threshold_is_left_alone(factory):
    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Start")
    context_id = factory.context_manager.get_current_context_id()
    compact = AsyncMock(return_value=summarized())
    with use(ScriptedRunner("Second.")), patch("core.factory.sessions.compact_conversation", new=compact):
        await factory.run_agent("worker", "Go on", context_id=context_id)
    compact.assert_not_awaited()


async def test_compaction_stops_after_repeated_failures(factory):
    context_id = factory.context_manager.start_new_context()
    session = factory._get_agent_session("worker", context_id)
    await session.add_items([{"role": "user", "content": "x" * 400_000}])
    compact = AsyncMock(side_effect=RuntimeError("summary model down"))
    limit = factory.compact_config.auto.max_consecutive_failures
    with patch("core.factory.sessions.compact_conversation", new=compact), patch.object(
        factory.models, "compact_client_and_model", return_value=(object(), "m")
    ):
        for _ in range(limit + 2):
            assert await factory.compact_session("worker", context_id) is None
    assert compact.await_count == limit
    # An explicit /compact still tries.
    with patch("core.factory.sessions.compact_conversation", new=AsyncMock(return_value=summarized())), patch.object(
        factory.models, "compact_client_and_model", return_value=(object(), "m")
    ):
        assert await factory.compact_session("worker", context_id, force=True)


async def test_a_context_overflow_summarizes_the_session_and_retries_once(factory):
    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Start")
    context_id = factory.context_manager.get_current_context_id()
    runner = ScriptedRunner(
        RuntimeError("context_length_exceeded"),
        RuntimeError("context_length_exceeded"),
        "unused",
    )
    compact = AsyncMock(return_value=summarized())
    with use(runner), patch("core.factory.sessions.compact_conversation", new=compact), patch.object(
        factory.models, "compact_client_and_model", return_value=(object(), "m")
    ):
        with pytest.raises(Exception, match="context_length_exceeded"):
            await factory.run_agent("worker", "Go on", context_id=context_id)

    compact.assert_awaited_once()
    assert len(runner.calls) == 2
    # Nothing of the failed attempt was stored, so the request is sent again.
    assert runner.calls[1].input == "Go on"


async def test_every_model_call_clears_old_tool_outputs_past_the_budget(factory):
    factory.compact_config.micro.compactable_tools = ["file_read"]
    factory.compact_config.micro.preserve_last_n = 1
    runner = ScriptedRunner("Done.")
    with use(runner):
        await factory.run_agent("worker", "Go")
    from agents.run import CallModelData, ModelInputData

    items = []
    for n in range(4):
        items += [
            {"type": "function_call", "call_id": f"c{n}", "name": "file_read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"c{n}", "output": "y" * 200_000},
        ]
    apply = runner.calls[0].run_config.call_model_input_filter
    sent = apply(CallModelData(model_data=ModelInputData(input=items, instructions="Be brief."), agent=None, context=None)).input
    outputs = [item["output"] for item in sent if item["type"] == "function_call_output"]
    assert outputs[-1] == "y" * 200_000  # the newest is kept
    assert all(output.startswith("[Output of file_read cleared") for output in outputs[:-1])
    assert items[1]["output"] == "y" * 200_000  # the session's items are untouched


def test_the_model_whitelist_allows_only_listed_models(factory):
    assert factory.models.is_allowed("anything")  # no list: every model
    factory.config.config.settings.allowed_models = ["m"]
    assert factory.models.is_allowed("m")
    assert not factory.models.is_allowed("other")


async def test_a_dynamic_agent_stops_at_the_agent_timeout_and_reports_it(factory):
    class Hanging:
        new_items = []

        async def stream_events(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    class HangingRunner:
        calls = 0

        @classmethod
        def run_streamed(cls, **kwargs):
            cls.calls += 1
            return Hanging()

    factory.config.get_agent_timeout = lambda agent_key=None: 0.05
    with use(HangingRunner):
        answer = await factory.run_agent_object_simple(SimpleNamespace(name="executor-1"), "task")

    assert "no answer within the agent timeout (0.05 s)" in answer
    assert HangingRunner.calls == 1
