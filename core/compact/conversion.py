"""Conversion between stored conversation messages and compaction messages."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, List

from schemas import ContextMessage

from .base import CompactMessage


def to_compact_messages(messages: Iterable[ContextMessage]) -> List[CompactMessage]:
    """Stored messages as CompactMessage objects, compaction markers included."""
    compact: List[CompactMessage] = []
    for msg in messages:
        metadata = msg.metadata or {}
        try:
            timestamp = datetime.fromisoformat(msg.timestamp) if msg.timestamp else None
        except (TypeError, ValueError):
            timestamp = None
        compact.append(
            CompactMessage(
                role=msg.role,
                content=msg.content,
                message_id=metadata.get("message_id"),
                uuid=metadata.get("uuid"),
                timestamp=timestamp,
                metadata=metadata.copy(),
                is_compact_summary=bool(metadata.get("is_compact_summary")),
                is_compact_boundary=bool(metadata.get("is_compact_boundary")),
            )
        )
    return compact


def to_context_messages(messages: Iterable[CompactMessage]) -> List[ContextMessage]:
    """Compacted messages back as stored messages."""
    stored: List[ContextMessage] = []
    for msg in messages:
        metadata = (msg.metadata or {}).copy()
        if msg.message_id:
            metadata.setdefault("message_id", msg.message_id)
        if msg.uuid:
            metadata.setdefault("uuid", msg.uuid)
        if msg.is_compact_summary:
            metadata["is_compact_summary"] = True
        if msg.is_compact_boundary:
            metadata["is_compact_boundary"] = True
        stored.append(
            ContextMessage(
                role=msg.role,
                content=msg.content,
                timestamp=(msg.timestamp or datetime.now()).isoformat(),
                metadata=metadata or None,
            )
        )
    return stored
