"""
Blackboard System - Shared memory for multi-agent coordination.

The Blackboard provides a thread-safe shared memory space where agents can:
- Post findings, hypotheses, critiques, votes, and artifacts
- Query entries with filters
- Subscribe to updates on specific tags
- Get formatted context for their perspective

This enables emergent collaboration patterns without hardcoding specific workflows.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from threading import RLock
from typing import Dict, List, Any, Optional, Callable
from pathlib import Path


class EntryType:
    """Types of blackboard entries."""
    HYPOTHESIS = "hypothesis"  # Tentative conclusion
    FACT = "fact"              # Verified information
    QUESTION = "question"      # Open question needing answer
    CRITIQUE = "critique"      # Critical analysis
    VOTE = "vote"              # Vote on a proposal
    ARTIFACT = "artifact"      # Produced output (code, text, etc.)
    SIGNAL = "signal"          # Coordination signal
    DECISION = "decision"      # Final decision
    PIPELINE = "pipeline"      # Pipeline execution record


@dataclass
class BlackboardEntry:
    """Single entry in the blackboard."""
    id: str
    entry_type: str
    author: str
    content: Any
    confidence: float = 1.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    parent_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BlackboardEntry":
        """Create from dictionary."""
        return cls(**data)


class Blackboard:
    """
    Thread-safe shared memory for multi-agent coordination.

    Key features:
    - Post/query entries with rich metadata
    - Filter by type, author, tags, time
    - Pub/sub for reactive triggers
    - JSON persistence
    - Context generation for agents
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        persist_path: Optional[str] = None,
        max_entries: int = 1000,
        entry_ttl_hours: int = 24
    ):
        """
        Initialize blackboard.

        Args:
            session_id: Unique session identifier
            persist_path: Path to JSON file for persistence
            max_entries: Maximum entries to keep
            entry_ttl_hours: Time-to-live for entries in hours
        """
        self._lock = RLock()
        self.session_id = session_id or f"bb-{uuid.uuid4().hex[:8]}"
        self.persist_path = persist_path
        self.max_entries = max_entries
        self.entry_ttl_hours = entry_ttl_hours

        self._entries: Dict[str, BlackboardEntry] = {}
        self._subscribers: Dict[str, List[Callable[[BlackboardEntry], None]]] = {}
        self._entry_order: List[str] = []  # Maintain insertion order

        # Load existing entries if persist path exists
        if persist_path:
            self._load()

    def post(
        self,
        entry_type: str,
        author: str,
        content: Any,
        confidence: float = 1.0,
        parent_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        notify: bool = True
    ) -> str:
        """
        Post an entry to the blackboard.

        Args:
            entry_type: Type from EntryType constants
            author: Name of the agent posting
            content: The actual content (any serializable type)
            confidence: Confidence score 0-1
            parent_id: ID of parent entry for threading
            tags: List of tags for filtering
            metadata: Additional metadata
            notify: Whether to notify subscribers

        Returns:
            ID of the created entry
        """
        with self._lock:
            entry_id = f"bb-{uuid.uuid4().hex[:8]}"

            entry = BlackboardEntry(
                id=entry_id,
                entry_type=entry_type,
                author=author,
                content=content,
                confidence=confidence,
                parent_id=parent_id,
                tags=tags or [],
                metadata=metadata or {}
            )

            self._entries[entry_id] = entry
            self._entry_order.append(entry_id)

            # Enforce max entries
            self._cleanup_old_entries()

            # Persist
            if self.persist_path:
                self._save()

            # Notify subscribers
            if notify:
                self._notify_subscribers(entry)

            return entry_id

    def post_entry(self, entry: BlackboardEntry, notify: bool = True) -> str:
        """Post a pre-constructed entry."""
        with self._lock:
            if not entry.id:
                entry.id = f"bb-{uuid.uuid4().hex[:8]}"

            self._entries[entry.id] = entry
            self._entry_order.append(entry.id)

            self._cleanup_old_entries()

            if self.persist_path:
                self._save()

            if notify:
                self._notify_subscribers(entry)

            return entry.id

    def query(
        self,
        entry_type: Optional[str] = None,
        author: Optional[str] = None,
        tags: Optional[List[str]] = None,
        since: Optional[str] = None,
        parent_id: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 100,
        offset: int = 0
    ) -> List[BlackboardEntry]:
        """
        Query entries with filters.

        Args:
            entry_type: Filter by entry type
            author: Filter by author
            tags: Filter by tags (any match)
            since: Filter entries after this timestamp
            parent_id: Filter by parent entry
            min_confidence: Minimum confidence score
            limit: Maximum entries to return
            offset: Number of entries to skip

        Returns:
            List of matching entries, newest first
        """
        with self._lock:
            results = list(self._entries.values())

            if entry_type:
                results = [e for e in results if e.entry_type == entry_type]

            if author:
                results = [e for e in results if e.author == author]

            if tags:
                results = [e for e in results if any(t in e.tags for t in tags)]

            if since:
                results = [e for e in results if e.timestamp >= since]

            if parent_id:
                results = [e for e in results if e.parent_id == parent_id]

            if min_confidence > 0:
                results = [e for e in results if e.confidence >= min_confidence]

            # Sort by timestamp descending (newest first)
            results.sort(key=lambda x: x.timestamp, reverse=True)

            # Apply pagination
            return results[offset:offset + limit]

    def get(self, entry_id: str) -> Optional[BlackboardEntry]:
        """Get a specific entry by ID."""
        with self._lock:
            return self._entries.get(entry_id)

    def get_thread(self, entry_id: str) -> List[BlackboardEntry]:
        """Get all entries in a thread (entry + all descendants)."""
        with self._lock:
            thread = []

            # Get the root entry
            root = self._entries.get(entry_id)
            if root:
                thread.append(root)

            # Get all children recursively
            def get_children(parent_id: str):
                for entry in self._entries.values():
                    if entry.parent_id == parent_id:
                        thread.append(entry)
                        get_children(entry.id)

            get_children(entry_id)

            # Sort by timestamp
            thread.sort(key=lambda x: x.timestamp)
            return thread

    def subscribe(self, tag: str, callback: Callable[[BlackboardEntry], None]):
        """
        Subscribe to entries with a specific tag.

        Args:
            tag: Tag to subscribe to
            callback: Function to call when matching entry is posted
        """
        with self._lock:
            if tag not in self._subscribers:
                self._subscribers[tag] = []
            self._subscribers[tag].append(callback)

    def unsubscribe(self, tag: str, callback: Callable[[BlackboardEntry], None]):
        """Unsubscribe from a tag."""
        with self._lock:
            if tag in self._subscribers:
                try:
                    self._subscribers[tag].remove(callback)
                except ValueError:
                    pass

    def get_context_for_agent(
        self,
        agent_name: str,
        max_entries: int = 20,
        include_types: Optional[List[str]] = None,
        exclude_self: bool = False
    ) -> str:
        """
        Get formatted context from blackboard for an agent.

        Args:
            agent_name: Name of the agent requesting context
            max_entries: Maximum entries to include
            include_types: Only include these entry types
            exclude_self: Exclude entries by this agent

        Returns:
            Formatted string with blackboard context
        """
        with self._lock:
            entries = list(self._entries.values())

            if include_types:
                entries = [e for e in entries if e.entry_type in include_types]

            if exclude_self:
                entries = [e for e in entries if e.author != agent_name]

            # Sort by timestamp descending
            entries.sort(key=lambda x: x.timestamp, reverse=True)
            entries = entries[:max_entries]

            # Reverse to show oldest first
            entries.reverse()

            if not entries:
                return "=== BLACKBOARD: Пусто ==="

            lines = ["=== BLACKBOARD CONTEXT ==="]
            for entry in entries:
                conf_str = f" ({entry.confidence:.0%})" if entry.confidence < 1.0 else ""
                tags_str = f" [{', '.join(entry.tags)}]" if entry.tags else ""

                # Format content
                if isinstance(entry.content, dict):
                    content_str = json.dumps(entry.content, ensure_ascii=False, indent=2)
                elif isinstance(entry.content, str) and len(entry.content) > 500:
                    content_str = entry.content[:500] + "..."
                else:
                    content_str = str(entry.content)

                lines.append(
                    f"[{entry.entry_type.upper()}] {entry.author}{conf_str}{tags_str}:\n{content_str}"
                )

            lines.append("=== END BLACKBOARD ===")
            return "\n\n".join(lines)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics of the blackboard."""
        with self._lock:
            type_counts = {}
            author_counts = {}

            for entry in self._entries.values():
                type_counts[entry.entry_type] = type_counts.get(entry.entry_type, 0) + 1
                author_counts[entry.author] = author_counts.get(entry.author, 0) + 1

            return {
                "session_id": self.session_id,
                "total_entries": len(self._entries),
                "by_type": type_counts,
                "by_author": author_counts,
                "oldest_entry": min((e.timestamp for e in self._entries.values()), default=None),
                "newest_entry": max((e.timestamp for e in self._entries.values()), default=None)
            }

    def clear(self, preserve_persist: bool = False):
        """Clear all entries."""
        with self._lock:
            self._entries.clear()
            self._entry_order.clear()

            if self.persist_path and not preserve_persist:
                self._save()

    def _notify_subscribers(self, entry: BlackboardEntry):
        """Notify subscribers about a new entry."""
        callbacks_to_run = []

        with self._lock:
            for tag in entry.tags:
                if tag in self._subscribers:
                    callbacks_to_run.extend(self._subscribers[tag])

            # Also notify on entry_type as pseudo-tag
            if entry.entry_type in self._subscribers:
                callbacks_to_run.extend(self._subscribers[entry.entry_type])

        # Run callbacks outside lock to avoid deadlocks
        for callback in callbacks_to_run:
            try:
                callback(entry)
            except Exception as e:
                # Log but don't fail
                print(f"Blackboard subscriber error: {e}")

    def _cleanup_old_entries(self):
        """Remove old entries to stay within limits."""
        with self._lock:
            # Remove by count
            while len(self._entry_order) > self.max_entries:
                oldest_id = self._entry_order.pop(0)
                self._entries.pop(oldest_id, None)

            # Remove by TTL
            if self.entry_ttl_hours > 0:
                cutoff = datetime.now().isoformat()
                # Simple approach: keep entries from last entry_ttl_hours
                # More sophisticated TTL would require datetime parsing

    def _save(self):
        """Save entries to JSON file."""
        if not self.persist_path:
            return

        with self._lock:
            data = {
                "session_id": self.session_id,
                "entries": [e.to_dict() for e in self._entries.values()]
            }

            path = Path(self.persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    def _load(self):
        """Load entries from JSON file."""
        if not self.persist_path:
            return

        path = Path(self.persist_path)
        if not path.exists():
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            with self._lock:
                self.session_id = data.get("session_id", self.session_id)

                for entry_data in data.get("entries", []):
                    entry = BlackboardEntry.from_dict(entry_data)
                    self._entries[entry.id] = entry
                    self._entry_order.append(entry.id)

        except Exception as e:
            print(f"Failed to load blackboard from {self.persist_path}: {e}")


# Global blackboard instance (can be overridden)
_global_blackboard: Optional[Blackboard] = None


def get_global_blackboard() -> Blackboard:
    """Get or create the global blackboard instance."""
    global _global_blackboard
    if _global_blackboard is None:
        _global_blackboard = Blackboard()
    return _global_blackboard


def set_global_blackboard(blackboard: Blackboard):
    """Set the global blackboard instance."""
    global _global_blackboard
    _global_blackboard = blackboard
