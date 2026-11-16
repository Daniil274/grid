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


class RecordingRunner(ScriptedRunner):
    """Records what each model call reads before the turn writes to the session."""

    def __init__(self, *replies):
        super().__init__(*replies)
        self.seen = []

    async def run(self, agent, run_input, *, context, max_turns, session, run_config=None):
        items = await session.get_items() if session is not None else []
        self.seen.append(SimpleNamespace(agent=agent, input=str(run_input), items=items))
        return await super().run(
            agent, run_input, context=context, max_turns=max_turns,
            session=session, run_config=run_config,
        )


async def test_the_answer_carries_no_service_context_id(factory):
    """The answer is the model's text: the app appends no service footer."""
    runner = ScriptedRunner("Done.")
    with use(runner):
        answer = await factory.run_agent("worker", "Fix it")

    assert answer == "Done."

    # A legacy model-written footer is passed through untouched, once; the
    # projections that reach the model strip it (test_context).
    context_id = factory.context_manager.get_current_context_id()
    runner = ScriptedRunner(f"Again.\n\nContext ID: {context_id}")
    with use(runner):
        answer = await factory.run_agent("worker", "More", context_id=context_id)
    assert answer.count("Context ID:") == 1


async def test_a_legacy_session_footer_is_kept_not_destructively_rewritten(factory):
    """The automatic legacy-history rewrite is a safe no-op.

    Footer text does not prove service provenance and the SDK Session has no
    atomic compare-and-replace, so stored items (footer included) must survive
    untouched rather than risk corrupting or losing history. The projections
    that do reach the model strip the footer (test_context).
    """
    context_id = factory.context_manager.get_current_context_id()
    session = factory._get_agent_session("worker", context_id)
    legacy = [
        {"role": "user", "content": "Start"},
        {"role": "assistant", "content": f"Old.\n\nContext ID: {context_id}"},
    ]
    await session.add_items(legacy)

    with use(ScriptedRunner("New.")):
        await factory.run_agent("worker", "Go", context_id=context_id)

    items = await factory._get_agent_session("worker", context_id).get_items()
    # Nothing destructive happened: the legacy answer and its footer are intact.
    assert "Old." in str(items)
    assert f"Context ID: {context_id}" in str(items)
    # The new turn still reaches the same stored session.
    assert "New." in str(items)


async def test_history_reaches_the_model_through_one_channel(factory):
    """History is delivered in the session, never twice and never in the prompt."""
    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "SECRET-A-DETAIL")
    context_id = factory.context_manager.get_current_context_id()

    built = []
    original = factory.instructions_builder.assemble_model_context

    def spy(agent_key, context_path=None, **kwargs):
        built.append((agent_key, kwargs["include_conversation_context"]))
        return original(agent_key, context_path, **kwargs)

    factory.instructions_builder.assemble_model_context = spy
    runner = RecordingRunner("Second.", "Review.")
    with use(runner):
        # The worker's own session holds the conversation: no transcript.
        await factory.run_agent("worker", "Continue", context_id=context_id)
        # The reviewer joins it: its empty session is seeded instead of a transcript.
        await factory.run_agent("reviewer", "Review it", context_id=context_id)

    # The last build per agent is the one its run used (creating an agent builds too).
    assert dict(built) == {"worker": False, "reviewer": False}
    joined = runner.seen[1]
    # The joining agent reads the conversation in its session, exactly once...
    assert str(joined.items).count("SECRET-A-DETAIL") == 1
    assert "SECRET-A-DETAIL" in str(joined.items)
    # ...and not as prompt sections beside it.
    assert "SECRET-A-DETAIL" not in joined.agent.instructions


async def test_a_joining_agents_next_turn_still_has_the_conversation(factory):
    """A -> B -> B: what B was told on joining is still there on its next turn."""
    runner = RecordingRunner("First.", "Review.", "More.")
    with use(runner):
        await factory.run_agent("worker", "SECRET-A-DETAIL")
        context_id = factory.context_manager.get_current_context_id()
        await factory.run_agent("reviewer", "Review it", context_id=context_id)
        await factory.run_agent("reviewer", "Go on", context_id=context_id)

    second = runner.seen[2]
    # The conversation the reviewer joined on is in its session, once.
    assert str(second.items).count("SECRET-A-DETAIL") == 1
    assert "Review." in str(second.items)
    # Not repeated in the prompt, and no "meanwhile" block for what it saw.
    assert "SECRET-A-DETAIL" not in second.input
    assert "Meanwhile in this conversation" not in second.input


async def test_an_agent_that_comes_back_hears_what_the_other_agent_said(factory):
    """A -> B -> A: A keeps its own history and is told B's exchange, once."""
    runner = RecordingRunner("First.", "Review: keep the card.", "Third.")
    with use(runner):
        await factory.run_agent("worker", "SECRET-A-DETAIL")
        context_id = factory.context_manager.get_current_context_id()
        await factory.run_agent("reviewer", "Review it", context_id=context_id)
        await factory.run_agent("worker", "Go on", context_id=context_id)

    back = runner.seen[2]
    # Its own session still holds what it said, and none of the reviewer's answer.
    assert str(back.items).count("SECRET-A-DETAIL") == 1
    assert "Review: keep the card." not in str(back.items)
    # The reviewer's exchange reaches it as the one record of what it missed.
    assert back.input.count("Review: keep the card.") == 1
    assert "Agent reviewer:" in back.input
    # Its own words are not replayed to it.
    assert "SECRET-A-DETAIL" not in back.input


async def test_the_seed_keeps_the_whole_dialogue_and_compaction_follows_it(factory):
    """Seeding trims nothing; a long session is summarized, not cut by hand.

    The joining agent takes the whole stored dialogue, the earliest message
    included. A session past the auto-compact threshold then goes through the
    turn's own ``compact_session``: the normal path summarizes what was seeded,
    so the beginning of the conversation survives as summary instead of being
    silently dropped while seeding.
    """
    runner = RecordingRunner("First.", "Review.")
    with use(runner):
        await factory.run_agent("worker", "OLDEST-MESSAGE-THAT-IS-KEPT")
        context_id = factory.context_manager.get_current_context_id()
        await factory.run_agent("reviewer", "Review it", context_id=context_id)

    seeded = runner.seen[1].items
    # The whole projection is seeded: the earliest message is not dropped.
    assert "OLDEST-MESSAGE-THAT-IS-KEPT" in str(seeded)
    assert "First." in str(seeded)

    # A joining agent whose seeded session is past the threshold is compacted by
    # the standard path, and the summary is made from everything that was seeded.
    factory.models.compact_threshold = lambda agent_key, compact: 6  # a few tokens
    compact = AsyncMock(return_value=summarized())
    runner = RecordingRunner("Review too.")
    with use(runner), patch("core.factory.sessions.compact_conversation", new=compact):
        await factory.run_agent("reviewer", "Review again", context_id=context_id)

    transcript = compact.await_args.kwargs["messages"]
    assert any("OLDEST-MESSAGE-THAT-IS-KEPT" in m.get_text() for m in transcript)
    # The session the turn then runs on is the summary, as after any compaction.
    assert runner.seen[0].items[0]["content"] == "Summary: the user started a task."


async def test_a_session_is_seeded_only_from_its_own_conversation(factory):
    """Chats stay isolated: another conversation's history never seeds a session."""
    runner = RecordingRunner("One.", "Other.", "Two.")
    with use(runner):
        await factory.run_agent("worker", "SECRET-FIRST-CHAT")
        factory.context_manager.get_current_context_id()
        other = factory.context_manager.start_new_context()
        await factory.run_agent("reviewer", "hello", context_id=other)
        await factory.run_agent("worker", "hello again", context_id=other)

    assert "SECRET-FIRST-CHAT" not in str(runner.seen[1].items)
    assert "SECRET-FIRST-CHAT" not in str(runner.seen[2].items)
    assert "SECRET-FIRST-CHAT" not in runner.seen[2].input
    # The worker's session in the other chat holds that chat's reviewer turn.
    assert "Other." in str(runner.seen[2].items)


async def test_the_recorded_prompt_is_the_agent_that_answered_last(factory):
    """After a switch the inspector shows the running agent's prompt, not the first."""
    from core.config.prompt_sections import PromptSection

    factory.config.build_agent_prompt_sections = lambda agent_key: [
        PromptSection(key="base_prompt", content=f"PROMPT OF {agent_key}", scope="static")
    ]
    with use(ScriptedRunner("First.", "Review.")):
        await factory.run_agent("worker", "Go")
        context_id = factory.context_manager.get_current_context_id()
        await factory.run_agent("reviewer", "Review it", context_id=context_id)

    metadata = factory.context_manager.get_context_metadata(context_id)
    assert "PROMPT OF reviewer" in metadata["agent_instructions"]
    assert "PROMPT OF worker" not in metadata["agent_instructions"]
    assert metadata["last_context_assembly"]["metadata"]["agent_key"] == "reviewer"


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
    # An explicit /compact still tries, and resets the lockout it inherits:
    # the auto path tries again on its next turn, not stays locked out forever.
    with patch("core.factory.sessions.compact_conversation", new=AsyncMock(return_value=summarized())), patch.object(
        factory.models, "compact_client_and_model", return_value=(object(), "m")
    ):
        assert await factory.compact_session("worker", context_id, force=True)
    assert factory._compact_tracking.consecutive_failures == 0


async def test_a_request_past_the_threshold_compacts_the_session_mid_turn(factory):
    """A turn of many tool calls grows its session unchecked: a request that
    still passes the auto-compact threshold with its old tool outputs cleared
    is compacted in flight, and this call reads the summary."""
    from agents.run import CallModelData, ModelInputData

    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Start")
    context_id = factory.context_manager.get_current_context_id()
    session = factory._get_agent_session("worker", context_id)
    await session.add_items([
        {"type": "function_call", "call_id": "c1", "name": "file_read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "x" * 400_000},
    ])
    shown = []
    observer = SimpleNamespace(handle_compaction=lambda before, after: shown.append((before, after)))
    run_config = factory._run_config("worker", session=session, observer=observer, context_id=context_id)
    data = CallModelData(
        model_data=ModelInputData(input=await session.get_items(), instructions="Be brief."),
        agent=None, context=None,
    )

    with patch("core.factory.sessions.compact_conversation", new=AsyncMock(return_value=summarized())):
        sent = await run_config.call_model_input_filter(data)

    # This call reads the compacted session: the summary, the dialogue beside
    # it, and the newest tool results of the very steps it is running - not
    # the huge request.
    assert [item["content"] for item in sent.input if item.get("role")] == [
        "Summary: the user started a task.",
        "Start",
        "First.",
    ]
    assert [item.get("call_id") for item in sent.input if item.get("type") == "function_call"] == ["c1"]
    outputs = [item for item in sent.input if item.get("type") == "function_call_output"]
    assert [item.get("call_id") for item in outputs] == ["c1"]  # no orphan call or output
    assert len(outputs[0]["output"]) < 400_000  # a huge result is clipped, head and tail
    assert "more characters]" in outputs[0]["output"]
    assert await session.get_items() == sent.input
    from core.context_budget import request_tokens

    assert request_tokens(await session.get_items()) < factory.models.compact_threshold(
        "worker", factory.compact_config
    )
    # The running turn shows the step, the stored chat the marker - with the
    # summary it holds, for a session seeded after it.
    assert shown and shown[0][0] > shown[0][1]
    marker = [
        m
        for m in factory.context_manager.conversation_snapshot()
        if (m.metadata or {}).get("type") == COMPACTED_TYPE
    ][-1]
    assert marker.metadata["summary"] == "Summary: the user started a task."


async def test_compacting_one_agent_clears_the_other_agents_stale_sessions(factory):
    """Every agent of a conversation keeps a session of its own; compacting
    one clears the others', so a sibling re-seeds from the compacted
    conversation instead of replaying a stale pre-compaction history."""
    with use(ScriptedRunner("First.")):
        await factory.run_agent("worker", "Start")
    context_id = factory.context_manager.get_current_context_id()
    worker = factory._get_agent_session("worker", context_id)
    reviewer = factory._get_agent_session("reviewer", context_id)
    await reviewer.add_items([{"role": "user", "content": "the reviewer's own stale history"}])
    factory._agent_sessions[("worker", context_id)] = worker
    factory._agent_sessions[("reviewer", context_id)] = reviewer

    with patch("core.factory.sessions.compact_conversation", new=AsyncMock(return_value=summarized())):
        result = await factory.compact_session("worker", context_id, force=True)

    assert result["summary"] == "Summary: the user started a task."
    # The compaction keeps the dialogue beside the summary: the tool calls and
    # results fold into it, what was said does not.
    assert [item.get("content") for item in await worker.get_items()] == [
        "Summary: the user started a task.",
        "Start",
        "First.",
    ]
    assert await reviewer.get_items() == []


async def test_compaction_keeps_the_dialogue_inside_a_budget(factory):
    """The dialogue a compaction keeps beside its summary is bounded: the
    compacted session lands under the threshold and smaller than it was, the
    user's messages and the newest answers verbatim, an older long answer
    clipped head-and-tail."""
    context_id = factory.context_manager.start_new_context()
    session = factory._get_agent_session("worker", context_id)
    items = []
    for n in range(10):
        items.append({"role": "user", "content": f"question {n}"})
        items.append({"role": "assistant", "content": "a" * 10_000})
    # Tool noise the summary replaces is what made the session huge.
    items += [
        {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "x" * 400_000},
    ]
    await session.add_items(items)

    with patch("core.factory.sessions.compact_conversation", new=AsyncMock(return_value=summarized())), patch.object(
        factory.models, "compact_client_and_model", return_value=(object(), "m")
    ):
        result = await factory.compact_session("worker", context_id)

    threshold = factory.models.compact_threshold("worker", factory.compact_config)
    assert result["tokens_before"] > threshold
    assert result["tokens_after"] < threshold
    assert result["tokens_after"] < result["tokens_before"]
    fresh = await session.get_items()
    contents = [item["content"] for item in fresh if item.get("role")]
    assert contents[0] == "Summary: the user started a task."
    # Every user message survives verbatim, the newest answers whole.
    assert [c for c in contents if c.startswith("question")] == [f"question {n}" for n in range(10)]
    assert contents[-2:] == ["question 9", "a" * 10_000]
    # Clipping stops once the dialogue fits: only the oldest answers shrink.
    assert contents.count("a" * 10_000) == 8
    assert any("more characters]" in c for c in contents)
    # The tool calls and results are gone: the summary holds them.
    assert [item for item in fresh if item.get("type") in ("function_call", "function_call_output")] == []


async def test_a_session_joining_after_a_compaction_seeds_from_the_marker(factory):
    """An agent joining a compacted conversation seeds from the last marker:
    the summary it holds, then the conversation's dialogue - what the
    compaction kept beside its summary and what was said since - never the
    tool calls and results the summary replaced."""
    with use(RecordingRunner("First.")):
        await factory.run_agent("worker", "OLDEST-BEFORE-COMPACTION")
        context_id = factory.context_manager.get_current_context_id()
    with patch("core.factory.sessions.compact_conversation", new=AsyncMock(return_value=summarized())):
        await factory.compact_session("worker", context_id, force=True)

    # The marker sits at the end of the chat: a turn stored after it is read as
    # itself, the compacted part through the summary.
    with use(RecordingRunner("After.")):
        await factory.run_agent("worker", "SENT-AFTER-COMPACTION", context_id=context_id)

    reviewer = factory._get_agent_session("reviewer", context_id)
    assert await factory._seed_session(reviewer, context_id, "reviewer")
    items = str(await reviewer.get_items())
    assert "Summary: the user started a task." in items
    assert "SENT-AFTER-COMPACTION" in items
    assert "After." in items
    # The dialogue from before the compaction is preserved: the participants'
    # own words do not fall out of what a joining agent reads.
    assert "OLDEST-BEFORE-COMPACTION" in items
    assert "First." in items
    # The seeding runs once: the dialogue the summary already covers is not
    # duplicated by a second copy of the marker's summary.
    assert items.count("Summary: the user started a task.") == 1


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
    sent = apply(
        CallModelData(model_data=ModelInputData(input=items, instructions="Be brief."), agent=None, context=None)
    ).input
    outputs = [item["output"] for item in sent if item["type"] == "function_call_output"]
    assert outputs[-1] == "y" * 200_000  # the newest is kept
    assert all(output.startswith("[Output of file_read cleared") for output in outputs[:-1])
    assert items[1]["output"] == "y" * 200_000  # the session's items are untouched


async def test_a_broken_filter_chain_never_hides_the_real_failure(factory):
    """The SDK's UserError (must return a ModelInputData) once masked the
    turn's real error - all models had failed 403, a swallowed summary
    failure then left the old chain returning None. The guard sends the
    request as it was, so the model's own error is what the user sees."""
    from agents.run import CallModelData, ModelInputData

    data = CallModelData(
        model_data=ModelInputData(
            input=[{"type": "message", "role": "user", "content": "Go"}],
            instructions="Be brief.",
        ),
        agent=None,
        context=None,
    )

    def broken(payload):  # a swallowed failure left nothing
        return None

    async def broken_async(payload):  # ... in an async chain
        return None

    def broken_mid_compaction(payload):  # ... behind a mid-turn compaction
        return broken_async(payload)

    for apply in (broken, broken_async, broken_mid_compaction):
        guarded = factory._guarded_filter(apply)
        result = guarded(data)
        if not isinstance(result, ModelInputData):
            result = await result
        assert result is data.model_data  # the request goes as it was


async def test_the_guard_passes_a_filter_result_through_unchanged(factory):
    from agents.run import CallModelData, ModelInputData

    data = CallModelData(
        model_data=ModelInputData(input=[{"type": "message", "role": "user", "content": "Go"}], instructions="Be brief."),
        agent=None,
        context=None,
    )
    expected = ModelInputData(input=data.model_data.input, instructions="other")

    def apply(payload):
        return expected

    result = factory._guarded_filter(apply)(data)
    assert result is expected


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


class _FinishedRun:
    """A streamed run that answers at once, with the usage the SDK counted."""

    def __init__(self, answer, requests, input_tokens, output_tokens):
        self.final_output = answer
        self.new_items = []
        self.context_wrapper = SimpleNamespace(
            usage=SimpleNamespace(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)
        )

    async def stream_events(self):
        return
        yield  # pragma: no cover


async def test_a_dynamic_agent_reports_what_its_run_took_and_gets_the_callers_turn_limit(factory):
    class Runner:
        kwargs = []

        @classmethod
        def run_streamed(cls, **kwargs):
            cls.kwargs.append(kwargs)
            return _FinishedRun("done", 3, 1200, 80)

    with use(Runner):
        outcome = await factory.run_agent_object(SimpleNamespace(name="executor-2"), "task", max_turns=2)

    assert (outcome.text, outcome.stopped) == ("done", None)
    assert (outcome.model_calls, outcome.input_tokens, outcome.output_tokens) == (3, 1200, 80)
    assert Runner.kwargs[0]["max_turns"] == 2


async def test_a_dynamic_agent_stopped_by_its_time_limit_says_so(factory):
    class Hanging:
        new_items = []

        async def stream_events(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover

    class HangingRunner:
        @classmethod
        def run_streamed(cls, **kwargs):
            return Hanging()

    with use(HangingRunner):
        outcome = await factory.run_agent_object(SimpleNamespace(name="executor-3"), "task", timeout=0.05)

    assert outcome.stopped == "timeout"
    assert "no answer within the agent timeout (0.05 s)" in outcome.text


class _TurnLimitRun:
    """A streamed run that used up its turns after some tool calls."""

    new_items = [SimpleNamespace(type="tool_call_item", raw_item=SimpleNamespace(name="web_fetch"))]
    final_output = None
    context_wrapper = SimpleNamespace(usage=SimpleNamespace(requests=4, input_tokens=900, output_tokens=40))

    async def stream_events(self):
        from agents.exceptions import MaxTurnsExceeded

        raise MaxTurnsExceeded("Max turns (4) exceeded")
        yield  # pragma: no cover


async def test_a_dynamic_agent_at_its_turn_limit_writes_its_report_without_tools(factory):
    from agents import Agent

    class Runner:
        kwargs = []

        @classmethod
        def run_streamed(cls, **kwargs):
            cls.kwargs.append(kwargs)
            if len(cls.kwargs) == 1:
                return _TurnLimitRun()
            return _FinishedRun("Found three providers; the fourth is left.", 1, 300, 60)

    with use(Runner):
        outcome = await factory.run_agent_object(Agent(name="executor-5", instructions="Research."), "task", max_turns=4)

    assert outcome.stopped == "max_turns"
    assert outcome.text == (
        "[Agent executor-5 reached its turn limit; its report of the work so far:]\n"
        "Found three providers; the fourth is left."
    )
    report = Runner.kwargs[1]
    assert report["max_turns"] == 1 and report["session"] is Runner.kwargs[0]["session"]
    assert report["starting_agent"].model_settings.tool_choice == "none"
    assert "no more tool calls" in report["input"]
    # The report's request counts with the run's.
    assert (outcome.model_calls, outcome.input_tokens, outcome.output_tokens) == (5, 1200, 100)


async def test_without_a_report_the_turn_limit_still_says_what_the_agent_did(factory):
    from agents import Agent

    class Runner:
        @classmethod
        def run_streamed(cls, **kwargs):
            return _TurnLimitRun()

    with use(Runner):
        outcome = await factory.run_agent_object(Agent(name="executor-6", instructions="Research."), "task", max_turns=4)

    assert outcome.stopped == "max_turns"
    assert outcome.text.startswith("[Agent executor-6 reached its turn limit]")
    # The summary is of the run that did the work, not of the failed report.
    assert "web_fetch" in outcome.text
