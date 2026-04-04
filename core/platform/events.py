"""Backwards-compatible structured import for platform events."""

from core.event_bus import DomainEvent, DomainEventBus

__all__ = ["DomainEvent", "DomainEventBus"]
