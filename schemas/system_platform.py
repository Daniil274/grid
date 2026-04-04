"""Schemas for the self-organizing system platform foundation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union, Annotated

from pydantic import BaseModel, Field, model_validator


class ExecutableNodeType(str, Enum):
    AGENT = "agent_node"
    TOOL = "tool_node"
    ROUTER = "router_node"
    WORKFLOW = "workflow_node"
    SYSTEM_REF = "system_ref_node"
    EVALUATOR = "evaluator_node"


class ConditionOperator(str, Enum):
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EXISTS = "exists"
    AND = "and"
    OR = "or"
    NOT = "not"


class ValueRef(BaseModel):
    """Typed operand for condition evaluation."""

    var: Optional[str] = None
    value: Any = None

    @model_validator(mode="after")
    def validate_exactly_one_source(self) -> "ValueRef":
        if self.var and self.value is not None:
            raise ValueError("ValueRef must define either 'var' or 'value', not both")
        if not self.var and self.value is None:
            raise ValueError("ValueRef must define either 'var' or 'value'")
        return self


class ConditionPredicate(BaseModel):
    """Declarative predicate AST for safe edge conditions."""

    op: ConditionOperator
    left: Optional[ValueRef] = None
    right: Optional[ValueRef] = None
    args: List["ConditionPredicate"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self) -> "ConditionPredicate":
        unary_ops = {ConditionOperator.NOT, ConditionOperator.EXISTS}
        binary_ops = {
            ConditionOperator.EQ,
            ConditionOperator.NEQ,
            ConditionOperator.IN,
            ConditionOperator.NOT_IN,
            ConditionOperator.GT,
            ConditionOperator.GTE,
            ConditionOperator.LT,
            ConditionOperator.LTE,
        }
        variadic_ops = {ConditionOperator.AND, ConditionOperator.OR}

        if self.op in unary_ops:
            if self.op == ConditionOperator.NOT:
                if len(self.args) != 1:
                    raise ValueError("Predicate op='not' requires exactly one nested predicate")
            else:
                if self.left is None:
                    raise ValueError("Predicate op='exists' requires 'left'")
        elif self.op in binary_ops:
            if self.left is None or self.right is None:
                raise ValueError(f"Predicate op='{self.op.value}' requires left and right operands")
        elif self.op in variadic_ops:
            if len(self.args) < 1:
                raise ValueError(f"Predicate op='{self.op.value}' requires nested predicates")
        return self


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_ms: int = Field(default=0, ge=0, le=60000)


class FailureMode(str, Enum):
    FAIL_RUN = "fail_run"
    CONTINUE_WITH_WARNING = "continue_with_warning"
    SKIP_NODE = "skip_node"
    FALLBACK_TO_NODE = "fallback_to_node"


class FailurePolicy(BaseModel):
    on_error: FailureMode = FailureMode.FAIL_RUN
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    fallback_node: Optional[str] = None


class AgentNodeDefinition(BaseModel):
    type: Literal["agent_node"] = "agent_node"
    agent_ref: str
    title: str = ""
    description: str = ""
    capabilities: List[str] = Field(default_factory=list)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)


class ToolNodeDefinition(BaseModel):
    type: Literal["tool_node"] = "tool_node"
    tool_ref: str
    title: str = ""
    description: str = ""
    capabilities: List[str] = Field(default_factory=list)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)


class SystemRefNodeDefinition(BaseModel):
    type: Literal["system_ref_node"] = "system_ref_node"
    target_system: str
    target_channel: Optional[str] = "stable"
    target_version: Optional[str] = None
    title: str = ""
    description: str = ""
    capabilities: List[str] = Field(default_factory=list)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)

    @model_validator(mode="after")
    def validate_target_selector(self) -> "SystemRefNodeDefinition":
        if self.target_channel and self.target_version:
            raise ValueError("SystemRefNodeDefinition cannot define both target_channel and target_version")
        return self


class EvaluatorNodeDefinition(BaseModel):
    type: Literal["evaluator_node"] = "evaluator_node"
    evaluator_ref: str
    title: str = ""
    description: str = ""
    capabilities: List[str] = Field(default_factory=list)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)


class WorkflowNodeDefinition(BaseModel):
    type: Literal["workflow_node"] = "workflow_node"
    workflow_ref: str
    title: str = ""
    description: str = ""
    capabilities: List[str] = Field(default_factory=list)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)


class RouterNodeDefinition(BaseModel):
    type: Literal["router_node"] = "router_node"
    router_ref: str
    title: str = ""
    description: str = ""
    capabilities: List[str] = Field(default_factory=list)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)


ExecutableNode = Annotated[
    Union[
        AgentNodeDefinition,
        ToolNodeDefinition,
        SystemRefNodeDefinition,
        EvaluatorNodeDefinition,
        WorkflowNodeDefinition,
        RouterNodeDefinition,
    ],
    Field(discriminator="type"),
]


class SystemEdge(BaseModel):
    from_node: str = Field(alias="from")
    to_node: str = Field(alias="to")
    when: Optional[ConditionPredicate] = None

    model_config = {"populate_by_name": True}


class SystemInterface(BaseModel):
    input_schema: str
    output_schema: str
    description: str = ""


class PermissionRolePolicy(BaseModel):
    allow: List[str] = Field(default_factory=list)
    deny: List[str] = Field(default_factory=list)


class PermissionPolicy(BaseModel):
    roles: Dict[str, PermissionRolePolicy] = Field(default_factory=dict)


class BudgetPolicy(BaseModel):
    max_new_systems_per_window: int = Field(default=10, ge=1)
    max_candidate_versions_per_system: int = Field(default=5, ge=1)
    max_active_canaries: int = Field(default=3, ge=1)
    max_promotions_per_window: int = Field(default=10, ge=1)
    reset_window: Literal["daily", "rolling_24h"] = "daily"
    hard_fail_on_exceed: bool = True


class SystemPolicy(BaseModel):
    execution_mode: Literal["proxy", "graph"] = "proxy"
    max_system_call_depth: int = Field(default=3, ge=1, le=20)
    sandbox_profile: str = "candidate_sandbox"
    permissions: PermissionPolicy = Field(default_factory=PermissionPolicy)
    budgets: BudgetPolicy = Field(default_factory=BudgetPolicy)


class SystemDefinition(BaseModel):
    system_id: str
    version: str
    entrypoint: str
    interface: SystemInterface
    graph: Dict[str, ExecutableNode] = Field(alias="nodes")
    edges: List[SystemEdge] = Field(default_factory=list)
    policy: SystemPolicy = Field(default_factory=SystemPolicy)
    default_agent: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    task_types: List[str] = Field(default_factory=list)
    capabilities: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_structure(self) -> "SystemDefinition":
        if self.entrypoint not in self.graph:
            raise ValueError(f"Entrypoint '{self.entrypoint}' is not present in the system graph")
        for edge in self.edges:
            if edge.from_node not in self.graph:
                raise ValueError(f"Edge source '{edge.from_node}' is not defined in graph")
            if edge.to_node not in self.graph:
                raise ValueError(f"Edge target '{edge.to_node}' is not defined in graph")
        return self


class SystemVersionStatus(str, Enum):
    DRAFT = "draft"
    CANDIDATE = "candidate"
    CANARY = "canary"
    STABLE = "stable"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    BLOCKED = "blocked"


class SystemVersionRecord(BaseModel):
    system_id: str
    version: str
    status: SystemVersionStatus = SystemVersionStatus.CANDIDATE
    definition: SystemDefinition
    parent_version: Optional[str] = None
    created_by: str = "system"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SystemReleaseState(BaseModel):
    system_id: str
    channels: Dict[str, str] = Field(default_factory=dict)
    revision: int = 0
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class SystemManifest(BaseModel):
    system_id: str
    title: str
    description: str = ""
    latest_stable: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    task_types: List[str] = Field(default_factory=list)
    input_schema: str = ""
    output_schema: str = ""
    discoverable: bool = True
    invokable: bool = True


class SideEffectRecord(BaseModel):
    name: str
    reversible: bool = False
    compensation_handler: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class NodeExecutionResult(BaseModel):
    node_id: str
    status: Literal["completed", "failed", "skipped"] = "completed"
    output: Any = None
    warnings: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    attempts: int = 1
    side_effects: List[SideEffectRecord] = Field(default_factory=list)


class SystemRunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class SystemRunResult(BaseModel):
    system_id: str
    resolved_version: str
    status: SystemRunStatus
    final_output: Any = None
    node_results: List[NodeExecutionResult] = Field(default_factory=list)
    failed_nodes: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    retry_trace: List[Dict[str, Any]] = Field(default_factory=list)
    side_effects: List[SideEffectRecord] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GovernanceDecision(BaseModel):
    decision_id: str
    subject: str
    decision: Literal["approve", "approve_with_constraints", "defer", "reject", "require_human_review"]
    rationale: List[str] = Field(default_factory=list)
    inputs: Dict[str, Any] = Field(default_factory=dict)
    next_action: List[str] = Field(default_factory=list)


class SystemRegistryState(BaseModel):
    version: int = 1
    manifests: Dict[str, SystemManifest] = Field(default_factory=dict)
    versions: Dict[str, Dict[str, SystemVersionRecord]] = Field(default_factory=dict)
    releases: Dict[str, SystemReleaseState] = Field(default_factory=dict)
