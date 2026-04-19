"""Compiler for system definitions into a validated executable graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from schemas.system_platform import SystemDefinition, SystemEdge, SystemRefNodeDefinition


class SystemCompileError(ValueError):
    """Raised when a system definition cannot be compiled safely."""


@dataclass
class CompiledSystemGraph:
    system_id: str
    version: str
    entrypoint: str
    node_ids: List[str]
    adjacency: Dict[str, List[SystemEdge]]
    incoming: Dict[str, List[SystemEdge]]


class SystemCompiler:
    """Validate and compile system definitions."""

    def compile(
        self,
        definition: SystemDefinition,
        *,
        dependency_resolver: Optional[callable] = None,
    ) -> CompiledSystemGraph:
        self._validate_graph_acyclic(definition)
        self._validate_dependency_cycles(
            definition,
            dependency_resolver=dependency_resolver,
            current_stack=[],
        )
        adjacency: Dict[str, List[SystemEdge]] = {node_id: [] for node_id in definition.graph}
        incoming: Dict[str, List[SystemEdge]] = {node_id: [] for node_id in definition.graph}
        for edge in definition.edges:
            adjacency[edge.from_node].append(edge)
            incoming[edge.to_node].append(edge)
        return CompiledSystemGraph(
            system_id=definition.system_id,
            version=definition.version,
            entrypoint=definition.entrypoint,
            node_ids=list(definition.graph.keys()),
            adjacency=adjacency,
            incoming=incoming,
        )

    def _validate_graph_acyclic(self, definition: SystemDefinition) -> None:
        adjacency: Dict[str, List[str]] = {node_id: [] for node_id in definition.graph}
        for edge in definition.edges:
            adjacency[edge.from_node].append(edge.to_node)

        visited: Set[str] = set()
        in_stack: Set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in in_stack:
                raise SystemCompileError(f"Cycle detected in system graph at node '{node_id}'")
            if node_id in visited:
                return
            in_stack.add(node_id)
            for child in adjacency.get(node_id, []):
                visit(child)
            in_stack.remove(node_id)
            visited.add(node_id)

        for node_id in definition.graph:
            visit(node_id)

    def _validate_dependency_cycles(
        self,
        definition: SystemDefinition,
        *,
        dependency_resolver: Optional[callable],
        current_stack: List[str],
    ) -> None:
        if dependency_resolver is None:
            return

        stack = [*current_stack, definition.system_id]
        for node in definition.graph.values():
            if not isinstance(node, SystemRefNodeDefinition):
                continue
            if node.target_system in stack:
                raise SystemCompileError(
                    f"System dependency cycle detected: {' -> '.join([*stack, node.target_system])}"
                )
            child = dependency_resolver(node.target_system, node.target_version, node.target_channel)
            if child is None:
                continue
            self._validate_dependency_cycles(
                child,
                dependency_resolver=dependency_resolver,
                current_stack=stack,
            )
