"""Schemas for the system-builder prototype layer."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .system_platform import SystemDefinition, SystemReleaseState, SystemRunResult


class GeneratedToolSpec(BaseModel):
    """System-local tool source generated for a bundle."""

    tool_ref: str
    function_name: str
    description: str = ""
    python_code: str


class BuilderBundleSpec(BaseModel):
    """Generated bundle description returned by the builder model."""

    mode: Literal["create", "improve"]
    title: str
    description: str
    system_definition: SystemDefinition
    local_tools: List[GeneratedToolSpec] = Field(default_factory=list)
    test_payload: Dict[str, Any] = Field(default_factory=dict)
    review_instructions: str = ""
    notes: List[str] = Field(default_factory=list)


class BuilderRunReport(BaseModel):
    """Persisted result of a builder run."""

    request_text: str
    mode: Literal["create", "improve"]
    system_id: str
    version: str
    bundle_dir: str
    registry_path: str
    candidate_ready: bool
    promoted_to_stable: bool = False
    invocation_result: Optional[SystemRunResult] = None
    release_state: Optional[SystemReleaseState] = None
    created_files: List[str] = Field(default_factory=list)
    review_instructions: str = ""
    notes: List[str] = Field(default_factory=list)
