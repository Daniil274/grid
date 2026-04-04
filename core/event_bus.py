"""Lightweight domain event bus for cross-layer coordination."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List


@dataclass
class DomainEvent:
    event_type: str
    payload: Dict[str, Any]
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class DomainEventBus:
    """In-memory event bus with sync subscribers."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Callable[[DomainEvent], None]]] = {}
        self._all_subscribers: List[Callable[[DomainEvent], None]] = []
        self._events: List[DomainEvent] = []

    def subscribe(self, event_type: str, callback: Callable[[DomainEvent], None]) -> None:
        self._subscribers.setdefault(event_type, []).append(callback)

    def subscribe_all(self, callback: Callable[[DomainEvent], None]) -> None:
        self._all_subscribers.append(callback)

    def publish(self, event: DomainEvent) -> None:
        self._events.append(event)
        for callback in self._all_subscribers:
            callback(event)
        for callback in self._subscribers.get(event.event_type, []):
            callback(event)

    def list_events(self) -> List[DomainEvent]:
        return list(self._events)
