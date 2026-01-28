"""
Pipeline Memory - Evolutionary storage for successful agent pipelines.

Key concepts:
- Agents build pipelines from primitives (execute, critique, validate, etc.)
- Successful pipelines are stored with task patterns and scores
- Similar tasks can reuse or adapt proven pipelines
- Pipelines evolve: modifications create new versions

This enables emergent learning: the system improves over time
by remembering what works for different types of tasks.
"""

from __future__ import annotations

import json
import uuid
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Dict, List, Any, Optional, Tuple
from difflib import SequenceMatcher


@dataclass
class PipelineStep:
    """Single step in a pipeline."""
    primitive: str  # execute, critique, validate, synthesize, branch, vote
    params: Dict[str, Any] = field(default_factory=dict)
    input_from: Optional[str] = None  # Reference to previous step output
    output_key: str = "output"  # Key for storing this step's output
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineStep":
        return cls(**data)


@dataclass
class PipelineRecord:
    """A recorded pipeline with its performance history."""
    id: str
    name: str
    description: str
    task_pattern: str  # Pattern or keywords that match this pipeline
    task_examples: List[str]  # Example tasks this worked for
    steps: List[PipelineStep]
    success_score: float  # Average success score (0-1)
    usage_count: int
    success_count: int
    failure_count: int
    created_at: str
    last_used: str
    parent_id: Optional[str] = None  # If evolved from another pipeline
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["steps"] = [s.to_dict() if hasattr(s, 'to_dict') else s for s in self.steps]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineRecord":
        steps_data = data.pop("steps", [])
        steps = [
            PipelineStep.from_dict(s) if isinstance(s, dict) else s
            for s in steps_data
        ]
        return cls(steps=steps, **data)

    def get_effectiveness(self) -> float:
        """Calculate effectiveness score considering usage and success rate."""
        if self.usage_count == 0:
            return 0.0

        success_rate = self.success_count / self.usage_count
        # Blend success score with success rate, weighted by usage
        confidence = min(1.0, self.usage_count / 10)  # Max confidence at 10 uses
        return (self.success_score * 0.6 + success_rate * 0.4) * confidence


class PipelineMemory:
    """
    Evolutionary memory for successful pipelines.

    Key features:
    - Store pipelines with task patterns
    - Find similar pipelines for new tasks
    - Track success/failure statistics
    - Evolve pipelines by modification
    - Suggest pipelines based on task similarity
    """

    def __init__(
        self,
        persist_path: Optional[str] = None,
        max_pipelines: int = 500,
        similarity_threshold: float = 0.7,
        min_success_score: float = 0.6
    ):
        """
        Initialize pipeline memory.

        Args:
            persist_path: Path to JSON file for persistence
            max_pipelines: Maximum pipelines to keep
            similarity_threshold: Minimum similarity for matching
            min_success_score: Minimum score to consider a pipeline successful
        """
        self._lock = RLock()
        self.persist_path = persist_path
        self.max_pipelines = max_pipelines
        self.similarity_threshold = similarity_threshold
        self.min_success_score = min_success_score

        self._pipelines: Dict[str, PipelineRecord] = {}
        self._task_index: Dict[str, List[str]] = {}  # keyword -> pipeline_ids

        if persist_path:
            self._load()

    def save_pipeline(
        self,
        name: str,
        description: str,
        task: str,
        steps: List[Dict[str, Any]],
        success_score: float,
        tags: Optional[List[str]] = None,
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Save a new pipeline or update existing.

        Args:
            name: Human-readable name
            description: What this pipeline does
            task: The task it was used for
            steps: List of pipeline steps
            success_score: How well it performed (0-1)
            tags: Categorization tags
            parent_id: If evolved from another pipeline
            metadata: Additional metadata

        Returns:
            ID of the saved pipeline
        """
        with self._lock:
            pipeline_id = f"pipe-{uuid.uuid4().hex[:8]}"

            # Convert steps to PipelineStep objects
            pipeline_steps = [
                PipelineStep.from_dict(s) if isinstance(s, dict) else s
                for s in steps
            ]

            # Extract task pattern (keywords)
            task_pattern = self._extract_task_pattern(task)

            record = PipelineRecord(
                id=pipeline_id,
                name=name,
                description=description,
                task_pattern=task_pattern,
                task_examples=[task],
                steps=pipeline_steps,
                success_score=success_score,
                usage_count=1,
                success_count=1 if success_score >= self.min_success_score else 0,
                failure_count=0 if success_score >= self.min_success_score else 1,
                created_at=datetime.now().isoformat(),
                last_used=datetime.now().isoformat(),
                parent_id=parent_id,
                tags=tags or [],
                metadata=metadata or {}
            )

            self._pipelines[pipeline_id] = record
            self._index_pipeline(record)

            # Cleanup if needed
            self._cleanup_old_pipelines()

            if self.persist_path:
                self._save()

            return pipeline_id

    def find_similar(
        self,
        task: str,
        threshold: Optional[float] = None,
        limit: int = 5,
        min_effectiveness: float = 0.0
    ) -> List[Tuple[PipelineRecord, float]]:
        """
        Find pipelines similar to the given task.

        Args:
            task: Task description to match
            threshold: Similarity threshold (default: instance threshold)
            limit: Maximum results to return
            min_effectiveness: Minimum effectiveness score

        Returns:
            List of (pipeline, similarity_score) tuples, sorted by relevance
        """
        threshold = threshold or self.similarity_threshold

        with self._lock:
            candidates = []

            task_keywords = self._extract_keywords(task)
            task_lower = task.lower()

            for pipeline in self._pipelines.values():
                # Skip ineffective pipelines
                if pipeline.get_effectiveness() < min_effectiveness:
                    continue

                # Calculate similarity
                similarity = self._calculate_similarity(
                    task_lower,
                    task_keywords,
                    pipeline
                )

                if similarity >= threshold:
                    candidates.append((pipeline, similarity))

            # Sort by similarity * effectiveness
            candidates.sort(
                key=lambda x: x[1] * x[0].get_effectiveness(),
                reverse=True
            )

            return candidates[:limit]

    def get_best_for_task(self, task: str) -> Optional[PipelineRecord]:
        """Get the single best pipeline for a task."""
        similar = self.find_similar(task, limit=1)
        if similar:
            return similar[0][0]
        return None

    def record_usage(
        self,
        pipeline_id: str,
        success: bool,
        score: Optional[float] = None,
        task: Optional[str] = None
    ):
        """
        Record a pipeline usage for learning.

        Args:
            pipeline_id: ID of the used pipeline
            success: Whether the usage was successful
            score: Detailed success score (0-1)
            task: The task it was used for (to add as example)
        """
        with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if not pipeline:
                return

            pipeline.usage_count += 1
            pipeline.last_used = datetime.now().isoformat()

            if success:
                pipeline.success_count += 1
            else:
                pipeline.failure_count += 1

            # Update success score (exponential moving average)
            if score is not None:
                alpha = 0.3  # Weight for new observation
                pipeline.success_score = (
                    alpha * score + (1 - alpha) * pipeline.success_score
                )

            # Add task example if new
            if task and task not in pipeline.task_examples:
                pipeline.task_examples.append(task)
                # Keep only last 10 examples
                if len(pipeline.task_examples) > 10:
                    pipeline.task_examples = pipeline.task_examples[-10:]
                # Update pattern
                pipeline.task_pattern = self._extract_task_pattern(
                    " ".join(pipeline.task_examples)
                )

            if self.persist_path:
                self._save()

    def evolve_pipeline(
        self,
        base_id: str,
        modifications: Dict[str, Any],
        new_name: Optional[str] = None,
        reason: str = ""
    ) -> Optional[str]:
        """
        Create an evolved version of a pipeline.

        Args:
            base_id: ID of the base pipeline
            modifications: What to change (add_step, remove_step, modify_step)
            new_name: Name for the new pipeline
            reason: Why this evolution was made

        Returns:
            ID of the new pipeline, or None if base not found
        """
        with self._lock:
            base = self._pipelines.get(base_id)
            if not base:
                return None

            # Deep copy steps
            new_steps = [
                PipelineStep.from_dict(s.to_dict())
                for s in base.steps
            ]

            # Apply modifications
            if "add_step" in modifications:
                step_data = modifications["add_step"]
                position = step_data.get("position", len(new_steps))
                new_step = PipelineStep.from_dict(step_data.get("step", {}))
                new_steps.insert(position, new_step)

            if "remove_step" in modifications:
                index = modifications["remove_step"]
                if 0 <= index < len(new_steps):
                    new_steps.pop(index)

            if "modify_step" in modifications:
                mod = modifications["modify_step"]
                index = mod.get("index", 0)
                if 0 <= index < len(new_steps):
                    for key, value in mod.get("changes", {}).items():
                        if hasattr(new_steps[index], key):
                            setattr(new_steps[index], key, value)

            # Create new pipeline
            new_id = f"pipe-{uuid.uuid4().hex[:8]}"
            new_record = PipelineRecord(
                id=new_id,
                name=new_name or f"{base.name} (evolved)",
                description=f"Evolved from {base.name}. {reason}",
                task_pattern=base.task_pattern,
                task_examples=[],  # Start fresh
                steps=new_steps,
                success_score=base.success_score * 0.9,  # Slight penalty for untested
                usage_count=0,
                success_count=0,
                failure_count=0,
                created_at=datetime.now().isoformat(),
                last_used=datetime.now().isoformat(),
                parent_id=base_id,
                tags=base.tags + ["evolved"],
                metadata={"evolution_reason": reason, **base.metadata}
            )

            self._pipelines[new_id] = new_record
            self._index_pipeline(new_record)

            if self.persist_path:
                self._save()

            return new_id

    def get_pipeline(self, pipeline_id: str) -> Optional[PipelineRecord]:
        """Get a pipeline by ID."""
        with self._lock:
            return self._pipelines.get(pipeline_id)

    def list_pipelines(
        self,
        tags: Optional[List[str]] = None,
        min_effectiveness: float = 0.0,
        limit: int = 50
    ) -> List[PipelineRecord]:
        """List pipelines with optional filters."""
        with self._lock:
            results = list(self._pipelines.values())

            if tags:
                results = [
                    p for p in results
                    if any(t in p.tags for t in tags)
                ]

            if min_effectiveness > 0:
                results = [
                    p for p in results
                    if p.get_effectiveness() >= min_effectiveness
                ]

            # Sort by effectiveness
            results.sort(key=lambda x: x.get_effectiveness(), reverse=True)

            return results[:limit]

    def delete_pipeline(self, pipeline_id: str) -> bool:
        """Delete a pipeline."""
        with self._lock:
            if pipeline_id in self._pipelines:
                del self._pipelines[pipeline_id]
                # Remove from index
                for keyword, ids in self._task_index.items():
                    if pipeline_id in ids:
                        ids.remove(pipeline_id)
                if self.persist_path:
                    self._save()
                return True
            return False

    def get_statistics(self) -> Dict[str, Any]:
        """Get memory statistics."""
        with self._lock:
            total = len(self._pipelines)
            if total == 0:
                return {"total": 0}

            scores = [p.success_score for p in self._pipelines.values()]
            usages = [p.usage_count for p in self._pipelines.values()]

            return {
                "total": total,
                "avg_success_score": sum(scores) / total,
                "total_usages": sum(usages),
                "most_used": max(self._pipelines.values(), key=lambda x: x.usage_count).name,
                "most_effective": max(self._pipelines.values(), key=lambda x: x.get_effectiveness()).name,
                "tags": list(set(t for p in self._pipelines.values() for t in p.tags))
            }

    def _extract_task_pattern(self, task: str) -> str:
        """Extract a pattern from a task description."""
        keywords = self._extract_keywords(task)
        return " ".join(sorted(keywords)[:10])  # Top 10 keywords

    def _extract_keywords(self, text: str) -> set:
        """Extract keywords from text."""
        # Simple keyword extraction
        text = text.lower()

        # Remove common stop words
        stop_words = {
            'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
            'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
            'would', 'could', 'should', 'may', 'might', 'must', 'shall',
            'can', 'need', 'dare', 'ought', 'used', 'to', 'of', 'in',
            'for', 'on', 'with', 'at', 'by', 'from', 'as', 'into',
            'through', 'during', 'before', 'after', 'above', 'below',
            'between', 'under', 'again', 'further', 'then', 'once',
            'и', 'в', 'на', 'с', 'по', 'для', 'к', 'за', 'от', 'из',
            'у', 'о', 'об', 'до', 'при', 'что', 'как', 'это', 'все',
            'так', 'же', 'не', 'да', 'нет', 'но', 'или', 'если', 'то'
        }

        # Extract words
        words = re.findall(r'\b\w+\b', text)
        keywords = {w for w in words if len(w) > 2 and w not in stop_words}

        return keywords

    def _calculate_similarity(
        self,
        task_lower: str,
        task_keywords: set,
        pipeline: PipelineRecord
    ) -> float:
        """Calculate similarity between task and pipeline."""
        # Keyword overlap
        pattern_keywords = self._extract_keywords(pipeline.task_pattern)
        if not pattern_keywords:
            return 0.0

        keyword_overlap = len(task_keywords & pattern_keywords) / len(
            task_keywords | pattern_keywords
        )

        # Example similarity (best match)
        example_similarity = 0.0
        for example in pipeline.task_examples:
            sim = SequenceMatcher(None, task_lower, example.lower()).ratio()
            example_similarity = max(example_similarity, sim)

        # Combine scores
        return keyword_overlap * 0.6 + example_similarity * 0.4

    def _index_pipeline(self, pipeline: PipelineRecord):
        """Index pipeline for fast keyword lookup."""
        keywords = self._extract_keywords(pipeline.task_pattern)
        for keyword in keywords:
            if keyword not in self._task_index:
                self._task_index[keyword] = []
            if pipeline.id not in self._task_index[keyword]:
                self._task_index[keyword].append(pipeline.id)

    def _cleanup_old_pipelines(self):
        """Remove least effective pipelines if over limit."""
        with self._lock:
            if len(self._pipelines) <= self.max_pipelines:
                return

            # Sort by effectiveness
            sorted_pipelines = sorted(
                self._pipelines.values(),
                key=lambda x: x.get_effectiveness()
            )

            # Remove lowest performers
            to_remove = len(self._pipelines) - self.max_pipelines
            for pipeline in sorted_pipelines[:to_remove]:
                self.delete_pipeline(pipeline.id)

    def _save(self):
        """Save to JSON file."""
        if not self.persist_path:
            return

        with self._lock:
            data = {
                "pipelines": [p.to_dict() for p in self._pipelines.values()]
            }

            path = Path(self.persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    def _load(self):
        """Load from JSON file."""
        if not self.persist_path:
            return

        path = Path(self.persist_path)
        if not path.exists():
            return

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            with self._lock:
                for pipeline_data in data.get("pipelines", []):
                    pipeline = PipelineRecord.from_dict(pipeline_data)
                    self._pipelines[pipeline.id] = pipeline
                    self._index_pipeline(pipeline)

        except Exception as e:
            print(f"Failed to load pipeline memory from {self.persist_path}: {e}")


# Global instance
_global_pipeline_memory: Optional[PipelineMemory] = None


def get_global_pipeline_memory() -> PipelineMemory:
    """Get or create the global pipeline memory."""
    global _global_pipeline_memory
    if _global_pipeline_memory is None:
        _global_pipeline_memory = PipelineMemory()
    return _global_pipeline_memory


def set_global_pipeline_memory(memory: PipelineMemory):
    """Set the global pipeline memory."""
    global _global_pipeline_memory
    _global_pipeline_memory = memory
