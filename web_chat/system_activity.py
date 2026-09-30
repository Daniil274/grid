"""What the systems of a server did: their turns and what was done to them.

The systems page (web_chat.system_hub) shows it so a new system can be followed
from its first test to its use after publishing:

- every turn: which system and agent answered, how it ended, how long it took,
  how many calls the action policy held, and who asked;
- every event of a created or user system: made, edited, published, withdrawn,
  submitted, imported, declined.

One JSON file per server, replaced atomically on every change. It keeps counts
and the most recent entries only - never messages, answers or tool output.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("grid.web_chat.system_activity")

RECENT_TURNS = 50
RECENT_EVENTS = 100
OUTCOMES = ("answered", "interrupted", "error", "stopped")


def _empty() -> Dict[str, Any]:
    return {
        "turns": 0,
        "outcomes": {outcome: 0 for outcome in OUTCOMES},
        "policy_blocks": 0,
        "duration_ms": 0,
        "users": [],
        "first_at": None,
        "last_at": None,
        "recent": [],
        "events": [],
    }


class SystemActivity:
    """The activity of every system of one server."""

    VERSION = 1

    def __init__(self, path: Optional[Path]) -> None:
        """*path* None keeps the activity in memory only."""
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._systems: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            systems = document.get("systems", {})
            return {key: {**_empty(), **value} for key, value in systems.items() if isinstance(value, dict)}
        except (OSError, ValueError, AttributeError) as exc:
            logger.error("System activity %s is unreadable (%s); starting empty", self.path, exc)
            return {}

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.path.with_name(self.path.name + ".tmp")
            partial.write_text(
                json.dumps({"version": self.VERSION, "systems": self._systems}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(partial, self.path)
        except OSError:
            # Losing a count must never fail a turn.
            logger.exception("Could not save system activity to %s", self.path)

    def record_turn(
        self,
        system: str,
        *,
        agent: str,
        outcome: str,
        duration_ms: int,
        user_id: str,
        policy_blocks: int = 0,
    ) -> None:
        now = time.time()
        with self._lock:
            entry = self._systems.setdefault(system, _empty())
            entry["turns"] += 1
            entry["outcomes"][outcome if outcome in OUTCOMES else "error"] = (
                entry["outcomes"].get(outcome if outcome in OUTCOMES else "error", 0) + 1
            )
            entry["policy_blocks"] += policy_blocks
            entry["duration_ms"] += max(0, int(duration_ms))
            if user_id not in entry["users"]:
                entry["users"].append(user_id)
            entry["first_at"] = entry["first_at"] or now
            entry["last_at"] = now
            entry["recent"] = [
                {
                    "at": now,
                    "agent": agent,
                    "outcome": outcome,
                    "duration_ms": int(duration_ms),
                    "policy_blocks": policy_blocks,
                    "user_id": user_id,
                },
                *entry["recent"],
            ][:RECENT_TURNS]
            self._save()

    def record_event(self, system: str, event: str, *, by: str = "", note: str = "") -> None:
        with self._lock:
            entry = self._systems.setdefault(system, _empty())
            entry["events"] = [{"at": time.time(), "event": event, "by": by, "note": note}, *entry["events"]][
                :RECENT_EVENTS
            ]
            self._save()

    def summary(self, system: str, *, names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """One system's activity; *names* maps user ids to the names shown."""
        names = names or {}
        with self._lock:
            entry = json.loads(json.dumps(self._systems.get(system, _empty())))
        turns = entry["turns"]
        entry["average_ms"] = round(entry.pop("duration_ms") / turns) if turns else 0
        entry["user_count"] = len(entry.pop("users"))
        for turn in entry["recent"]:
            turn["user"] = names.get(turn["user_id"], turn["user_id"])
        return entry

    def counts(self) -> Dict[str, Dict[str, Any]]:
        """Turns, errors and the last use of every system, for a list."""
        with self._lock:
            return {
                key: {
                    "turns": value["turns"],
                    "errors": value["outcomes"].get("error", 0),
                    "last_at": value["last_at"],
                }
                for key, value in self._systems.items()
            }

    def forget(self, system: str) -> None:
        with self._lock:
            if self._systems.pop(system, None) is not None:
                self._save()
