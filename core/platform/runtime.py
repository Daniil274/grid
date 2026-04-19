"""Execution runtime for system definitions."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from core.platform.conditions import ConditionEvaluationError, ConditionEvaluator
from core.platform.events import DomainEvent, DomainEventBus
from core.platform.compiler import CompiledSystemGraph, SystemCompiler
from core.platform.governance import PermissionChecker
from core.platform.registry import SystemRegistry
from schemas.system_platform import (
    AgentNodeDefinition,
    ExecutableNode,
    FailureMode,
    NodeExecutionResult,
    SideEffectRecord,
    SystemDefinition,
    SystemRefNodeDefinition,
    SystemRunResult,
    SystemRunStatus,
    ToolNodeDefinition,
)


class SystemRuntime:
    """Proxy-first runtime with graph-mode support for safe node execution."""

    def __init__(
        self,
        registry: SystemRegistry,
        *,
        compiler: Optional[SystemCompiler] = None,
        condition_evaluator: Optional[ConditionEvaluator] = None,
        permission_checker: Optional[PermissionChecker] = None,
        event_bus: Optional[DomainEventBus] = None,
        agent_executor: Optional[Callable[[str, Any], Any]] = None,
        tool_executor: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        self.registry = registry
        self.compiler = compiler or SystemCompiler()
        self.condition_evaluator = condition_evaluator or ConditionEvaluator()
        self.permission_checker = permission_checker or PermissionChecker()
        self.event_bus = event_bus or DomainEventBus()
        self.agent_executor = agent_executor
        self.tool_executor = tool_executor

    def invoke(
        self,
        system_id: str,
        *,
        channel: Optional[str] = "stable",
        version: Optional[str] = None,
        input_payload: Any = None,
        actor_role: str = "runtime_agent",
        call_depth: int = 0,
    ) -> SystemRunResult:
        definition = self.registry.get_definition(system_id, version=version, channel=channel)
        return self.invoke_definition(
            definition,
            input_payload=input_payload,
            actor_role=actor_role,
            call_depth=call_depth,
        )

    def invoke_definition(
        self,
        definition: SystemDefinition,
        *,
        input_payload: Any = None,
        actor_role: str = "runtime_agent",
        call_depth: int = 0,
    ) -> SystemRunResult:
        self.permission_checker.assert_allowed(
            definition.policy.permissions, actor_role, "invoke_system"
        )
        if call_depth >= definition.policy.max_system_call_depth:
            return SystemRunResult(
                system_id=definition.system_id,
                resolved_version=definition.version,
                status=SystemRunStatus.BLOCKED,
                warnings=[f"Max system call depth {definition.policy.max_system_call_depth} exceeded"],
            )

        compiled = self.compiler.compile(definition, dependency_resolver=self.registry._resolve_dependency)
        started_at = time.time()
        self.event_bus.publish(
            DomainEvent(
                event_type="system_invoked",
                payload={"system_id": definition.system_id, "version": definition.version},
            )
        )
        if definition.policy.execution_mode == "proxy":
            result = self._run_proxy_mode(definition, input_payload=input_payload, actor_role=actor_role, call_depth=call_depth)
        else:
            result = self._run_graph_mode(compiled, definition, input_payload=input_payload, actor_role=actor_role, call_depth=call_depth)
        result.metadata["duration_ms"] = int((time.time() - started_at) * 1000)
        return result

    def _run_proxy_mode(
        self,
        definition: SystemDefinition,
        *,
        input_payload: Any,
        actor_role: str,
        call_depth: int,
    ) -> SystemRunResult:
        node = definition.graph[definition.entrypoint]
        node_result = self._execute_node(
            definition.entrypoint,
            node,
            input_payload,
            actor_role=actor_role,
            call_depth=call_depth,
        )
        status = SystemRunStatus.COMPLETED if node_result.status == "completed" else SystemRunStatus.FAILED
        return SystemRunResult(
            system_id=definition.system_id,
            resolved_version=definition.version,
            status=status,
            final_output=node_result.output,
            node_results=[node_result],
            failed_nodes=[node_result.node_id] if node_result.status == "failed" else [],
            warnings=node_result.warnings,
            side_effects=node_result.side_effects,
        )

    def _run_graph_mode(
        self,
        compiled: CompiledSystemGraph,
        definition: SystemDefinition,
        *,
        input_payload: Any,
        actor_role: str,
        call_depth: int,
    ) -> SystemRunResult:
        runtime_context: Dict[str, Any] = {
            "input": input_payload or {},
            "state": {},
            "node_output": {},
            "context": {"flags": {}},
        }
        node_results: list[NodeExecutionResult] = []
        warnings: list[str] = []
        retry_trace: list[dict[str, Any]] = []
        failed_nodes: list[str] = []
        side_effects: list[SideEffectRecord] = []
        current_nodes = [compiled.entrypoint]
        visited: set[str] = set()

        while current_nodes:
            node_id = current_nodes.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)
            node = definition.graph[node_id]
            node_input = self._build_node_input(
                runtime_context["input"],
                runtime_context["node_output"],
                compiled.incoming.get(node_id, []),
            )
            result = self._execute_node(
                node_id,
                node,
                node_input,
                actor_role=actor_role,
                call_depth=call_depth,
            )
            node_results.append(result)
            runtime_context["node_output"][node_id] = result.output
            side_effects.extend(result.side_effects)
            if result.status == "failed":
                failed_nodes.append(node_id)
                action = node.failure_policy.on_error
                retry_trace.append({"node_id": node_id, "attempts": result.attempts, "mode": action.value})
                if action == FailureMode.FAIL_RUN:
                    return SystemRunResult(
                        system_id=definition.system_id,
                        resolved_version=definition.version,
                        status=SystemRunStatus.FAILED,
                        final_output=result.output,
                        node_results=node_results,
                        failed_nodes=failed_nodes,
                        warnings=warnings + result.warnings,
                        retry_trace=retry_trace,
                        side_effects=side_effects,
                    )
                if action == FailureMode.CONTINUE_WITH_WARNING:
                    warnings.extend(result.warnings or [f"Node '{node_id}' failed but execution continued"])
                if action == FailureMode.SKIP_NODE:
                    continue
                if action == FailureMode.FALLBACK_TO_NODE and node.failure_policy.fallback_node:
                    current_nodes.insert(0, node.failure_policy.fallback_node)
                    continue

            next_edges = compiled.adjacency.get(node_id, [])
            for edge in next_edges:
                if edge.when is None:
                    current_nodes.append(edge.to_node)
                    continue
                try:
                    if self.condition_evaluator.evaluate(edge.when, runtime_context):
                        current_nodes.append(edge.to_node)
                except ConditionEvaluationError as exc:
                    warnings.append(f"Edge {edge.from_node}->{edge.to_node} skipped: {exc}")

        final_output = node_results[-1].output if node_results else None
        return SystemRunResult(
            system_id=definition.system_id,
            resolved_version=definition.version,
            status=SystemRunStatus.COMPLETED if not failed_nodes else SystemRunStatus.FAILED,
            final_output=final_output,
            node_results=node_results,
            failed_nodes=failed_nodes,
            warnings=warnings,
            retry_trace=retry_trace,
            side_effects=side_effects,
        )

    def _build_node_input(
        self,
        root_input: Any,
        node_output: Dict[str, Any],
        incoming_edges: list,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"system_input": root_input, "node_output": dict(node_output)}
        if isinstance(root_input, dict):
            payload.update(root_input)
        else:
            payload["input"] = root_input

        upstream_nodes = [edge.from_node for edge in incoming_edges]
        for upstream_node in upstream_nodes:
            if upstream_node in node_output:
                payload[upstream_node] = node_output[upstream_node]

        if len(upstream_nodes) == 1 and upstream_nodes[0] in node_output:
            previous_output = node_output[upstream_nodes[0]]
            if isinstance(previous_output, dict):
                payload.update(previous_output)
            payload["previous"] = previous_output

        return payload

    def _execute_node(
        self,
        node_id: str,
        node: ExecutableNode,
        input_payload: Any,
        *,
        actor_role: str,
        call_depth: int,
    ) -> NodeExecutionResult:
        attempts = 0
        last_error: Optional[str] = None
        warnings: list[str] = []
        side_effects: list[SideEffectRecord] = []

        while attempts < node.failure_policy.retry.max_attempts:
            attempts += 1
            try:
                if isinstance(node, AgentNodeDefinition):
                    output = self.agent_executor(node.agent_ref, input_payload) if self.agent_executor else {
                        "proxy_agent": node.agent_ref,
                        "input": input_payload,
                    }
                elif isinstance(node, ToolNodeDefinition):
                    output = self.tool_executor(node.tool_ref, input_payload) if self.tool_executor else {
                        "proxy_tool": node.tool_ref,
                        "input": input_payload,
                    }
                elif isinstance(node, SystemRefNodeDefinition):
                    nested = self.invoke(
                        node.target_system,
                        channel=node.target_channel,
                        version=node.target_version,
                        input_payload=input_payload,
                        actor_role=actor_role,
                        call_depth=call_depth + 1,
                    )
                    warnings.extend(nested.warnings)
                    side_effects.extend(nested.side_effects)
                    output = nested.final_output
                else:
                    output = {"node_type": node.type, "input": input_payload}
                return NodeExecutionResult(
                    node_id=node_id,
                    status="completed",
                    output=output,
                    attempts=attempts,
                    warnings=warnings,
                    side_effects=side_effects,
                )
            except Exception as exc:  # pragma: no cover
                last_error = str(exc)
                warnings.append(f"Node '{node_id}' attempt {attempts} failed: {exc}")

        return NodeExecutionResult(
            node_id=node_id,
            status="failed",
            error=last_error,
            attempts=attempts,
            warnings=warnings,
            side_effects=side_effects,
        )
