"""Schemas for the improvement and self-evolution control loop."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .benchmarking import BenchmarkScorecard
from .schemas import ImprovementConfig


class ImprovementProblemStatus(str, Enum):
    """Lifecycle states for an observed improvement problem."""

    OBSERVED = "observed"
    REQUIREMENTS_DRAFT = "requirements_draft"
    REQUIREMENTS_APPROVED = "requirements_approved"
    EXPERIMENT_ACTIVE = "experiment_active"
    SOLVED = "solved"
    REJECTED = "rejected"


class ImprovementExperimentStatus(str, Enum):
    """Lifecycle states for an improvement experiment."""

    DRAFT = "draft"
    REQUIREMENTS_REVIEW = "requirements_review"
    IMPLEMENTATION_READY = "implementation_ready"
    EVALUATION_RUNNING = "evaluation_running"
    EVALUATION_PASSED = "evaluation_passed"
    IN_REVIEW = "in_review"
    PROMOTION_PENDING = "promotion_pending"
    CANARY = "canary"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class ImprovementReviewType(str, Enum):
    """Supported review checkpoints."""

    REQUIREMENTS = "requirements"
    FINAL = "final"


class ImprovementReviewDecision(str, Enum):
    """Possible review outcomes."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class ImprovementRisk(str, Enum):
    """Risk level for an experiment."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ImprovementConfigDiff(BaseModel):
    """Single config mutation proposed by an improvement experiment."""

    path: str
    old: Any = None
    new: Any


class ImprovementReview(BaseModel):
    """Review record attached to a problem or experiment."""

    id: str
    review_type: ImprovementReviewType
    target_id: str
    decision: ImprovementReviewDecision
    reviewer: str
    reviewer_kind: str = Field(default="agent", description="human or agent")
    summary: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ImprovementProblem(BaseModel):
    """Observed problem that may lead to one or more experiments."""

    id: str
    title: str
    summary: str
    requirements: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)
    status: ImprovementProblemStatus = ImprovementProblemStatus.OBSERVED
    priority: str = "medium"
    source: str = "agent"
    owner: Optional[str] = None
    related_paths: List[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: Dict[str, Any] = Field(default_factory=dict)
    reviews: List[ImprovementReview] = Field(default_factory=list)
    experiment_ids: List[str] = Field(default_factory=list)


class ImprovementExperiment(BaseModel):
    """Concrete attempt to improve the system safely."""

    id: str
    problem_id: str
    title: str
    hypothesis: str
    status: ImprovementExperimentStatus = ImprovementExperimentStatus.DRAFT
    risk: ImprovementRisk = ImprovementRisk.MEDIUM
    change_type: str = "unspecified"
    allowed_paths: List[str] = Field(default_factory=list)
    config_diff: List[ImprovementConfigDiff] = Field(default_factory=list)
    branch_name: Optional[str] = None
    created_by: str = "agent"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    metrics: Dict[str, Any] = Field(default_factory=dict)
    baseline_scorecard: Optional[BenchmarkScorecard] = None
    candidate_scorecard: Optional[BenchmarkScorecard] = None
    evaluation_summary: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    reviews: List[ImprovementReview] = Field(default_factory=list)
    promotion_notes: Optional[str] = None


class ImprovementRegistryState(BaseModel):
    """Persistent state stored by the improvement registry."""

    version: int = 1
    problems: Dict[str, ImprovementProblem] = Field(default_factory=dict)
    experiments: Dict[str, ImprovementExperiment] = Field(default_factory=dict)
