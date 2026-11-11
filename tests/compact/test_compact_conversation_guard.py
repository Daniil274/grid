"""Tests for non-beneficial full compaction guard."""

import pytest

from core.compact.compact_conversation import compact_conversation
from core.compact import CompactMessage, CompactionStatus


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    async def create(self, **kwargs):
        # Deliberately long summary to force non-beneficial compaction.
        return _FakeResponse("Very long summary. " * 200)


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self):
        self.chat = _FakeChat()


@pytest.mark.asyncio
async def test_compact_conversation_aborts_if_summary_is_larger():
    messages = [
        CompactMessage(role="user", content="Hello", message_id="m1"),
        CompactMessage(role="assistant", content="And hello to you too", message_id="m2"),
    ]

    result = await compact_conversation(
        messages=messages,
        llm_client=_FakeClient(),
        model="fake-model",
        is_auto_compact=False,
    )

    assert result.status == CompactionStatus.ABORTED
    assert result.tokens_saved == 0
    assert result.compacted_messages == messages


@pytest.mark.asyncio
async def test_the_real_session_size_prevents_a_false_abort():
    """A session of huge tool outputs clips down to a small transcript.

    A summary measured against the clipped transcript is larger than it and
    reads as "not smaller than the context" - a false ABORTED, for the session
    it would summarize is far larger. Measured against the session's real size
    (``raw_tokens_before``, what the caller knows) the same summary stands."""
    messages = [
        CompactMessage(role="user", content="Hello", message_id="m1"),
        CompactMessage(role="tool", content="Result: 42", message_id="m2"),
    ]

    against_the_transcript = await compact_conversation(
        messages=messages,
        llm_client=_FakeClient(),
        model="fake-model",
        is_auto_compact=True,
    )
    assert against_the_transcript.status == CompactionStatus.ABORTED

    against_the_session = await compact_conversation(
        messages=messages,
        llm_client=_FakeClient(),
        model="fake-model",
        is_auto_compact=True,
        raw_tokens_before=500_000,
    )
    assert against_the_session.status == CompactionStatus.SUCCESS
