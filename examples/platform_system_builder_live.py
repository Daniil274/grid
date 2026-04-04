#!/usr/bin/env python3
"""Live prototype: builder model creates bundles, registers candidates, and tests them."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config
from core.system_builder import LiveSystemBuilder
from core.system_registry import SystemRegistry
from core.system_workbench import SystemWorkbench
from schemas.system_platform import PermissionPolicy, PermissionRolePolicy, SystemDefinition


def _permission_policy() -> PermissionPolicy:
    return PermissionPolicy(
        roles={
            "builder_agent": PermissionRolePolicy(allow=["create_candidate", "create_draft", "run_candidate"]),
            "runtime_agent": PermissionRolePolicy(allow=["invoke_system"]),
            "human_reviewer": PermissionRolePolicy(allow=["promote_candidate", "move_stable_channel", "invoke_system"]),
        }
    )


def _seed_base_system(registry_path: Path) -> None:
    registry = SystemRegistry(registry_path)
    workbench = SystemWorkbench(registry)
    base_path = PROJECT_ROOT / "examples" / "platform_systems" / "claude_tools_plus" / "base_system.json"
    definition = SystemDefinition.model_validate_json(base_path.read_text(encoding="utf-8"))
    definition.policy.permissions = _permission_policy()
    try:
        workbench.create_version(definition, actor_role="builder_agent")
        workbench.send_to_canary(definition.system_id, definition.version, actor_role="human_reviewer")
        workbench.promote_to_stable(definition.system_id, definition.version, actor_role="human_reviewer")
    except Exception:
        pass


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the live system-builder prototype with kimi-k2.5-opencode.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output-dir", default="workspace/generated_systems_live")
    parser.add_argument("--auto-promote", action="store_true")
    args = parser.parse_args()

    config = Config(args.config)
    output_root = (PROJECT_ROOT / args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    registry_path = output_root / "system_registry.json"
    if registry_path.exists():
        registry_path.unlink()

    _seed_base_system(registry_path)

    builder = LiveSystemBuilder(config, model_key="kimi-k2.5-opencode")
    reports = []
    failures = []

    try:
        reports.append(
            await builder.build_from_request(
                "Improve claude_tools_system. Add an orchestration step and a local testing tool that checks the plan includes a testing phase.",
                mode="improve",
                output_root=output_root,
                registry_path=registry_path,
                requested_system_id="claude_tools_system",
                base_system_id="claude_tools_system",
                auto_promote=args.auto_promote,
            )
        )
    except Exception as exc:
        failures.append({"request": "improve claude_tools_system", "error": str(exc)})

    try:
        reports.append(
            await builder.build_from_request(
                "Create a new system called release_bundle_system. It must use graph mode with exactly these stages: planner agent, bundle tool node, validate tool node, summarize node. The generated local tools must be used by the graph. The system accepts a task and a files list, validates the bundle, and returns a delivery summary.",
                mode="create",
                output_root=output_root,
                registry_path=registry_path,
                requested_system_id="release_bundle_system",
                auto_promote=args.auto_promote,
            )
        )
    except Exception as exc:
        failures.append({"request": "create release_bundle_system", "error": str(exc)})

    summary = {
        "registry_path": str(registry_path),
        "reports": [report.model_dump(mode="json") for report in reports],
        "failures": failures,
    }
    summary_path = output_root / "builder_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"registry_path": str(registry_path), "summary_path": str(summary_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
