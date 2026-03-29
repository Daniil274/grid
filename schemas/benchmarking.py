"""Schemas for benchmark fixtures, metrics, and scorecards."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class BenchmarkMetricDefinition(BaseModel):
    """Single benchmark metric definition."""

    name: str
    description: str
    threshold: Optional[float] = None
    target: str = Field(default="informational", description="pass_rate, upper_bound, lower_bound, informational")
    rationale: Optional[str] = None


class BenchmarkMetricsDocument(BaseModel):
    """Benchmark metric configuration document."""

    version: int = 1
    metrics: List[BenchmarkMetricDefinition] = Field(default_factory=list)


class BenchmarkFixture(BaseModel):
    """Single benchmark fixture."""

    id: str
    prompt: str
    agent: str = "chat_agent"
    owner: str
    source: str
    tags: List[str] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    required_tools: List[str] = Field(default_factory=list)
    forbidden_tools: List[str] = Field(default_factory=list)
    required_substrings: List[str] = Field(default_factory=list)
    forbidden_substrings: List[str] = Field(default_factory=list)
    max_latency_ms: Optional[int] = Field(default=None, ge=1)
    notes: Optional[str] = None


class BenchmarkAdapterResult(BaseModel):
    """Result returned by a benchmark execution adapter."""

    output: str
    tools_used: List[str] = Field(default_factory=list)
    latency_ms: int = Field(default=0, ge=0)
    token_usage: Optional[Dict[str, int]] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BenchmarkFixtureResult(BaseModel):
    """Evaluated result for one fixture."""

    fixture_id: str
    passed: bool
    output: str
    tools_used: List[str] = Field(default_factory=list)
    latency_ms: int = Field(default=0, ge=0)
    token_usage: Optional[Dict[str, int]] = None
    checks: Dict[str, bool] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BenchmarkScorecard(BaseModel):
    """Aggregate benchmark scorecard."""

    run_id: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    config_path: str
    metrics_path: str
    fixture_count: int = Field(default=0, ge=0)
    passed_count: int = Field(default=0, ge=0)
    aggregate_score: float = Field(default=0.0, ge=0.0, le=1.0)
    average_latency_ms: float = Field(default=0.0, ge=0.0)
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)
    results: List[BenchmarkFixtureResult] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)
