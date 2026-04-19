"""System-local tools for the artifact delivery system example."""

from __future__ import annotations

import uuid
from typing import Any


def artifact_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an artifact bundle summary from task payload."""
    task = payload.get("task") or payload.get("goal") or "artifact_delivery"
    files = payload.get("files") or []
    if not isinstance(files, list):
        files = [str(files)]
    return {
        "bundle_id": f"bundle-{uuid.uuid4().hex[:8]}",
        "task": task,
        "files": files,
        "artifact_count": len(files),
        "summary": f"Bundled {len(files)} artifact(s) for '{task}'",
    }


def artifact_validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate that a candidate artifact bundle contains the expected fields."""
    candidate = payload.get("candidate") or payload
    missing = [field for field in ["task", "files"] if field not in candidate]
    files = candidate.get("files") or []
    return {
        "passed": not missing and isinstance(files, list),
        "missing_fields": missing,
        "artifact_count": len(files) if isinstance(files, list) else 0,
    }


LOCAL_TOOL_EXECUTORS = {
    "artifact_delivery.bundle": artifact_bundle,
    "artifact_delivery.validate": artifact_validate,
}
