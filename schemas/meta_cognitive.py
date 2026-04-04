"""Schemas for the knowledge and meta-cognitive layer."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TaskType(BaseModel):
    task_type_id: str
    title: str
    description: str = ""
    properties: Dict[str, Any] = Field(default_factory=dict)
    signals: List[str] = Field(default_factory=list)
    related_capabilities: List[str] = Field(default_factory=list)


class Capability(BaseModel):
    capability_id: str
    title: str
    description: str = ""
    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    quality_dimensions: List[str] = Field(default_factory=list)
    task_types: List[str] = Field(default_factory=list)


class SemanticContract(BaseModel):
    system_id: str
    primary_task_types: List[str] = Field(default_factory=list)
    primary_capabilities: List[str] = Field(default_factory=list)
    forbidden_drift: List[str] = Field(default_factory=list)


class StrategyPattern(BaseModel):
    pattern_id: str
    title: str
    applies_to_task_types: List[str] = Field(default_factory=list)
    preconditions: List[str] = Field(default_factory=list)
    steps: List[str] = Field(default_factory=list)
    expected_benefits: List[str] = Field(default_factory=list)
    anti_patterns: List[str] = Field(default_factory=list)
    signals_of_success: List[str] = Field(default_factory=list)
    status: str = "draft"


class AntiPattern(BaseModel):
    anti_pattern_id: str
    title: str
    harm: List[str] = Field(default_factory=list)
    common_contexts: List[str] = Field(default_factory=list)
    signals: List[str] = Field(default_factory=list)
    status: str = "draft"


class DesignTemplateSlot(BaseModel):
    slot_id: str
    allowed_node_types: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    optional_capabilities: List[str] = Field(default_factory=list)
    cardinality: int = Field(default=1, ge=1)


class DesignTemplateEdge(BaseModel):
    from_slot: str
    to_slot: str
    condition_template: Dict[str, Any] = Field(default_factory=dict)


class DesignTemplate(BaseModel):
    template_id: str
    title: str
    applies_to_task_types: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    slots: Dict[str, DesignTemplateSlot] = Field(default_factory=dict)
    edges: List[DesignTemplateEdge] = Field(default_factory=list)
    defaults: Dict[str, Any] = Field(default_factory=dict)
    instantiation_rules: Dict[str, Any] = Field(default_factory=dict)
    status: str = "draft"


class ReflectionRecord(BaseModel):
    reflection_id: str
    task_type: str
    attempted_pattern: str
    system_used: str
    alternatives_considered: List[str] = Field(default_factory=list)
    decision_rationale: Dict[str, Any] = Field(default_factory=dict)
    outcome: Dict[str, Any] = Field(default_factory=dict)
    lessons: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class DecisionTrace(BaseModel):
    trace_id: str
    task_type: str
    steps: List[str] = Field(default_factory=list)
    outcome_link: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class CapabilityCompositionHypothesis(BaseModel):
    hypothesis_id: str
    inputs: List[str] = Field(default_factory=list)
    proposed_emergent_capability: str
    rationale: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: str = "draft"


class CoverageRecord(BaseModel):
    task_type: str
    required_capabilities: List[str] = Field(default_factory=list)
    covered_by: List[str] = Field(default_factory=list)
    coverage_score: float = Field(default=0.0, ge=0.0, le=1.0)
    known_gaps: List[str] = Field(default_factory=list)


class SystemLifecycleHealth(BaseModel):
    system_id: str
    usage_frequency: str = "unknown"
    dependency_count: int = 0
    last_successful_use: Optional[str] = None
    duplication_score: float = Field(default=0.0, ge=0.0, le=1.0)
    semantic_overlap_with: List[str] = Field(default_factory=list)
    recommended_action: str = "keep"


class PatternRegistryState(BaseModel):
    version: int = 1
    strategy_patterns: Dict[str, StrategyPattern] = Field(default_factory=dict)
    anti_patterns: Dict[str, AntiPattern] = Field(default_factory=dict)
    templates: Dict[str, DesignTemplate] = Field(default_factory=dict)
    composition_hypotheses: Dict[str, CapabilityCompositionHypothesis] = Field(default_factory=dict)
    reflections: Dict[str, ReflectionRecord] = Field(default_factory=dict)
    decision_traces: Dict[str, DecisionTrace] = Field(default_factory=dict)
