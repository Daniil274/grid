"""What the factory records about its runs, for recovery and the timeline.

The pending-run record of a turn (resumable after a crash) and the
runtime events of its attempts, both kept by the context manager. Relies on
``self.context_manager`` and ``self.config``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional


def safe_preview(value: Any, max_length: Optional[int] = 500) -> str:
    """Convert arbitrary runtime data to a compact preview string."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = text.strip()
    if max_length is not None and len(text) > max_length:
        return text[:max_length] + "..."
    return text


class RunJournal:
    """The pending-run record of the turns in one conversation store, and the
    runtime events of their attempts (context manager metadata)."""

    def __init__(self, context_manager: Any, config: Any) -> None:
        self.context_manager = context_manager
        self.config = config

    def _preview_limit(self) -> Optional[int]:
        """Max length for tool_events in context metadata; None = keep full payloads."""
        settings = getattr(self.config.config.settings, "agent_logging", None)
        if not settings or not getattr(settings, "enabled", True):
            return 700
        level = (getattr(settings, "level", "full") or "full").lower()
        if level in ("full", "detailed"):
            return None
        return 700

    def update_pending_run(
        self,
        *,
        agent_key: str,
        active_context_id: Optional[str],
        input_preview: str,
        status: str,
        retry_count: int = 0,
        last_error: Optional[str] = None,
        clear_tool_events: bool = False,
        task: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist durable state for the currently running agent attempt.

        ``clear_tool_events`` starts the record of a new turn; ``task`` and
        ``turn_id`` are what recovering the turn after a crash needs
        (ContextManager.recover_abandoned_turn).
        """
        existing = self.context_manager.get_metadata("pending_agent_run")
        payload: Dict[str, Any] = existing.copy() if isinstance(existing, dict) else {}
        if clear_tool_events or not isinstance(payload.get("tool_events"), list):
            payload["tool_events"] = []
        if clear_tool_events:
            payload.pop("task", None)
            payload.pop("turn_id", None)
        if task is not None:
            payload["task"] = task
        if turn_id is not None:
            payload["turn_id"] = turn_id

        payload.update(
            {
                "agent": agent_key,
                "context_id": active_context_id,
                "status": status,
                "retry_count": retry_count,
                "updated_at": datetime.now().isoformat(),
            }
        )
        if input_preview:
            payload["input_preview"] = input_preview
        if last_error is not None:
            payload["last_error"] = last_error
        elif status in {"completed", "running"}:
            payload.pop("last_error", None)

        self.context_manager.set_metadata("pending_agent_run", payload)
        return payload

    def record_event(
        self,
        *,
        event_type: str,
        tool_name: Optional[str] = None,
        arguments: Any = None,
        output: Any = None,
        extra: Optional[Dict[str, Any]] = None,
        persist: bool = False,
    ) -> None:
        """Append a compact runtime event to durable context metadata.

        Events are written with the next status change, not one file write per
        event - except with ``persist``: a tool call about to run is saved at
        once, so a process that dies during it still leaves the call on record
        (ContextManager.recover_abandoned_turn).
        """
        event: Dict[str, Any] = {
            "event_type": event_type,
            "timestamp": datetime.now().isoformat(),
        }
        if tool_name:
            event["tool_name"] = tool_name
        preview_limit = self._preview_limit()
        if arguments is not None:
            event["arguments"] = safe_preview(arguments, max_length=preview_limit)
        if output is not None:
            event["output"] = safe_preview(output, max_length=preview_limit)
        if extra:
            for key, value in extra.items():
                if value is not None:
                    event[key] = safe_preview(
                        value, max_length=preview_limit or 300
                    )
        pending = self.context_manager.get_metadata("pending_agent_run")
        if not isinstance(pending, dict):
            pending = {}
        tool_events = pending.get("tool_events")
        if not isinstance(tool_events, list):
            tool_events = []
        tool_events = [item for item in tool_events if isinstance(item, dict)]
        tool_events.append(event)
        pending["tool_events"] = tool_events[-100:]
        pending["updated_at"] = datetime.now().isoformat()
        self.context_manager.set_metadata("pending_agent_run", pending, persist=persist)
