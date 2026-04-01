"""Tests for microcompact - time-based tool result clearing."""

import pytest
from datetime import datetime, timedelta

from core.compact import (
    CLEARED_RESULT_MARKER,
    COMPACTABLE_TOOLS,
    GAP_THRESHOLD_MINUTES,
    KEEP_RECENT,
    collect_compactable_tool_ids,
    evaluate_time_based_trigger,
    microcompact_messages,
    reset_microcompact_state,
    CompactMessage,
)
from schemas import CompactConfig, CompactMicroConfig


def make_assistant_message_with_tool_use(
    tool_name: str,
    tool_use_id: str,
    timestamp: datetime = None,
) -> CompactMessage:
    """Create an assistant message with a tool_use block."""
    return CompactMessage(
        role="assistant",
        content=[
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": tool_name,
                "input": {},
            }
        ],
        message_id=f"msg-{tool_use_id}",
        timestamp=timestamp or datetime.now(),
    )


def make_user_message_with_tool_result(
    tool_use_id: str,
    content: str,
    timestamp: datetime = None,
) -> CompactMessage:
    """Create a user message with a tool_result block."""
    return CompactMessage(
        role="user",
        content=[
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
            }
        ],
        message_id=f"result-{tool_use_id}",
        timestamp=timestamp or datetime.now(),
    )


class TestCollectCompactableToolIds:
    """Tests for collect_compactable_tool_ids."""

    def test_empty_messages(self):
        """Empty list returns empty."""
        assert collect_compactable_tool_ids([]) == []

    def test_collects_compactable_tool(self):
        """Tool in COMPACTABLE_TOOLS is collected."""
        msg = make_assistant_message_with_tool_use("read_file", "tc-1")
        ids = collect_compactable_tool_ids([msg])
        assert "tc-1" in ids

    def test_ignores_non_compactable_tool(self):
        """Tool not in COMPACTABLE_TOOLS is ignored."""
        msg = make_assistant_message_with_tool_use("unknown_tool", "tc-2")
        ids = collect_compactable_tool_ids([msg])
        assert "tc-2" not in ids

    def test_ignores_user_messages(self):
        """User messages are skipped."""
        msg = CompactMessage(
            role="user",
            content=[{"type": "tool_use", "id": "tc-3", "name": "read_file"}],
            message_id="msg-1",
        )
        ids = collect_compactable_tool_ids([msg])
        assert "tc-3" not in ids

    def test_multiple_tools_ordered(self):
        """Order matches encounter order."""
        msgs = [
            make_assistant_message_with_tool_use("read_file", "tc-1"),
            make_assistant_message_with_tool_use("web_fetch", "tc-2"),
        ]
        ids = collect_compactable_tool_ids(msgs)
        assert ids == ["tc-1", "tc-2"]


class TestEvaluateTimeBasedTrigger:
    """Tests for evaluate_time_based_trigger."""

    def test_no_messages(self):
        """No messages → no trigger."""
        assert evaluate_time_based_trigger([]) is None

    def test_no_assistant_message(self):
        """No assistant message → no trigger."""
        msg = CompactMessage(role="user", content="hello", message_id="m1")
        assert evaluate_time_based_trigger([msg]) is None

    def test_recent_assistant_message(self):
        """Recent assistant message → no trigger."""
        msg = CompactMessage(
            role="assistant",
            content="hi",
            message_id="m1",
            timestamp=datetime.now() - timedelta(minutes=5),
        )
        assert evaluate_time_based_trigger([msg]) is None

    def test_old_assistant_message_triggers(self):
        """Gap > threshold → returns gap in minutes."""
        msg = CompactMessage(
            role="assistant",
            content="hi",
            message_id="m1",
            timestamp=datetime.now() - timedelta(hours=2),
        )
        gap = evaluate_time_based_trigger([msg])
        assert gap is not None
        assert gap >= 60.0

    def test_no_timestamp_no_trigger(self):
        """Assistant message without timestamp → no trigger."""
        msg = CompactMessage(role="assistant", content="hi", message_id="m1", timestamp=None)
        assert evaluate_time_based_trigger([msg]) is None

    def test_custom_threshold_is_respected(self):
        """Custom trigger threshold should override the default."""
        msg = CompactMessage(
            role="assistant",
            content="hi",
            message_id="m1",
            timestamp=datetime.now() - timedelta(minutes=30),
        )
        assert evaluate_time_based_trigger([msg], gap_threshold_minutes=20) is not None


class TestMicrocompactMessages:
    """Tests for microcompact_messages."""

    def _old_timestamp(self, hours: float = 2.0) -> datetime:
        return datetime.now() - timedelta(hours=hours)

    def test_empty_messages(self):
        """Empty list returns unchanged."""
        result = microcompact_messages([])
        assert result["messages"] == []

    def test_no_trigger_recent(self):
        """Recent messages → no clearing."""
        assist = make_assistant_message_with_tool_use("read_file", "tc-1")
        user = make_user_message_with_tool_result("tc-1", "x" * 200)
        result = microcompact_messages([assist, user])
        # Tool result content should be unchanged
        user_out = result["messages"][1]
        content_block = user_out.content[0]
        assert content_block["content"] == "x" * 200

    def test_trigger_clears_old_results(self):
        """Old assistant message → clear tool results beyond KEEP_RECENT."""
        old_ts = self._old_timestamp(2)
        # Need > KEEP_RECENT compactable tool IDs so the first one gets cleared
        messages = []
        for i in range(KEEP_RECENT + 1):
            messages.append(
                make_assistant_message_with_tool_use("read_file", f"tc-{i}", timestamp=old_ts)
            )
            messages.append(make_user_message_with_tool_result(f"tc-{i}", f"content-{i}"))
        result = microcompact_messages(messages)
        # tc-0 is the oldest — should be cleared
        first_user_out = result["messages"][1]
        block = first_user_out.content[0]
        assert block["content"] == CLEARED_RESULT_MARKER
        # tc-KEEP_RECENT (the last one) should be preserved
        last_user_out = result["messages"][-1]
        last_block = last_user_out.content[0]
        assert last_block["content"] == f"content-{KEEP_RECENT}"

    def test_keeps_recent_results(self):
        """Last KEEP_RECENT results are preserved even when trigger fires."""
        old_ts = self._old_timestamp(2)
        messages = []
        # Add KEEP_RECENT + 1 turns so oldest gets cleared, newest preserved
        for i in range(KEEP_RECENT + 1):
            messages.append(
                make_assistant_message_with_tool_use("read_file", f"tc-{i}", timestamp=old_ts)
            )
            messages.append(make_user_message_with_tool_result(f"tc-{i}", f"content-{i}"))

        result = microcompact_messages(messages)
        # tc-0 (oldest) should be cleared
        first_user = result["messages"][1]
        assert first_user.content[0]["content"] == CLEARED_RESULT_MARKER
        # tc-KEEP_RECENT (newest) should be preserved
        last_user = result["messages"][-1]
        assert last_user.content[0]["content"] == f"content-{KEEP_RECENT}"

    def test_non_compactable_tool_not_cleared(self):
        """Tool not in COMPACTABLE_TOOLS is not cleared even when trigger fires."""
        old_ts = self._old_timestamp(2)
        assist = CompactMessage(
            role="assistant",
            content=[
                {
                    "type": "tool_use",
                    "id": "tc-x",
                    "name": "unknown_custom_tool",
                    "input": {},
                }
            ],
            message_id="m1",
            timestamp=old_ts,
        )
        user = make_user_message_with_tool_result("tc-x", "sensitive data")
        result = microcompact_messages([assist, user])
        user_out = result["messages"][1]
        block = user_out.content[0]
        # Not in COMPACTABLE_TOOLS → untouched
        assert block["content"] == "sensitive data"

    def test_already_cleared_not_cleared_again(self):
        """Already-cleared content is not re-processed."""
        old_ts = self._old_timestamp(2)
        assist = make_assistant_message_with_tool_use("read_file", "tc-1", timestamp=old_ts)
        user = make_user_message_with_tool_result("tc-1", CLEARED_RESULT_MARKER)
        result = microcompact_messages([assist, user])
        user_out = result["messages"][1]
        block = user_out.content[0]
        assert block["content"] == CLEARED_RESULT_MARKER  # unchanged

    def test_compact_config_controls_threshold_and_tool_list(self):
        """Microcompact should use compact.micro.* from config."""
        compact_cfg = CompactConfig(
            micro=CompactMicroConfig(
                enabled=True,
                gap_threshold_minutes=10.0,
                preserve_last_n=1,
                compactable_tools=["custom_tool"],
            )
        )
        old_ts = datetime.now() - timedelta(minutes=30)
        messages = [
            make_assistant_message_with_tool_use("custom_tool", "tc-1", timestamp=old_ts),
            make_user_message_with_tool_result("tc-1", "old custom output"),
            make_assistant_message_with_tool_use("custom_tool", "tc-2", timestamp=old_ts),
            make_user_message_with_tool_result("tc-2", "recent custom output"),
        ]

        result = microcompact_messages(messages, compact_cfg=compact_cfg)

        assert result["messages"][1].content[0]["content"] == CLEARED_RESULT_MARKER
        assert result["messages"][3].content[0]["content"] == "recent custom output"

    def test_disabled_compact_returns_messages_unchanged(self):
        """Global compact disable should bypass microcompact."""
        compact_cfg = CompactConfig(enabled=False)
        old_ts = self._old_timestamp(2)
        assist = make_assistant_message_with_tool_use("read_file", "tc-1", timestamp=old_ts)
        user = make_user_message_with_tool_result("tc-1", "original")

        result = microcompact_messages([assist, user], compact_cfg=compact_cfg)

        assert result["messages"][1].content[0]["content"] == "original"


class TestResetMicrocompactState:
    """Tests for reset_microcompact_state."""

    def test_reset_is_no_op(self):
        """reset_microcompact_state succeeds without error (stateless)."""
        reset_microcompact_state()  # Should not raise
