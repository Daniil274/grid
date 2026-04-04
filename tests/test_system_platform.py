import pytest

from core.condition_evaluator import ConditionEvaluator, ConditionEvaluationError
from core.event_bus import DomainEventBus
from core.meta_cognitive import (
    CapabilityRegistry,
    CoverageIndex,
    LifecycleHealthAnalyzer,
    PatternRegistry,
    ReflectionStore,
    SemanticDriftMonitor,
    TaskOntology,
    TemplateInstantiator,
)
from core.system_compiler import SystemCompileError
from core.system_registry import ChannelConflictError, SystemRegistry
from core.system_release_manager import CandidatePromotionFlow
from core.system_mutation import DraftSystemBuilder, MutationKind, MutationSet, SystemMutator
from core.system_workbench import SystemWorkbench
from core.system_runtime import SystemRuntime
from core.config import Config
from examples.platform_systems.build_case_examples import build_registry, run_examples
from schemas import (
    AgentNodeDefinition,
    Capability,
    ConditionPredicate,
    DecisionTrace,
    DesignTemplate,
    DesignTemplateEdge,
    DesignTemplateSlot,
    PermissionPolicy,
    PermissionRolePolicy,
    ReflectionRecord,
    SemanticContract,
    SystemDefinition,
    SystemEdge,
    SystemInterface,
    SystemPolicy,
    SystemRefNodeDefinition,
    TaskType,
    ValueRef,
)
from tools.function_tools import AVAILABLE_TOOLS, TOOL_ALIASES


def _permission_policy():
    return PermissionPolicy(
        roles={
            "builder_agent": PermissionRolePolicy(allow=["create_candidate", "run_candidate"]),
            "runtime_agent": PermissionRolePolicy(allow=["invoke_system"]),
            "human_reviewer": PermissionRolePolicy(
                allow=["promote_candidate", "move_stable_channel", "invoke_system"]
            ),
        }
    )


def _simple_system(system_id: str, version: str = "0.1.0", *, execution_mode: str = "proxy") -> SystemDefinition:
    return SystemDefinition(
        system_id=system_id,
        version=version,
        entrypoint="main",
        interface=SystemInterface(input_schema="task_v1", output_schema="result_v1"),
        nodes={
            "main": AgentNodeDefinition(agent_ref="agents.chat_agent", capabilities=["analysis"]),
        },
        policy=SystemPolicy(execution_mode=execution_mode, permissions=_permission_policy()),
        capabilities=["analysis"],
        task_types=["coding_task"],
        metadata={"title": system_id, "description": "base system"},
    )


def test_condition_evaluator_supports_safe_predicates():
    evaluator = ConditionEvaluator()
    predicate = ConditionPredicate(
        op="and",
        args=[
            ConditionPredicate(op="eq", left=ValueRef(var="input.kind"), right=ValueRef(value="coding")),
            ConditionPredicate(op="gte", left=ValueRef(var="state.priority"), right=ValueRef(value=2)),
        ],
    )

    result = evaluator.evaluate(predicate, {"input": {"kind": "coding"}, "state": {"priority": 3}, "node_output": {}, "context": {"flags": {}}})
    assert result is True

    with pytest.raises(ConditionEvaluationError):
        evaluator.evaluate(
            ConditionPredicate(op="eq", left=ValueRef(var="os.environ"), right=ValueRef(value="x")),
            {"input": {}, "state": {}, "node_output": {}, "context": {"flags": {}}},
        )


def test_system_registry_registers_versions_and_promotes_with_cas(temp_dir):
    registry = SystemRegistry(temp_dir / "system_registry.json")
    system = _simple_system("coding_assistant")

    record = registry.register_system(system, actor_role="builder_agent")
    assert record.system_id == "coding_assistant"
    assert registry.list_systems()[0].system_id == "coding_assistant"

    flow = CandidatePromotionFlow(registry)
    flow.send_to_canary("coding_assistant", "0.1.0", actor_role="human_reviewer")
    release = flow.promote_to_stable("coding_assistant", "0.1.0", actor_role="human_reviewer", expected_current=None)
    assert release.channels["stable"] == "0.1.0"

    system_v2 = _simple_system("coding_assistant", version="0.2.0")
    registry.register_system(system_v2, actor_role="builder_agent")
    flow.send_to_canary("coding_assistant", "0.2.0", actor_role="human_reviewer")

    with pytest.raises(ChannelConflictError):
        flow.promote_to_stable(
            "coding_assistant",
            "0.2.0",
            actor_role="human_reviewer",
            expected_current="non-existent",
        )


def test_system_compiler_detects_system_ref_cycles(temp_dir):
    registry = SystemRegistry(temp_dir / "system_registry.json")
    system_a = SystemDefinition(
        system_id="system_a",
        version="0.1.0",
        entrypoint="main",
        interface=SystemInterface(input_schema="task_v1", output_schema="result_v1"),
        nodes={"main": SystemRefNodeDefinition(target_system="system_b")},
        policy=SystemPolicy(permissions=_permission_policy()),
    )
    system_b = SystemDefinition(
        system_id="system_b",
        version="0.1.0",
        entrypoint="main",
        interface=SystemInterface(input_schema="task_v1", output_schema="result_v1"),
        nodes={"main": SystemRefNodeDefinition(target_system="system_a")},
        policy=SystemPolicy(permissions=_permission_policy()),
    )
    registry.register_system(system_a, actor_role="builder_agent")

    with pytest.raises(SystemCompileError):
        registry.register_system(system_b, actor_role="builder_agent")


def test_system_runtime_supports_proxy_and_graph_modes(temp_dir):
    registry = SystemRegistry(temp_dir / "system_registry.json")
    proxy_system = _simple_system("proxy_system", execution_mode="proxy")
    graph_system = SystemDefinition(
        system_id="graph_system",
        version="0.1.0",
        entrypoint="analyze",
        interface=SystemInterface(input_schema="task_v1", output_schema="result_v1"),
        nodes={
            "analyze": AgentNodeDefinition(agent_ref="agents.analyzer", capabilities=["analysis"]),
            "execute": AgentNodeDefinition(agent_ref="agents.executor", capabilities=["execution"]),
        },
        edges=[
            SystemEdge(
                **{
                    "from": "analyze",
                    "to": "execute",
                    "when": ConditionPredicate(op="exists", left=ValueRef(var="input.task")),
                }
            )
        ],
        policy=SystemPolicy(execution_mode="graph", permissions=_permission_policy()),
        capabilities=["analysis", "execution"],
        task_types=["coding_task"],
        metadata={"title": "graph_system"},
    )
    registry.register_system(proxy_system, actor_role="builder_agent")
    registry.register_system(graph_system, actor_role="builder_agent")

    runtime = SystemRuntime(
        registry,
        agent_executor=lambda ref, payload: {"ref": ref, "task": payload.get("task")},
    )

    proxy_result = runtime.invoke("proxy_system", version="0.1.0", input_payload={"task": "x"})
    assert proxy_result.status.value == "completed"
    assert proxy_result.final_output["ref"] == "agents.chat_agent"

    graph_result = runtime.invoke("graph_system", version="0.1.0", input_payload={"task": "x"})
    assert graph_result.status.value == "completed"
    assert len(graph_result.node_results) == 2
    assert graph_result.final_output["ref"] == "agents.executor"


def test_template_instantiation_and_reflection_flow(temp_dir):
    template = DesignTemplate(
        template_id="analyzer_executor_validator",
        title="Analyzer Executor Validator",
        applies_to_task_types=["coding_task"],
        required_capabilities=["analysis", "execution", "validation"],
        slots={
            "analyzer": DesignTemplateSlot(
                slot_id="analyzer",
                allowed_node_types=["agent_node", "system_ref_node"],
                required_capabilities=["analysis"],
            ),
            "executor": DesignTemplateSlot(
                slot_id="executor",
                allowed_node_types=["agent_node"],
                required_capabilities=["execution"],
            ),
            "validator": DesignTemplateSlot(
                slot_id="validator",
                allowed_node_types=["system_ref_node", "agent_node"],
                required_capabilities=["validation"],
            ),
        },
        edges=[
            DesignTemplateEdge(from_slot="analyzer", to_slot="executor"),
            DesignTemplateEdge(from_slot="executor", to_slot="validator"),
        ],
    )
    instantiator = TemplateInstantiator()
    definition, unresolved = instantiator.instantiate(
        template=template,
        bindings={
            "analyzer": {"node_type": "agent_node", "ref": "agents.analyzer", "capabilities": ["analysis"]},
            "executor": {"node_type": "agent_node", "ref": "agents.executor", "capabilities": ["execution"]},
            "validator": {"node_type": "system_ref_node", "ref": "validator_system", "capabilities": ["validation"]},
        },
        task_type="coding_task",
        system_id="generated_system",
        version="0.1.0",
        input_schema="task_v1",
        output_schema="result_v1",
    )
    assert unresolved == []
    assert definition.entrypoint == "analyzer"
    assert "validator" in definition.graph

    pattern_registry = PatternRegistry(temp_dir / "pattern_registry.json", event_bus=DomainEventBus())
    reflection_store = ReflectionStore(pattern_registry)
    reflection_store.record(
        ReflectionRecord(
            reflection_id="refl-1",
            task_type="coding_task",
            attempted_pattern="analyzer_executor_validator",
            system_used="generated_system@0.1.0",
            lessons=["validator improved confidence"],
        ),
        DecisionTrace(
            trace_id="trace-1",
            task_type="coding_task",
            steps=["classify_task", "choose_template", "bind_slots"],
            outcome_link={"reflection_id": "refl-1"},
        ),
    )
    assert pattern_registry._state.reflections["refl-1"].task_type == "coding_task"
    assert pattern_registry._state.decision_traces["trace-1"].steps[0] == "classify_task"


def test_knowledge_layer_supports_coverage_and_semantic_drift():
    ontology = TaskOntology()
    capability_registry = CapabilityRegistry()
    coverage_index = CoverageIndex()
    drift_monitor = SemanticDriftMonitor()
    health_analyzer = LifecycleHealthAnalyzer()

    ontology.register(
        TaskType(
            task_type_id="flaky_test_repair",
            title="Flaky Test Repair",
            related_capabilities=["failure_isolation", "test_fixing"],
        )
    )
    capability_registry.register_capability(Capability(capability_id="failure_isolation", title="Failure Isolation"))
    capability_registry.register_capability(Capability(capability_id="test_fixing", title="Test Fixing"))
    capability_registry.set_semantic_contract(
        SemanticContract(
            system_id="test_repair_system",
            primary_task_types=["flaky_test_repair"],
            primary_capabilities=["failure_isolation", "test_fixing"],
            forbidden_drift=["general_web_research_system"],
        )
    )

    definition = _simple_system("test_repair_system", version="0.1.0")
    definition.task_types = ["flaky_test_repair"]
    definition.capabilities = ["failure_isolation", "test_fixing"]
    records = coverage_index.build(
        task_types=ontology.list(),
        systems=[definition],
        capabilities=capability_registry.list_capabilities(),
    )
    assert records[0].coverage_score == 1.0

    drifted = definition.model_copy(deep=True)
    drifted.task_types = ["web_research"]
    issues = drift_monitor.check(drifted, capability_registry.get_contract("test_repair_system"))
    assert "primary_task_types_drift" in issues

    health = health_analyzer.analyze(
        system_id="test_repair_system",
        dependency_count=0,
        usage_frequency="low",
        duplication_score=0.85,
        semantic_overlap_with=["legacy_test_fixer"],
    )
    assert health.recommended_action == "consolidate"


def test_domain_mutation_layer_updates_draft_system():
    builder = DraftSystemBuilder()
    definition = builder.create(
        system_id="draft_system",
        version="0.1.0",
        entrypoint="main",
        interface=SystemInterface(input_schema="task_v1", output_schema="result_v1"),
        nodes={"main": AgentNodeDefinition(agent_ref="agents.chat_agent", capabilities=["analysis"])},
        policy=SystemPolicy(permissions=_permission_policy()),
    )
    mutator = SystemMutator()
    mutation_set = (
        MutationSet()
        .add(
            MutationKind.ADD_NODE,
            node_id="validator",
            node=SystemRefNodeDefinition(target_system="validator_system", capabilities=["validation"]),
        )
        .add(MutationKind.CONNECT_NODES, from_node="main", to_node="validator")
        .add(MutationKind.SET_ENTRYPOINT, entrypoint="main")
        .add(MutationKind.ADD_DEPENDENCY, dependency="validator_system")
    )
    updated = mutator.apply(definition, mutation_set)
    assert "validator" in updated.graph
    assert updated.dependencies == ["validator_system"]
    assert len(updated.edges) == 1


def test_platform_tools_are_registered_in_global_tool_registry():
    assert "system_list_systems" in AVAILABLE_TOOLS
    assert "system_get_system_info" in AVAILABLE_TOOLS
    assert "system_get_system_versions" in AVAILABLE_TOOLS
    assert "system_invoke_system" in AVAILABLE_TOOLS
    assert "system_create_version" in AVAILABLE_TOOLS
    assert "system_clone_version" in AVAILABLE_TOOLS
    assert "system_apply_mutations" in AVAILABLE_TOOLS
    assert "system_promote_version" in AVAILABLE_TOOLS
    assert "system_reject_version" in AVAILABLE_TOOLS
    assert "system_rollback_stable" in AVAILABLE_TOOLS
    assert "system_build_bundle" in AVAILABLE_TOOLS

    assert TOOL_ALIASES["list_systems"] == "system_list_systems"
    assert TOOL_ALIASES["invoke_system"] == "system_invoke_system"
    assert TOOL_ALIASES["clone_system_version"] == "system_clone_version"
    assert TOOL_ALIASES["build_system_bundle"] == "system_build_bundle"


def test_system_workbench_supports_clone_mutate_and_promotion(temp_dir):
    registry = SystemRegistry(temp_dir / "system_registry.json")
    base = _simple_system("builder_system")
    workbench = SystemWorkbench(registry)

    workbench.create_version(base, actor_role="builder_agent")
    workbench.send_to_canary("builder_system", "0.1.0", actor_role="human_reviewer")
    workbench.promote_to_stable("builder_system", "0.1.0", actor_role="human_reviewer")

    cloned = workbench.clone_version(
        "builder_system",
        new_version="0.2.0",
        source_channel="stable",
        actor_role="builder_agent",
    )
    assert cloned.parent_version == "0.1.0"
    assert cloned.status.value == "candidate"

    mutation_set = (
        MutationSet()
        .add(
            MutationKind.ADD_NODE,
            node_id="validator",
            node=SystemRefNodeDefinition(target_system="validator_system", capabilities=["validation"]),
        )
        .add(MutationKind.CONNECT_NODES, from_node="main", to_node="validator")
        .add(MutationKind.ADD_DEPENDENCY, dependency="validator_system")
    )
    mutated = workbench.mutate_version(
        "builder_system",
        mutation_set,
        new_version="0.3.0",
        source_version="0.2.0",
        actor_role="builder_agent",
    )
    assert "validator" in mutated.definition.graph
    assert mutated.parent_version == "0.2.0"

    rejected = workbench.reject_version("builder_system", "0.3.0")
    assert rejected.status.value == "rejected"

    workbench.send_to_canary("builder_system", "0.2.0", actor_role="human_reviewer")
    workbench.promote_to_stable(
        "builder_system",
        "0.2.0",
        actor_role="human_reviewer",
        expected_current="0.1.0",
    )
    assert registry.get_release_state("builder_system").channels["stable"] == "0.2.0"

    workbench.rollback_stable("builder_system", "0.1.0", actor_role="human_reviewer")
    assert registry.get_release_state("builder_system").channels["stable"] == "0.1.0"


def test_config_exposes_platform_registry_path(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
settings:
  default_agent: chat_agent
  platform:
    enabled: true
    registry_path: data/custom_system_registry.json
providers:
  test_provider:
    name: Test Provider
    base_url: http://localhost:1234/v1
    api_key: test
models:
  test_model:
    name: Test Model
    provider: test_provider
agents:
  chat_agent:
    name: Chat Agent
    model: test_model
    tools: []
tools: {}
prompt_templates: {}
""".strip(),
        encoding="utf-8",
    )
    config = Config(str(config_path))
    assert config.get_system_registry_path().endswith("data/custom_system_registry.json")


def test_case_example_bundles_build_and_run(tmp_path):
    registry_path = build_registry(tmp_path / "platform_case_examples")
    results = run_examples(registry_path)

    claude_result = results["claude_tools_system"]
    artifact_result = results["artifact_delivery_system"]

    assert claude_result["resolved_version"] == "0.2.0"
    assert claude_result["status"] == "completed"
    assert [node["node_id"] for node in claude_result["node_results"]] == ["main", "orchestrator", "tester"]
    assert claude_result["final_output"]["passed"] is True

    assert artifact_result["resolved_version"] == "0.1.0"
    assert artifact_result["status"] == "completed"
    assert [node["node_id"] for node in artifact_result["node_results"]] == ["planner", "bundle", "validator"]
    assert artifact_result["final_output"]["passed"] is True
