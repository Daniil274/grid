"""Tests for compact utilities."""

import pytest
from datetime import datetime

from core.compact import (
    rough_token_count as estimate_tokens,
    estimate_messages_tokens,
    strip_images_from_messages,
    format_compact_summary,
    create_compact_boundary_message,
    CompactMessage,
)


class TestEstimateTokens:
    """Tests for estimate_tokens function."""

    def test_empty_string(self):
        """Test token estimation for empty string."""
        assert estimate_tokens("") == 0

    def test_simple_string(self):
        """Test token estimation for simple string."""
        tokens = estimate_tokens("Hello world")
        assert tokens >= 1

    def test_longer_string_more_tokens(self):
        """Test that longer strings have more tokens."""
        short = estimate_tokens("Hello")
        long = estimate_tokens("Hello " * 100)
        assert long > short

    def test_whitespace_handling(self):
        """Test whitespace handling in estimation."""
        text1 = estimate_tokens("hello world")
        text2 = estimate_tokens("hello    world")
        # Multiple spaces might slightly increase estimate
        assert text2 >= text1


class TestStripImagesFromMessages:
    """Tests for strip_images_from_messages function."""

    def test_empty_messages(self):
        """Test stripping images from empty list."""
        result = strip_images_from_messages([])
        assert result == []

    def test_text_only_messages(self):
        """Test that text messages are unchanged."""
        messages = [
            CompactMessage(role="user", content="Hello", message_id="m1"),
            CompactMessage(role="assistant", content="Hi", message_id="m2"),
        ]
        result = strip_images_from_messages(messages)
        assert len(result) == 2

    def test_image_in_content_list(self):
        """Test stripping images from content list."""
        messages = [
            CompactMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Hello"},
                    {"type": "image_url", "image_url": {"url": "http://example.com/img.png"}},
                ],
                message_id="m1",
            ),
        ]
        result = strip_images_from_messages(messages)
        # Image should be replaced with marker
        content = result[0].content
        assert any(part.get("type") == "text" for part in content)

    def test_image_type_replaced_with_marker(self):
        """Test stripping 'image' type blocks (CC/Anthropic format)."""
        messages = [
            CompactMessage(
                role="user",
                content=[
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
                ],
                message_id="m1",
            ),
        ]
        result = strip_images_from_messages(messages)
        content = result[0].content
        # 'image' block should be replaced with a text marker
        assert any(part.get("type") == "text" and "image" in part.get("text", "") for part in content)


class TestFormatCompactSummary:
    """Tests for format_compact_summary function."""

    def test_strips_analysis_section(self):
        """Test that analysis section is stripped."""
        summary = "<analysis>Thinking...</analysis><summary>Result</summary>"
        formatted = format_compact_summary(summary)
        assert "<analysis>" not in formatted
        assert "Thinking" not in formatted

    def test_formats_summary(self):
        """Test that summary is formatted."""
        summary = "<summary>Test content</summary>"
        formatted = format_compact_summary(summary)
        assert "Test content" in formatted

    def test_cleans_extra_whitespace(self):
        """Test that extra whitespace is cleaned."""
        summary = "<summary>Line 1\n\n\n\nLine 2</summary>"
        formatted = format_compact_summary(summary)
        # Should reduce multiple newlines to double
        assert "\n\n\n" not in formatted


class TestCreateCompactBoundaryMessage:
    """Tests for create_compact_boundary_message function."""

    def test_creates_system_message(self):
        """Test that boundary message is system role."""
        msg = create_compact_boundary_message("Test reason")
        assert msg.role == "system"

    def test_contains_reason(self):
        """Test that boundary contains reason."""
        msg = create_compact_boundary_message("Context overflow")
        assert "Context overflow" in msg.content

    def test_has_message_id(self):
        """Test that boundary has unique ID."""
        msg = create_compact_boundary_message("Test")
        assert msg.message_id is not None
        assert "compact-" in msg.message_id

    def test_has_metadata(self):
        """Test that boundary has metadata."""
        msg = create_compact_boundary_message("Test")
        assert msg.metadata is not None
        assert msg.metadata.get("type") == "compact_boundary"


class TestEstimateMessagesTokens:
    """Tests for estimate_messages_tokens function."""

    def test_empty_list(self):
        """Test token count for empty list."""
        assert estimate_messages_tokens([]) == 0

    def test_single_message(self):
        """Test token count for single message."""
        messages = [
            CompactMessage(role="user", content="Hello", message_id="m1"),
        ]
        tokens = estimate_messages_tokens(messages)
        assert tokens > 0

    def test_multiple_messages(self):
        """Test that multiple messages have more tokens."""
        messages1 = [
            CompactMessage(role="user", content="Hello", message_id="m1"),
        ]
        messages2 = [
            CompactMessage(role="user", content="Hello", message_id="m1"),
            CompactMessage(role="assistant", content="Hi there", message_id="m2"),
        ]
        tokens1 = estimate_messages_tokens(messages1)
        tokens2 = estimate_messages_tokens(messages2)
        assert tokens2 > tokens1

    def test_tool_uses_add_tokens(self):
        """Test that tool_use blocks in content add to token count."""
        messages_no_tools = [
            CompactMessage(role="assistant", content="", message_id="m1"),
        ]
        messages_with_tools = [
            CompactMessage(
                role="assistant",
                content=[
                    {"type": "tool_use", "id": "tc-1", "name": "read_file", "input": {"path": "/file.txt"}},
                ],
                message_id="m1",
            ),
        ]
        tokens_no_tools = estimate_messages_tokens(messages_no_tools)
        tokens_with_tools = estimate_messages_tokens(messages_with_tools)
        assert tokens_with_tools > tokens_no_tools