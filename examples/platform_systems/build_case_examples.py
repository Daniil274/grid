#!/usr/bin/env python3
"""Build and run two concrete platform case examples."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from core.system_mutation import MutationKind, MutationSet
from core.system_registry import SystemRegistry
from core.system_runtime import SystemRuntime
from core.system_workbench import SystemWorkbench
from examples.platform_systems.artifact_delivery.local_tools import LOCAL_TOOL_EXECUTORS as ARTIFACT_TOOLS
from examples.platform_systems.claude_tools_plus.local_tools import LOCAL_TOOL_EXECUTORS as CLAUDE_TOOLS_PLUS
from schemas.system_platform import (
    PermissionPolicy,
    PermissionRolePolicy,
    SystemDefinition,
)

BUNDLES_ROOT = PROJECT_ROOT / "examples" / "platform_systems"


def _load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def _permission_policy() -> PermissionPolicy:
    return PermissionPolicy(
        roles={
            "builder_agent": PermissionRolePolicy(allow=["create_candidate", "create_draft", "run_candidate"]),
            "runtime_agent": PermissionRolePolicy(allow=["invoke_system"]),
            "human_reviewer": PermissionRolePolicy(allow=["promote_candidate", "move_stable_channel", "invoke_system"]),
        }
    )


def _base_claude_tools_system() -> SystemDefinition:
    raw = _load_json(BUNDLES_ROOT / "claude_tools_plus" / "base_system.json")
    definition = SystemDefinition.model_validate(raw)
    definition.policy.permissions = _permission_policy()
    return definition


def _artifact_delivery_system() -> SystemDefinition:
    raw = _load_json(BUNDLES_ROOT / "artifact_delivery" / "system_definition.json")
    definition = SystemDefinition.model_validate(raw)
    definition.policy.permissions = _permission_policy()
    return definition


def _claude_tools_improvement_mutation_set() -> MutationSet:
    raw_mutations = _load_json(BUNDLES_ROOT / "claude_tools_plus" / "improvement_mutations.json")
    mutation_set = MutationSet()
    for mutation in raw_mutations:
        payload = dict(mutation)
        if payload["kind"] == MutationKind.SET_POLICY.value:
            policy = payload.get("policy", {})
            payload["policy"] = {
                **policy,
                "permissions": _permission_policy().model_dump(mode="json"),
            }
        kind = MutationKind(payload.pop("kind"))
        mutation_set.add(kind, **payload)
    return mutation_set


def _agent_executor(agent_ref: str, payload: dict) -> dict:
    if agent_ref == "agents.kimi_engineer":
        task = payload.get("task") or "unspecified"
        return {
            "agent_ref": agent_ref,
            "task": task,
            "subtasks": [
                f"inspect repo for {task}",
                f"implement change for {task}",
            ],
        }
    if agent_ref == "agents.coordinator":
        task = payload.get("task") or "unspecified"
        return {
            "agent_ref": agent_ref,
            "task": task,
            "subtasks": [
                f"plan {task}",
                f"execute {task}",
                f"test {task}",
            ],
        }
    if agent_ref == "agents.platform_planner":
        files = payload.get("files") or ["README.md", "report.md"]
        return {
            "agent_ref": agent_ref,
            "task": payload.get("task") or "artifact_delivery",
            "files": files,
        }
    return {"agent_ref": agent_ref, "input": payload}


def _tool_executor(tool_ref: str, payload: dict) -> dict:
    executors = {}
    executors.update(CLAUDE_TOOLS_PLUS)
    executors.update(ARTIFACT_TOOLS)

    if tool_ref not in executors:
        return {"tool_ref": tool_ref, "input": payload}

    if tool_ref == "claude_tools_plus.quality_gate":
        candidate = payload.get("orchestrator") or payload
        return executors[tool_ref]({"candidate": candidate})
    if tool_ref == "artifact_delivery.validate":
        candidate = payload.get("bundle") or payload
        return executors[tool_ref]({"candidate": candidate})
    return executors[tool_ref](payload)


def build_registry(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    registry_path = output_dir / "system_registry.json"
    if registry_path.exists():
        registry_path.unlink()
    results_path = output_dir / "results.json"
    if results_path.exists():
        results_path.unlink()
    registry = SystemRegistry(registry_path)
    workbench = SystemWorkbench(registry)

    try:
        workbench.create_version(_base_claude_tools_system(), actor_role="builder_agent")
    except ValueError:
        pass

    try:
        workbench.send_to_canary("claude_tools_system", "0.1.0", actor_role="human_reviewer")
    except Exception:
        pass
    try:
        workbench.promote_to_stable("claude_tools_system", "0.1.0", actor_role="human_reviewer")
    except Exception:
        pass

    mutation_set = _claude_tools_improvement_mutation_set()
    try:
        workbench.mutate_version(
            "claude_tools_system",
            mutation_set,
            new_version="0.2.0",
            source_version="0.1.0",
            actor_role="builder_agent",
        )
    except ValueError:
        pass

    try:
        workbench.send_to_canary("claude_tools_system", "0.2.0", actor_role="human_reviewer")
    except Exception:
        pass
    try:
        workbench.promote_to_stable(
            "claude_tools_system",
            "0.2.0",
            actor_role="human_reviewer",
            expected_current="0.1.0",
        )
    except Exception:
        pass

    try:
        workbench.create_version(_artifact_delivery_system(), actor_role="builder_agent")
    except ValueError:
        pass
    try:
        workbench.send_to_canary("artifact_delivery_system", "0.1.0", actor_role="human_reviewer")
    except Exception:
        pass
    try:
        workbench.promote_to_stable("artifact_delivery_system", "0.1.0", actor_role="human_reviewer")
    except Exception:
        pass

    return registry_path


def run_examples(registry_path: Path) -> dict:
    registry = SystemRegistry(registry_path)
    runtime = SystemRuntime(
        registry,
        agent_executor=_agent_executor,
        tool_executor=_tool_executor,
    )

    result_one = runtime.invoke(
        "claude_tools_system",
        channel="stable",
        input_payload={"task": "add orchestration and testing"},
        actor_role="runtime_agent",
    )
    result_two = runtime.invoke(
        "artifact_delivery_system",
        channel="stable",
        input_payload={"task": "prepare release bundle", "files": ["CHANGELOG.md", "dist/app.whl"]},
        actor_role="runtime_agent",
    )

    return {
        "systems": [manifest.model_dump(mode="json") for manifest in registry.list_systems()],
        "claude_tools_system": result_one.model_dump(mode="json"),
        "artifact_delivery_system": result_two.model_dump(mode="json"),
    }


def main() -> int:
    output_dir = PROJECT_ROOT / "workspace" / "platform_case_examples"
    registry_path = build_registry(output_dir)
    results = run_examples(registry_path)
    results_path = output_dir / "results.json"
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"registry_path": str(registry_path), "results_path": str(results_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
