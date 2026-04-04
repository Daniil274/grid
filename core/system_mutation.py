"""Domain mutation operations for draft and candidate system definitions."""

from __future__ import annotations

from copy import deepcopy
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import TypeAdapter

from schemas.system_platform import ExecutableNode, SystemDefinition, SystemEdge, SystemInterface, SystemPolicy

NODE_ADAPTER = TypeAdapter(ExecutableNode)


class MutationKind(str, Enum):
    ADD_NODE = "add_node"
    REMOVE_NODE = "remove_node"
    UPDATE_NODE = "update_node"
    CONNECT_NODES = "connect_nodes"
    DISCONNECT_NODES = "disconnect_nodes"
    SET_ENTRYPOINT = "set_entrypoint"
    SET_INTERFACE = "set_interface"
    SET_POLICY = "set_policy"
    ADD_DEPENDENCY = "add_dependency"


class MutationSet:
    """Serializable list of domain mutations."""

    def __init__(self, mutations: Optional[List[Dict[str, Any]]] = None) -> None:
        self.mutations: List[Dict[str, Any]] = mutations or []

    def add(self, kind: MutationKind, **payload: Any) -> "MutationSet":
        self.mutations.append({"kind": kind.value, **payload})
        return self

    def to_list(self) -> List[Dict[str, Any]]:
        return list(self.mutations)


class DraftSystemBuilder:
    """Construct new draft system definitions from minimal inputs."""

    def create(
        self,
        *,
        system_id: str,
        version: str,
        entrypoint: str,
        interface: SystemInterface,
        nodes: Dict[str, ExecutableNode],
        edges: Optional[List[SystemEdge]] = None,
        policy: Optional[SystemPolicy] = None,
        default_agent: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
        task_types: Optional[List[str]] = None,
        capabilities: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SystemDefinition:
        return SystemDefinition(
            system_id=system_id,
            version=version,
            entrypoint=entrypoint,
            interface=interface,
            nodes=nodes,
            edges=edges or [],
            policy=policy or SystemPolicy(),
            default_agent=default_agent,
            dependencies=dependencies or [],
            task_types=task_types or [],
            capabilities=capabilities or [],
            metadata=metadata or {},
        )


class SystemMutator:
    """Apply bounded domain mutations to a system definition."""

    def apply(self, definition: SystemDefinition, mutation_set: MutationSet) -> SystemDefinition:
        updated = definition.model_copy(deep=True)
        for mutation in mutation_set.to_list():
            kind = MutationKind(mutation["kind"])
            if kind == MutationKind.ADD_NODE:
                node_id = mutation["node_id"]
                if node_id in updated.graph:
                    raise ValueError(f"Node '{node_id}' already exists")
                updated.graph[node_id] = NODE_ADAPTER.validate_python(deepcopy(mutation["node"]))
            elif kind == MutationKind.REMOVE_NODE:
                node_id = mutation["node_id"]
                if node_id not in updated.graph:
                    raise ValueError(f"Node '{node_id}' does not exist")
                del updated.graph[node_id]
                updated.edges = [
                    edge for edge in updated.edges if edge.from_node != node_id and edge.to_node != node_id
                ]
                if updated.entrypoint == node_id:
                    raise ValueError("Cannot remove current entrypoint node")
            elif kind == MutationKind.UPDATE_NODE:
                node_id = mutation["node_id"]
                if node_id not in updated.graph:
                    raise ValueError(f"Node '{node_id}' does not exist")
                updated.graph[node_id] = NODE_ADAPTER.validate_python(deepcopy(mutation["node"]))
            elif kind == MutationKind.CONNECT_NODES:
                edge = SystemEdge(**{"from": mutation["from_node"], "to": mutation["to_node"], "when": mutation.get("when")})
                updated.edges.append(edge)
            elif kind == MutationKind.DISCONNECT_NODES:
                updated.edges = [
                    edge
                    for edge in updated.edges
                    if not (edge.from_node == mutation["from_node"] and edge.to_node == mutation["to_node"])
                ]
            elif kind == MutationKind.SET_ENTRYPOINT:
                entrypoint = mutation["entrypoint"]
                if entrypoint not in updated.graph:
                    raise ValueError(f"Entrypoint '{entrypoint}' does not exist")
                updated.entrypoint = entrypoint
            elif kind == MutationKind.SET_INTERFACE:
                updated.interface = SystemInterface(**mutation["interface"])
            elif kind == MutationKind.SET_POLICY:
                updated.policy = SystemPolicy(**mutation["policy"])
            elif kind == MutationKind.ADD_DEPENDENCY:
                dependency = mutation["dependency"]
                if dependency not in updated.dependencies:
                    updated.dependencies.append(dependency)
            else:  # pragma: no cover
                raise ValueError(f"Unsupported mutation kind: {kind}")
        return updated
