import asyncio
import json
from pathlib import Path

from core.config import Config
from core.platform.builder import LiveSystemBuilder
from core.platform.registry import SystemRegistry
from core.platform.workbench import SystemWorkbench
from schemas import (
    AgentNodeDefinition,
    BuilderBundleSpec,
    GeneratedToolSpec,
    PermissionPolicy,
    PermissionRolePolicy,
    SystemDefinition,
    SystemEdge,
    SystemInterface,
    SystemPolicy,
    ToolNodeDefinition,
)


def _permission_policy():
    return PermissionPolicy(
        roles={
            "builder_agent": PermissionRolePolicy(allow=["create_candidate", "create_draft", "run_candidate"]),
            "runtime_agent": PermissionRolePolicy(allow=["invoke_system"]),
            "human_reviewer": PermissionRolePolicy(allow=["promote_candidate", "move_stable_channel", "invoke_system"]),
        }
    )


def _write_config(path: Path) -> None:
    path.write_text(
        """
settings:
  default_agent: chat_agent
  platform:
    enabled: true
    registry_path: data/system_registry.json
providers:
  test_provider:
    name: Test Provider
    base_url: http://localhost:1234/v1
    api_key: test
models:
  kimi-k2.5-opencode:
    name: kimi-k2.5
    provider: test_provider
agents:
  chat_agent:
    name: Chat Agent
    model: kimi-k2.5-opencode
    tools: []
tools: {}
prompt_templates: {}
""".strip(),
        encoding="utf-8",
    )


async def _fake_generator(**kwargs):
    mode = kwargs["mode"]
    requested_system_id = kwargs["requested_system_id"]
    target_version = kwargs["target_version"]
    if mode == "improve":
        definition = SystemDefinition(
            system_id=requested_system_id,
            version=target_version,
            entrypoint="main",
            interface=SystemInterface(input_schema="task_v1", output_schema="result_v1"),
            nodes={
                "main": AgentNodeDefinition(agent_ref="agents.kimi_engineer", capabilities=["implementation"]),
                "orchestrator": AgentNodeDefinition(agent_ref="agents.coordinator", capabilities=["orchestration"]),
                "tester": ToolNodeDefinition(tool_ref=f"{requested_system_id}.quality_gate", capabilities=["testing"]),
            },
            edges=[
                SystemEdge(**{"from": "main", "to": "orchestrator"}),
                SystemEdge(**{"from": "orchestrator", "to": "tester"}),
            ],
            policy=SystemPolicy(execution_mode="graph", permissions=_permission_policy()),
            task_types=["coding_task"],
            capabilities=["implementation", "orchestration", "testing"],
        )
        tools = [
            GeneratedToolSpec(
                tool_ref=f"{requested_system_id}.quality_gate",
                function_name="quality_gate",
                python_code="""
def quality_gate(payload: Dict[str, Any]) -> Dict[str, Any]:
    candidate = payload.get("previous") or payload.get("orchestrator") or {}
    subtasks = candidate.get("subtasks") or []
    return {"passed": any("test" in item.lower() for item in subtasks), "subtask_count": len(subtasks)}
""".strip(),
            )
        ]
        return BuilderBundleSpec(
            mode="improve",
            title="Improved Claude Tools",
            description="Adds orchestration and testing.",
            system_definition=definition,
            local_tools=tools,
            test_payload={"task": "improve build flow"},
            review_instructions="Check that the tester reports passed=true.",
            notes=["fake-improve"],
        )

    definition = SystemDefinition(
        system_id=requested_system_id,
        version=target_version,
        entrypoint="planner",
        interface=SystemInterface(input_schema="artifact_request_v1", output_schema="artifact_bundle_v1"),
        nodes={
            "planner": AgentNodeDefinition(agent_ref="agents.platform_planner", capabilities=["planning"]),
            "bundle": ToolNodeDefinition(tool_ref=f"{requested_system_id}.bundle", capabilities=["artifact_bundle"]),
            "validator": ToolNodeDefinition(tool_ref=f"{requested_system_id}.validate", capabilities=["artifact_validation"]),
        },
        edges=[
            SystemEdge(**{"from": "planner", "to": "bundle"}),
            SystemEdge(**{"from": "bundle", "to": "validator"}),
        ],
        policy=SystemPolicy(execution_mode="graph", permissions=_permission_policy()),
        task_types=["artifact_delivery"],
        capabilities=["planning", "artifact_bundle", "artifact_validation"],
    )
    tools = [
        GeneratedToolSpec(
            tool_ref=f"{requested_system_id}.bundle",
            function_name="bundle",
            python_code="""
def bundle(payload: Dict[str, Any]) -> Dict[str, Any]:
    files = payload.get("files") or payload.get("previous", {}).get("files") or []
    task = payload.get("task") or payload.get("previous", {}).get("task") or "artifact_delivery"
    return {"task": task, "files": files, "artifact_count": len(files)}
""".strip(),
        ),
        GeneratedToolSpec(
            tool_ref=f"{requested_system_id}.validate",
            function_name="validate_bundle",
            python_code="""
def validate_bundle(payload: Dict[str, Any]) -> Dict[str, Any]:
    candidate = payload.get("previous") or payload
    return {"passed": bool(candidate.get("files")), "artifact_count": len(candidate.get("files") or [])}
""".strip(),
        ),
    ]
    return BuilderBundleSpec(
        mode="create",
        title="Release Bundle System",
        description="Builds and validates artifact bundles.",
        system_definition=definition,
        local_tools=tools,
        test_payload={"task": "prepare bundle", "files": ["dist/app.whl", "CHANGELOG.md"]},
        review_instructions="Check that artifact_count is 2.",
        notes=["fake-create"],
    )


def test_live_system_builder_materializes_bundle_and_candidate(tmp_path):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    config = Config(str(config_path))
    output_root = tmp_path / "generated"
    registry_path = output_root / "system_registry.json"
    output_root.mkdir(parents=True, exist_ok=True)

    registry = SystemRegistry(registry_path)
    workbench = SystemWorkbench(registry)
    base = SystemDefinition(
        system_id="claude_tools_system",
        version="0.1.0",
        entrypoint="main",
        interface=SystemInterface(input_schema="task_v1", output_schema="result_v1"),
        nodes={"main": AgentNodeDefinition(agent_ref="agents.kimi_engineer", capabilities=["implementation"])},
        policy=SystemPolicy(execution_mode="proxy", permissions=_permission_policy()),
        task_types=["coding_task"],
        capabilities=["implementation"],
    )
    workbench.create_version(base, actor_role="builder_agent")
    workbench.send_to_canary("claude_tools_system", "0.1.0", actor_role="human_reviewer")
    workbench.promote_to_stable("claude_tools_system", "0.1.0", actor_role="human_reviewer")

    builder = LiveSystemBuilder(config, spec_generator=_fake_generator)
    improve_report = asyncio.run(
        builder.build_from_request(
            "Improve the claude tools system.",
            mode="improve",
            output_root=output_root,
            registry_path=registry_path,
            requested_system_id="claude_tools_system",
            base_system_id="claude_tools_system",
        )
    )
    create_report = asyncio.run(
        builder.build_from_request(
            "Create a release bundle system.",
            mode="create",
            output_root=output_root,
            registry_path=registry_path,
            requested_system_id="release_bundle_system",
        )
    )

    assert improve_report.candidate_ready is True
    assert improve_report.invocation_result is not None
    assert improve_report.invocation_result.final_output["passed"] is True
    assert Path(improve_report.bundle_dir, "system_definition.json").exists()
    assert Path(improve_report.bundle_dir, "local_tools.py").exists()

    assert create_report.candidate_ready is True
    assert create_report.invocation_result is not None
    assert create_report.invocation_result.final_output["passed"] is True
    assert Path(create_report.bundle_dir, "builder_report.json").exists()

    saved = json.loads(Path(create_report.bundle_dir, "candidate_run.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
