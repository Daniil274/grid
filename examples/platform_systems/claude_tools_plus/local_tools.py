"""System-local tools for the claude tools improvement example."""

from __future__ import annotations

from typing import Any


def quality_gate(payload: dict[str, Any]) -> dict[str, Any]:
    """Approve the orchestrator output when a test plan is present."""
    candidate = payload.get("candidate") or payload
    subtasks = candidate.get("subtasks") or []
    return {
        "passed": bool(subtasks),
        "checked_subtasks": len(subtasks),
        "summary": "Quality gate passed" if subtasks else "Quality gate failed",
    }


LOCAL_TOOL_EXECUTORS = {
    "claude_tools_plus.quality_gate": quality_gate,
}
