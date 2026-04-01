"""
Base types and configuration for the Compact System.

Provides dataclasses and enums for compaction configuration,
results, and message representation. Aligned with Claude Code's compact architecture.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime
import uuid as uuid_module


class CompactionStrategy(str, Enum):
    """Strategy for compacting conversation context."""
    MICRO = "micro"              # Time-based tool result clearing
    SESSION_MEMORY = "session_memory"  # Session memory-based (no LLM call)
    FULL = "full"               # Full LLM summarization
    REACTIVE = "reactive"       # Triggered by prompt_too_long error


class CompactionDirection(str, Enum):
    """Direction for partial compaction."""
    FROM = "from"    # Compact from last compact boundary
    UP_TO = "up_to"  # Compact up to a point, keep messages after


class CompactionStatus(str, Enum):
    """Status of a compaction operation."""
    SUCCESS = "success"
    INSUFFICIENT_MESSAGES = "insufficient_messages"
    ABORTED = "aborted"
    ERROR = "error"
    MEDIA_UNSTRIPPABLE = "media_unstrippable"


@dataclass
class TokenWarningState:
    """
    Token usage warning state for context management.
    Mirrors Claude Code's calculateTokenWarningState output.
    """
    percent_left: float
    is_above_warning_threshold: bool
    is_above_error_threshold: bool
    is_above_auto_compact_threshold: bool
    is_at_blocking_limit: bool

    def __repr__(self) -> str:
        return (
            f"TokenWarningState(percent_left={self.percent_left:.1f}%, "
            f"warning={self.is_above_warning_threshold}, "
            f"error={self.is_above_error_threshold}, "
            f"auto_compact={self.is_above_auto_compact_threshold}, "
            f"blocking={self.is_at_blocking_limit})"
        )


@dataclass
class CompactMessage:
    """
    Unified message representation for compaction.

    role: "user", "assistant", or "system"
    content: str or list of content blocks (OpenAI/Anthropic format)
    message_id: Logical message ID — shared by streaming chunks of the same API response.
                Used as the grouping boundary gate (like CC's message.id).
    uuid: Unique per-message identifier (like CC's message.uuid).
          Used by session memory to track the last summarized position.
    timestamp: When the message was created (drives time-based microcompact).
    is_compact_summary: True for the LLM summary injected after compaction.
    is_compact_boundary: True for the system marker inserted at compact boundaries.
    """
    role: str
    content: Any  # str or list of content blocks
    message_id: Optional[str] = None
    uuid: Optional[str] = field(default_factory=lambda: str(uuid_module.uuid4()))
    timestamp: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_compact_summary: bool = False
    is_compact_boundary: bool = False

    # Separate tool tracking (for Anthropic-style block parsing compatibility)
    tool_uses: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)

    def get_text(self) -> str:
        """Extract plain text content from this message."""
        if isinstance(self.content, str):
            return self.content

        if isinstance(self.content, list):
            parts = []
            for block in self.content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type", "")
                if t == "text":
                    parts.append(block.get("text", ""))
                elif t == "tool_result":
                    rc = block.get("content", "")
                    if isinstance(rc, str):
                        parts.append(rc)
                    elif isinstance(rc, list):
                        for item in rc:
                            if isinstance(item, dict) and item.get("type") == "text":
                                parts.append(item.get("text", ""))
            return " ".join(parts)

        return str(self.content) if self.content else ""

    def has_images(self) -> bool:
        """Check if message contains image or document blocks."""
        if isinstance(self.content, list):
            for block in self.content:
                if isinstance(block, dict) and block.get("type") in (
                    "image", "image_url", "image_file", "document"
                ):
                    return True
        return False

    def has_text_blocks(self) -> bool:
        """
        Check whether this message contains text content (not just tool calls/results).
        Mirrors CC's hasTextBlocks() used by session memory compact.
        """
        if self.role == "assistant":
            if isinstance(self.content, list):
                return any(
                    isinstance(b, dict) and b.get("type") == "text"
                    for b in self.content
                )
            return bool(self.content)

        if self.role == "user":
            if isinstance(self.content, str):
                return len(self.content) > 0
            if isinstance(self.content, list):
                return any(
                    isinstance(b, dict) and b.get("type") == "text"
                    for b in self.content
                )

        return False

    def get_tool_use_ids(self) -> List[str]:
        """Return IDs of all tool_use blocks in this message (assistant messages)."""
        ids = []
        if isinstance(self.content, list):
            for block in self.content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    bid = block.get("id")
                    if bid:
                        ids.append(bid)
        # Also check tool_uses field
        for tu in self.tool_uses:
            tid = tu.get("id")
            if tid and tid not in ids:
                ids.append(tid)
        return ids

    def get_tool_result_ids(self) -> List[str]:
        """Return tool_use_ids referenced by tool_result blocks in this message (user messages)."""
        ids = []
        if isinstance(self.content, list):
            for block in self.content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        ids.append(tid)
        # Also check tool_results field
        for tr in self.tool_results:
            tid = tr.get("tool_use_id") or tr.get("tool_call_id")
            if tid and tid not in ids:
                ids.append(tid)
        return ids


@dataclass
class CompactGroup:
    """
    Group of messages representing one API round-trip.

    Bounded by assistant message_id — all streaming chunks sharing the same
    message_id belong to the same group. This preserves tool_use/tool_result
    pairing: every tool_use is resolved before the next assistant turn.
    """
    messages: List[CompactMessage]
    assistant_id: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    token_count: int = 0

    def get_user_messages(self) -> List[CompactMessage]:
        return [m for m in self.messages if m.role == "user"]

    def get_assistant_messages(self) -> List[CompactMessage]:
        return [m for m in self.messages if m.role == "assistant"]

    def has_tool_calls(self) -> bool:
        return any(m.tool_uses or m.get_tool_use_ids() for m in self.messages)


@dataclass
class CompactionConfig:
    """
    Configuration for context compaction. Mirrors CC's constants.
    """
    # Auto-compact thresholds (tokens)
    autocompact_buffer_tokens: int = 13_000
    warning_buffer_tokens: int = 20_000
    error_buffer_tokens: int = 20_000
    manual_compact_buffer_tokens: int = 3_000
    max_output_tokens_for_summary: int = 20_000
    max_consecutive_autocompact_failures: int = 3

    # Microcompact (time-based)
    micro_gap_threshold_minutes: float = 60.0
    micro_keep_recent: int = 5

    # Session memory compact
    sm_min_tokens: int = 10_000
    sm_min_text_block_messages: int = 5
    sm_max_tokens: int = 40_000

    # Full compact
    max_ptl_retries: int = 3
    summary_max_output_tokens: int = 20_000

    # Post-compact file/skill restoration
    restore_files_max: int = 5
    restore_files_max_tokens_per_file: int = 5_000
    restore_files_token_budget: int = 50_000
    restore_skills_token_budget: int = 25_000
    restore_skills_max_tokens_per_skill: int = 5_000

    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "CompactionConfig":
        compact = config.get("compact", {})
        return cls(
            autocompact_buffer_tokens=compact.get("autocompact_buffer_tokens", 13_000),
            micro_gap_threshold_minutes=compact.get("micro_gap_threshold_minutes", 60.0),
            micro_keep_recent=compact.get("micro_keep_recent", 5),
            sm_min_tokens=compact.get("sm_min_tokens", 10_000),
            sm_min_text_block_messages=compact.get("sm_min_text_block_messages", 5),
            sm_max_tokens=compact.get("sm_max_tokens", 40_000),
            max_ptl_retries=compact.get("max_ptl_retries", 3),
            summary_max_output_tokens=compact.get("summary_max_output_tokens", 20_000),
        )


@dataclass
class CompactionResult:
    """
    Result of any compaction operation.

    boundary_marker: System message marking where compaction occurred.
    summary_messages: User message(s) containing the LLM summary (or SM content).
    compacted_messages: Final ordered list: [boundary, summary, ...messagesToKeep, ...attachments]
    """
    status: CompactionStatus
    strategy: CompactionStrategy

    # Ordered final messages (boundary + summary + kept)
    compacted_messages: List[CompactMessage] = field(default_factory=list)

    # Separate parts (for callers that need to inspect them)
    boundary_marker: Optional[CompactMessage] = None
    summary_messages: List[CompactMessage] = field(default_factory=list)
    messages_to_keep: List[CompactMessage] = field(default_factory=list)

    # The LLM-generated summary text
    summary: Optional[str] = None

    # Token statistics
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0

    # Group statistics
    groups_before: int = 0
    groups_after: int = 0
    groups_compacted: int = 0

    # Error info
    error_message: Optional[str] = None

    # User-facing message
    user_display_message: Optional[str] = None

    def success(self) -> bool:
        return self.status == CompactionStatus.SUCCESS

    def get_stats(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "strategy": self.strategy.value,
            "tokens": {
                "before": self.tokens_before,
                "after": self.tokens_after,
                "saved": self.tokens_saved,
                "saved_percent": round(
                    100 * self.tokens_saved / max(1, self.tokens_before), 1
                ),
            },
            "groups": {
                "before": self.groups_before,
                "after": self.groups_after,
                "compacted": self.groups_compacted,
            },
        }
