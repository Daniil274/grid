"""System-local tools for the claude-tools improvement example."""

from __future__ import annotations

from typing import Any, Dict


def claude_tools_quality_gate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate that orchestration produced a minimal executable plan."""
    candidate = payload.get("candidate") or payload
    subtasks = candidate.get("subtasks") or []
    has_test_step = any("test" in str(item).lower() for item in subtasks)
    return {
        "passed": bool(subtasks) and has_test_step,
        "subtask_count": len(subtasks),
        "has_test_step": has_test_step,
        "summary": "Quality gate passed" if bool(subtasks) and has_test_step else "Quality gate failed",
    }


LOCAL_TOOL_EXECUTORS = {
    "claude_tools_plus.quality_gate": claude_tools_quality_gate,
}
