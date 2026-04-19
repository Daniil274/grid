"""Knowledge and meta-cognitive helpers for the system platform MVP."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional

from core.platform.events import DomainEvent, DomainEventBus
from schemas.meta_cognitive import (
    AntiPattern,
    Capability,
    CapabilityCompositionHypothesis,
    CoverageRecord,
    DecisionTrace,
    DesignTemplate,
    ReflectionRecord,
    SemanticContract,
    StrategyPattern,
    SystemLifecycleHealth,
    TaskType,
    PatternRegistryState,
)
from schemas.system_platform import (
    AgentNodeDefinition,
    SystemDefinition,
    SystemEdge,
    SystemInterface,
    SystemPolicy,
    ToolNodeDefinition,
    SystemRefNodeDefinition,
)


class TaskOntology:
    def __init__(self) -> None:
        self._task_types: Dict[str, TaskType] = {}

    def register(self, task_type: TaskType) -> TaskType:
        self._task_types[task_type.task_type_id] = task_type
        return task_type

    def get(self, task_type_id: str) -> TaskType:
        return self._task_types[task_type_id]

    def list(self) -> List[TaskType]:
        return sorted(self._task_types.values(), key=lambda item: item.task_type_id)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: Dict[str, Capability] = {}
        self._contracts: Dict[str, SemanticContract] = {}

    def register_capability(self, capability: Capability) -> Capability:
        self._capabilities[capability.capability_id] = capability
        return capability

    def set_semantic_contract(self, contract: SemanticContract) -> SemanticContract:
        self._contracts[contract.system_id] = contract
        return contract

    def get_contract(self, system_id: str) -> Optional[SemanticContract]:
        return self._contracts.get(system_id)

    def list_capabilities(self) -> List[Capability]:
        return sorted(self._capabilities.values(), key=lambda item: item.capability_id)


class PatternRegistry:
    """File-backed registry for patterns, templates, and reflection artifacts."""

    def __init__(self, path: str | Path = "data/pattern_registry.json", *, event_bus: Optional[DomainEventBus] = None) -> None:
        self._path = Path(path)
        self._lock = RLock()
        self._event_bus = event_bus or DomainEventBus()
        self._state = self._load()

    def _load(self) -> PatternRegistryState:
        if not self._path.exists():
            return PatternRegistryState()
        with self._path.open("r", encoding="utf-8") as fh:
            return PatternRegistryState(**json.load(fh))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            json.dump(self._state.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)

    def register_pattern(self, pattern: StrategyPattern) -> StrategyPattern:
        with self._lock:
            self._state.strategy_patterns[pattern.pattern_id] = pattern
            self._save()
            self._event_bus.publish(DomainEvent("pattern_draft_created", {"pattern_id": pattern.pattern_id}))
            return pattern

    def register_template(self, template: DesignTemplate) -> DesignTemplate:
        with self._lock:
            self._state.templates[template.template_id] = template
            self._save()
            return template

    def register_anti_pattern(self, anti_pattern: AntiPattern) -> AntiPattern:
        with self._lock:
            self._state.anti_patterns[anti_pattern.anti_pattern_id] = anti_pattern
            self._save()
            return anti_pattern

    def register_hypothesis(self, hypothesis: CapabilityCompositionHypothesis) -> CapabilityCompositionHypothesis:
        with self._lock:
            self._state.composition_hypotheses[hypothesis.hypothesis_id] = hypothesis
            self._save()
            return hypothesis

    def store_reflection(self, reflection: ReflectionRecord) -> ReflectionRecord:
        with self._lock:
            self._state.reflections[reflection.reflection_id] = reflection
            self._save()
            self._event_bus.publish(DomainEvent("reflection_record_created", {"reflection_id": reflection.reflection_id}))
            return reflection

    def store_decision_trace(self, trace: DecisionTrace) -> DecisionTrace:
        with self._lock:
            self._state.decision_traces[trace.trace_id] = trace
            self._save()
            return trace

    def get_template(self, template_id: str) -> DesignTemplate:
        return self._state.templates[template_id]


class ReflectionStore:
    def __init__(self, registry: PatternRegistry) -> None:
        self.registry = registry

    def record(self, reflection: ReflectionRecord, trace: Optional[DecisionTrace] = None) -> ReflectionRecord:
        stored = self.registry.store_reflection(reflection)
        if trace is not None:
            self.registry.store_decision_trace(trace)
        return stored


class TemplateInstantiationError(ValueError):
    """Raised when a design template cannot be instantiated safely."""


class TemplateInstantiator:
    """Instantiate parameterized graph templates into draft system definitions."""

    NODE_TYPE_FACTORY = {
        "agent_node": lambda ref, caps: AgentNodeDefinition(agent_ref=ref, capabilities=caps),
        "tool_node": lambda ref, caps: ToolNodeDefinition(tool_ref=ref, capabilities=caps),
        "system_ref_node": lambda ref, caps: SystemRefNodeDefinition(target_system=ref, capabilities=caps),
    }

    def instantiate(
        self,
        *,
        template: DesignTemplate,
        bindings: Dict[str, Dict[str, object]],
        task_type: str,
        system_id: str,
        version: str,
        input_schema: str,
        output_schema: str,
        entrypoint: Optional[str] = None,
    ) -> tuple[SystemDefinition, List[str]]:
        unresolved: List[str] = []
        graph: Dict[str, object] = {}
        edge_defs: List[SystemEdge] = []

        for slot_id, slot in template.slots.items():
            binding = bindings.get(slot_id)
            if binding is None:
                unresolved.append(f"missing_capability_binding:{slot_id}")
                continue
            node_type = str(binding.get("node_type", ""))
            target_ref = str(binding.get("ref", ""))
            capabilities = list(binding.get("capabilities", []))
            if node_type not in slot.allowed_node_types:
                unresolved.append(f"invalid_slot_binding:{slot_id}")
                continue
            missing_caps = [cap for cap in slot.required_capabilities if cap not in capabilities]
            if missing_caps:
                unresolved.append(f"missing_required_capabilities:{slot_id}:{','.join(missing_caps)}")
                continue
            factory = self.NODE_TYPE_FACTORY.get(node_type)
            if factory is None:
                unresolved.append(f"unsupported_node_type:{slot_id}:{node_type}")
                continue
            graph[slot_id] = factory(target_ref, capabilities)

        for edge in template.edges:
            if edge.from_slot in graph and edge.to_slot in graph:
                edge_defs.append(SystemEdge(**{"from": edge.from_slot, "to": edge.to_slot}))

        if not graph:
            raise TemplateInstantiationError("Template instantiation produced an empty graph")

        return (
            SystemDefinition(
                system_id=system_id,
                version=version,
                entrypoint=entrypoint or next(iter(graph.keys())),
                interface=SystemInterface(input_schema=input_schema, output_schema=output_schema),
                nodes=graph,
                edges=edge_defs,
                policy=SystemPolicy(execution_mode="graph"),
                task_types=[task_type],
                capabilities=template.required_capabilities,
                metadata={"title": template.title, "template_id": template.template_id},
            ),
            unresolved,
        )


class CoverageIndex:
    """Derived view mapping task types to system coverage."""

    def build(
        self,
        *,
        task_types: List[TaskType],
        systems: List[SystemDefinition],
        capabilities: List[Capability],
    ) -> List[CoverageRecord]:
        capability_ids = {cap.capability_id for cap in capabilities}
        records: List[CoverageRecord] = []
        for task_type in task_types:
            required = [cap for cap in task_type.related_capabilities if cap in capability_ids]
            covered_by = [
                f"{system.system_id}@{system.version}"
                for system in systems
                if set(required).issubset(set(system.capabilities))
            ]
            score = 1.0 if required and covered_by else 0.0
            gaps = [] if score == 1.0 else required
            records.append(
                CoverageRecord(
                    task_type=task_type.task_type_id,
                    required_capabilities=required,
                    covered_by=covered_by,
                    coverage_score=score,
                    known_gaps=gaps,
                )
            )
        return records


class SemanticDriftMonitor:
    """Detect semantic drift against registered semantic contracts."""

    def check(self, definition: SystemDefinition, contract: Optional[SemanticContract]) -> List[str]:
        if contract is None:
            return []
        issues: List[str] = []
        if contract.primary_task_types and not set(definition.task_types).intersection(contract.primary_task_types):
            issues.append("primary_task_types_drift")
        if contract.primary_capabilities and not set(contract.primary_capabilities).issubset(set(definition.capabilities)):
            issues.append("primary_capabilities_drift")
        for forbidden in contract.forbidden_drift:
            if forbidden in definition.metadata.get("description", ""):
                issues.append(f"forbidden_drift:{forbidden}")
        return issues


class LifecycleHealthAnalyzer:
    """Simple system health analyzer for consolidation heuristics."""

    def analyze(
        self,
        *,
        system_id: str,
        dependency_count: int,
        usage_frequency: str,
        duplication_score: float,
        semantic_overlap_with: List[str],
        last_successful_use: Optional[str] = None,
    ) -> SystemLifecycleHealth:
        action = "keep"
        if duplication_score >= 0.8:
            action = "consolidate"
        elif usage_frequency == "low" and dependency_count == 0:
            action = "archive"
        return SystemLifecycleHealth(
            system_id=system_id,
            usage_frequency=usage_frequency,
            dependency_count=dependency_count,
            duplication_score=duplication_score,
            semantic_overlap_with=semantic_overlap_with,
            last_successful_use=last_successful_use,
            recommended_action=action,
        )
