#!/usr/bin/env python3
"""Live smoke runner for the self-organizing platform MVP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.agent_factory import AgentFactory
from core.config import Config
from core.system_registry import SystemRegistry
from core.system_runtime import SystemRuntime
from schemas.system_platform import (
    AgentNodeDefinition,
    PermissionPolicy,
    PermissionRolePolicy,
    SystemDefinition,
    SystemInterface,
    SystemPolicy,
)


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _build_demo_registry(registry_path: Path) -> None:
    permission_policy = PermissionPolicy(
        roles={
            "builder_agent": PermissionRolePolicy(allow=["create_candidate", "create_draft", "run_candidate"]),
            "runtime_agent": PermissionRolePolicy(allow=["invoke_system"]),
            "human_reviewer": PermissionRolePolicy(
                allow=["promote_candidate", "move_stable_channel", "invoke_system"]
            ),
        }
    )
    registry = SystemRegistry(registry_path)
    definition = SystemDefinition(
        system_id="demo_platform_system",
        version="0.1.0",
        entrypoint="main",
        interface=SystemInterface(input_schema="task_v1", output_schema="result_v1"),
        nodes={
            "main": AgentNodeDefinition(agent_ref="agents.chat_agent", capabilities=["analysis"]),
        },
        policy=SystemPolicy(execution_mode="proxy", permissions=permission_policy),
        capabilities=["analysis"],
        task_types=["demo_task"],
        metadata={
            "title": "Demo Platform System",
            "description": "Smoke test system for the platform MVP",
        },
    )
    try:
        registry.register_system(definition, actor_role="builder_agent")
    except ValueError:
        pass
    try:
        registry.promote_channel("demo_platform_system", "stable", "0.1.0", actor_role="human_reviewer")
    except Exception:
        pass


def _build_smoke_config(base_config_path: Path, output_dir: Path, registry_path: Path) -> Path:
    _ = base_config_path
    output_dir = output_dir.resolve()
    registry_path = registry_path.resolve()
    pattern_registry_path = (output_dir / "pattern_registry.json").resolve()

    config_text = f"""settings:
  default_agent: platform_reporter
  max_history: 20
  max_turns: 8
  agent_timeout: 180
  debug: false
  mcp_enabled: false
  working_directory: "{output_dir.as_posix()}"
  config_directory: "{output_dir.as_posix()}"
  allow_path_override: true
  project_tools:
    enabled: false
    tools_directory: "./tools"
    base_tools: []
  agent_logging:
    enabled: true
    level: full
    save_prompts: true
    save_conversations: true
    save_executions: true
  image_processing:
    enabled: false
  platform:
    enabled: true
    registry_path: "{registry_path.as_posix()}"
    pattern_registry_path: "{pattern_registry_path.as_posix()}"
    default_actor_role: builder_agent
isolation:
  enabled: false
  type: docker
  image: grid-agent:latest
providers:
  opencode:
    name: opencode
    base_url: https://opencode.ai/zen/go/v1
    api_key_env: OPENCODE_API_KEY
    timeout: 300
    max_retries: 3
    streaming_enabled: true
models:
  kimi-k2.5-opencode:
    name: kimi-k2.5
    provider: opencode
    temperature: 0.2
    max_tokens: 1200
    description: Live smoke reporter model
    streaming_enabled: true
    context_window: 22000
    preserve_reasoning_content: true
tools: {{}}
agents:
  platform_reporter:
    name: Platform Reporter
    model: kimi-k2.5-opencode
    tools: []
    description: Live reporter for platform smoke results
    mcp_enabled: false
    custom_prompt: |
      You are a concise technical reporter for the platform MVP smoke test.
      You will receive factual smoke-test results as JSON.
      Do not invent extra details.
      Summarize status, what passed, and which limitations remain.
      Keep the answer short.
prompt_templates: {{}}
"""
    output_path = output_dir / "config.platform_smoke.yaml"
    output_path.write_text(config_text, encoding="utf-8")
    return output_path


def _collect_smoke_data(registry_path: Path) -> dict:
    registry = SystemRegistry(registry_path)
    runtime = SystemRuntime(registry)

    systems = [manifest.model_dump(mode="json") for manifest in registry.list_systems()]
    info = registry.get_manifest("demo_platform_system").model_dump(mode="json")
    versions = [item.model_dump(mode="json") for item in registry.list_versions("demo_platform_system")]
    invocation = runtime.invoke(
        "demo_platform_system",
        version="0.1.0",
        input_payload={"task": "smoke"},
        actor_role="runtime_agent",
    ).model_dump(mode="json")

    return {
        "systems": systems,
        "system_info": info,
        "versions": versions,
        "invocation": invocation,
    }


async def _run_smoke(config_path: Path, smoke_data: dict) -> str:
    config = Config(str(config_path))
    factory = AgentFactory(config)
    return await factory.run_agent(
        "platform_reporter",
        "Summarize the platform smoke-test results:\n" + json.dumps(smoke_data, ensure_ascii=False, indent=2),
        user_id="platform_smoke_runner",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live platform smoke test with kimi-k2.5-opencode.")
    parser.add_argument("--config", default="config.yaml", help="Base Grid config path.")
    parser.add_argument(
        "--output-dir",
        default="workspace/platform_smoke_runner",
        help="Directory for temporary smoke artifacts.",
    )
    parser.add_argument("--env-file", default=".env", help="Optional env file to preload.")
    args = parser.parse_args()

    _load_env_file((PROJECT_ROOT / args.env_file).resolve())

    if not os.getenv("OPENCODE_API_KEY"):
        print("OPENCODE_API_KEY is not set. Live smoke run cannot start.", file=sys.stderr)
        return 2

    output_dir = (PROJECT_ROOT / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    registry_path = output_dir / "system_registry.json"

    _build_demo_registry(registry_path)
    smoke_config_path = _build_smoke_config((PROJECT_ROOT / args.config).resolve(), output_dir, registry_path)
    smoke_data = _collect_smoke_data(registry_path)

    result = asyncio.run(_run_smoke(smoke_config_path, smoke_data))
    print("=== PLATFORM SMOKE RESULT START ===")
    print(result)
    print("=== PLATFORM SMOKE RESULT END ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
