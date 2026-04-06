"""Prototype system-builder flow: request -> bundle -> candidate -> test."""

from __future__ import annotations

import importlib.util
import json
import re
import yaml
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from core.config import Config
from core.managers.model_manager import ModelManager
from core.system_compiler import SystemCompileError, SystemCompiler
from core.system_registry import SystemRegistry
from core.system_runtime import SystemRuntime
from core.system_workbench import SystemWorkbench
from schemas import AgentNodeDefinition, SystemEdge, SystemInterface, SystemPolicy, SystemRefNodeDefinition, ToolNodeDefinition
from schemas.system_builder import BuilderBundleSpec, BuilderRunReport, GeneratedToolSpec
from schemas.system_platform import PermissionPolicy, PermissionRolePolicy, SystemDefinition


class SystemBuilderError(RuntimeError):
    """Raised when the builder cannot generate or materialize a bundle."""


SpecGenerator = Callable[..., Awaitable[BuilderBundleSpec]]


class LiveSystemBuilder:
    """Generate, materialize, register, and validate a system bundle."""

    def __init__(
        self,
        config: Config,
        *,
        model_key: str = "kimi-k2.5-opencode",
        spec_generator: Optional[SpecGenerator] = None,
    ) -> None:
        self.config = config
        self.model_key = model_key
        self._spec_generator = spec_generator or self._generate_bundle_spec
        self._compiler = SystemCompiler()

    async def build_from_request(
        self,
        request_text: str,
        *,
        mode: str,
        output_root: Path,
        registry_path: Path,
        requested_system_id: str,
        auto_promote: bool = False,
        base_system_id: Optional[str] = None,
        source_channel: str = "stable",
    ) -> BuilderRunReport:
        registry = SystemRegistry(registry_path)
        workbench = SystemWorkbench(registry)
        base_definition: Optional[SystemDefinition] = None

        if mode == "improve":
            target_id = base_system_id or requested_system_id
            base_definition = registry.get_definition(target_id, channel=source_channel)
            requested_system_id = target_id

        target_version = self._next_version(registry, requested_system_id, mode=mode, base_definition=base_definition)
        spec = await self._spec_generator(
            request_text=request_text,
            mode=mode,
            requested_system_id=requested_system_id,
            target_version=target_version,
            base_definition=base_definition,
        )

        definition = spec.system_definition.model_copy(deep=True)
        definition.system_id = requested_system_id
        definition.version = target_version
        definition.metadata = {
            **definition.metadata,
            "title": spec.title,
            "description": spec.description,
            "generated_by": "live_system_builder",
        }
        definition.policy.permissions = self._permission_policy()

        bundle_dir = output_root / definition.system_id / definition.version
        bundle_dir.mkdir(parents=True, exist_ok=True)

        created_files, bundle_artifacts = self._write_bundle_files(bundle_dir, request_text, spec, definition)
        local_tools = self._load_local_tool_executors(bundle_dir)

        record = workbench.create_version(
            definition,
            actor_role="builder_agent",
            parent_version=base_definition.version if base_definition else None,
        )

        runtime = SystemRuntime(
            registry,
            agent_executor=self._agent_executor,
            tool_executor=self._build_tool_executor(local_tools),
        )
        invocation_result = runtime.invoke(
            definition.system_id,
            version=definition.version,
            input_payload=spec.test_payload,
            actor_role="runtime_agent",
        )
        (bundle_dir / "candidate_run.json").write_text(
            json.dumps(invocation_result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        created_files.append(str((bundle_dir / "candidate_run.json").resolve()))

        candidate_ready = self._is_candidate_ready(invocation_result)
        release_state = None
        promoted_to_stable = False
        if candidate_ready:
            release_state = workbench.send_to_canary(definition.system_id, definition.version, actor_role="human_reviewer")
            if auto_promote:
                current_stable = None
                try:
                    current_stable = registry.get_release_state(definition.system_id).channels.get("stable")
                except Exception:
                    current_stable = None
                release_state = workbench.promote_to_stable(
                    definition.system_id,
                    definition.version,
                    actor_role="human_reviewer",
                    expected_current=current_stable,
                )
                promoted_to_stable = True

        report = BuilderRunReport(
            request_text=request_text,
            mode=mode,
            system_id=definition.system_id,
            version=definition.version,
            bundle_dir=str(bundle_dir.resolve()),
            registry_path=str(registry_path.resolve()),
            candidate_ready=candidate_ready,
            promoted_to_stable=promoted_to_stable,
            invocation_result=invocation_result,
            release_state=release_state,
            created_files=created_files,
            review_instructions=spec.review_instructions,
            notes=[*spec.notes, f"registered_status={record.status.value}"],
        )
        (bundle_dir / "builder_report.json").write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return report

    async def _generate_bundle_spec(
        self,
        *,
        request_text: str,
        mode: str,
        requested_system_id: str,
        target_version: str,
        base_definition: Optional[SystemDefinition],
    ) -> BuilderBundleSpec:
        model_manager = ModelManager(self.config)
        client, model_name = model_manager.get_openai_client_for_model(self.model_key)

        system_context = (
            json.dumps(base_definition.model_dump(mode="json"), ensure_ascii=False, indent=2)
            if base_definition
            else "null"
        )
        create_constraints = ""
        if mode == "create":
            create_constraints = """
- create mode must include at least two tool_node nodes that reference generated local_tools
- do not solve create mode with agent-only graphs
- prefer graph shape: planner agent -> bundle tool -> validate tool -> summarize agent/tool
""".strip()
        prompt = f"""
Return strict JSON only.

You are a system builder agent. Produce a runnable system bundle spec.

Constraints:
- mode: {mode}
- requested_system_id: {requested_system_id}
- requested_version: {target_version}
- allowed agent refs: agents.kimi_engineer, agents.coordinator, agents.platform_planner, agents.validator
- use local tool refs starting with "{requested_system_id}."
- output must be valid JSON, no markdown
- system_definition must be complete and runnable
- use execution_mode "graph" unless the system is trivially single-node
- local tool code must be pure Python with stdlib only
- every tool spec must contain a full top-level function definition
- test_payload must be small and runnable
{create_constraints}

Base definition:
{system_context}

User request:
{request_text}

Required JSON shape:
{{
  "mode": "{mode}",
  "title": "...",
  "description": "...",
  "system_definition": {{
    "system_id": "{requested_system_id}",
    "version": "{target_version}",
    "entrypoint": "...",
    "interface": {{"input_schema": "...", "output_schema": "..."}},
    "nodes": {{}},
    "edges": [],
    "policy": {{"execution_mode": "graph"}},
    "task_types": [],
    "capabilities": [],
    "metadata": {{}}
  }},
  "local_tools": [
    {{
      "tool_ref": "{requested_system_id}.example_tool",
      "function_name": "example_tool",
      "description": "...",
      "python_code": "def example_tool(payload):\\n    return {{...}}"
    }}
  ],
  "test_payload": {{}},
  "review_instructions": "...",
  "notes": ["..."]
}}
""".strip()
        messages = [
            {"role": "system", "content": "You generate strict JSON for runnable system bundles."},
            {"role": "user", "content": prompt},
        ]

        for _ in range(3):
            response = await client.chat.completions.create(
                model=model_name,
                temperature=0.2,
                messages=messages,
            )
            text = response.choices[0].message.content or ""
            parsed = self._extract_json_object(text)
            parsed = self._normalize_builder_payload(
                parsed,
                mode=mode,
                requested_system_id=requested_system_id,
                target_version=target_version,
            )
            spec = BuilderBundleSpec.model_validate(parsed)
            contract_errors = self._builder_contract_errors(spec, mode=mode)
            try:
                self._compiler.compile(spec.system_definition)
            except SystemCompileError as exc:
                contract_errors.append(str(exc))
            if not contract_errors:
                return spec
            messages.append({"role": "assistant", "content": text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Repair the previous JSON. It violated these constraints:\n- "
                        + "\n- ".join(contract_errors)
                        + "\nReturn strict JSON only."
                    ),
                }
            )

        raise SystemBuilderError("Builder model could not satisfy the bundle contract after repair attempts")

    @staticmethod
    def _extract_json_object(text: str) -> Dict[str, Any]:
        if not text:
            raise SystemBuilderError("Builder model returned empty content")
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise SystemBuilderError("Builder model did not return a JSON object")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise SystemBuilderError(f"Builder JSON parse failed: {exc}") from exc

    @classmethod
    def _normalize_builder_payload(
        cls,
        payload: Dict[str, Any],
        *,
        mode: str,
        requested_system_id: str,
        target_version: str,
    ) -> Dict[str, Any]:
        normalized = dict(payload)
        normalized["mode"] = mode
        normalized.setdefault("title", requested_system_id.replace("_", " ").title())
        normalized.setdefault("description", f"Generated bundle for {requested_system_id}")
        normalized["system_definition"] = cls._normalize_system_definition(
            normalized.get("system_definition") or {},
            requested_system_id=requested_system_id,
            target_version=target_version,
        )

        local_tools = []
        for tool in normalized.get("local_tools") or []:
            raw = dict(tool)
            tool_ref = raw.get("tool_ref") or raw.get("name") or raw.get("id")
            if not tool_ref:
                continue
            function_name = raw.get("function_name") or cls._tool_ref_to_function_name(tool_ref)
            python_code = raw.get("python_code") or raw.get("code") or ""
            if not python_code:
                continue
            local_tools.append(
                {
                    "tool_ref": tool_ref,
                    "function_name": function_name,
                    "description": raw.get("description") or "",
                    "python_code": python_code,
                }
            )
        normalized["local_tools"] = local_tools
        normalized.setdefault("test_payload", {})
        normalized.setdefault("review_instructions", "")
        normalized.setdefault("notes", [])
        return normalized

    @classmethod
    def _normalize_system_definition(
        cls,
        raw_definition: Dict[str, Any],
        *,
        requested_system_id: str,
        target_version: str,
    ) -> Dict[str, Any]:
        definition = dict(raw_definition)
        metadata = dict(definition.get("metadata") or {})

        interface = dict(definition.get("interface") or {})
        input_schema = interface.get("input_schema", "generated_input_v1")
        output_schema = interface.get("output_schema", "generated_output_v1")
        if isinstance(input_schema, dict):
            metadata["raw_input_schema"] = input_schema
            input_schema = input_schema.get("title") or "generated_input_v1"
        if isinstance(output_schema, dict):
            metadata["raw_output_schema"] = output_schema
            output_schema = output_schema.get("title") or "generated_output_v1"
        interface = SystemInterface(input_schema=str(input_schema), output_schema=str(output_schema)).model_dump(mode="json")

        raw_nodes = definition.get("nodes") or definition.get("graph") or {}
        normalized_nodes: Dict[str, Any] = {}
        if isinstance(raw_nodes, list):
            node_iterable = [(node.get("id") or node.get("node_id") or node.get("name"), node) for node in raw_nodes]
        else:
            node_iterable = list(raw_nodes.items())

        derived_entrypoint = definition.get("entrypoint")
        for node_id, node in node_iterable:
            if not node_id:
                continue
            if cls._is_pseudo_node(node):
                if not derived_entrypoint:
                    transitions = node.get("transitions") or node.get("next") or []
                    if isinstance(transitions, list) and transitions:
                        derived_entrypoint = transitions[0]
                continue
            normalized_node = cls._normalize_node(node)
            if normalized_node is None:
                continue
            normalized_nodes[node_id] = normalized_node

        raw_edges = definition.get("edges") or []
        normalized_edges = []
        for edge in raw_edges:
            normalized_edge = cls._normalize_edge(edge)
            if normalized_edge is None:
                continue
            if normalized_edge["from_node"] not in normalized_nodes or normalized_edge["to_node"] not in normalized_nodes:
                continue
            normalized_edges.append(normalized_edge)

        if not derived_entrypoint and normalized_edges:
            derived_entrypoint = normalized_edges[0]["from_node"]

        policy = dict(definition.get("policy") or {})
        policy.setdefault("execution_mode", "graph" if normalized_edges else "proxy")
        policy = SystemPolicy(**policy).model_dump(mode="json")

        normalized = {
            "system_id": requested_system_id,
            "version": target_version,
            "entrypoint": derived_entrypoint or next(iter(normalized_nodes.keys()), "main"),
            "interface": interface,
            "nodes": normalized_nodes,
            "edges": normalized_edges,
            "policy": policy,
            "dependencies": definition.get("dependencies") or [],
            "task_types": definition.get("task_types") or [],
            "capabilities": definition.get("capabilities") or [],
            "metadata": metadata,
        }
        return normalized

    @staticmethod
    def _normalize_node(raw_node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        node = dict(raw_node or {})
        node_type = node.get("type")
        if LiveSystemBuilder._is_pseudo_node(node):
            return None
        if node_type == "agent_node":
            return AgentNodeDefinition(
                agent_ref=node.get("agent_ref") or node.get("agent"),
                title=node.get("title") or "",
                description=node.get("description") or "",
                capabilities=node.get("capabilities") or [],
            ).model_dump(mode="json")
        if node_type == "tool_node":
            return ToolNodeDefinition(
                tool_ref=node.get("tool_ref") or node.get("tool"),
                title=node.get("title") or "",
                description=node.get("description") or "",
                capabilities=node.get("capabilities") or [],
            ).model_dump(mode="json")
        if node_type == "system_ref_node":
            return SystemRefNodeDefinition(
                target_system=node.get("target_system") or node.get("system"),
                target_channel=node.get("target_channel", "stable"),
                target_version=node.get("target_version"),
                title=node.get("title") or "",
                description=node.get("description") or "",
                capabilities=node.get("capabilities") or [],
            ).model_dump(mode="json")
        if node.get("agent_ref") or node.get("agent"):
            return AgentNodeDefinition(
                agent_ref=node.get("agent_ref") or node.get("agent"),
                title=node.get("title") or "",
                description=node.get("description") or "",
                capabilities=node.get("capabilities") or [],
            ).model_dump(mode="json")
        if node.get("tool_ref") or node.get("tool"):
            return ToolNodeDefinition(
                tool_ref=node.get("tool_ref") or node.get("tool"),
                title=node.get("title") or "",
                description=node.get("description") or "",
                capabilities=node.get("capabilities") or [],
            ).model_dump(mode="json")
        if node.get("target_system") or node.get("system"):
            return SystemRefNodeDefinition(
                target_system=node.get("target_system") or node.get("system"),
                target_channel=node.get("target_channel", "stable"),
                target_version=node.get("target_version"),
                title=node.get("title") or "",
                description=node.get("description") or "",
                capabilities=node.get("capabilities") or [],
            ).model_dump(mode="json")
        raise SystemBuilderError(f"Unsupported generated node payload: {node}")

    @staticmethod
    def _normalize_edge(raw_edge: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        edge = dict(raw_edge or {})
        source = edge.get("from") or edge.get("from_node") or edge.get("source")
        target = edge.get("to") or edge.get("to_node") or edge.get("target")
        if LiveSystemBuilder._is_pseudo_node_ref(source) or LiveSystemBuilder._is_pseudo_node_ref(target):
            return None
        return SystemEdge(
            **{
                "from": source,
                "to": target,
                "when": edge.get("when"),
            }
        ).model_dump(mode="json")

    @staticmethod
    def _tool_ref_to_function_name(tool_ref: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_]+", "_", tool_ref.split(".")[-1]).strip("_") or "tool_impl"

    @staticmethod
    def _is_pseudo_node(raw_node: Dict[str, Any]) -> bool:
        node_type = str(raw_node.get("type") or raw_node.get("node_type") or "").lower()
        return node_type in {
            "input",
            "output",
            "entry",
            "exit",
            "system.input",
            "system.output",
            "system",
        }

    @staticmethod
    def _is_pseudo_node_ref(node_ref: Any) -> bool:
        ref = str(node_ref or "").lower()
        return ref in {"system.input", "system.output", "input", "output", "entry", "exit"}

    @staticmethod
    def _builder_contract_errors(spec: BuilderBundleSpec, *, mode: str) -> list[str]:
        errors: list[str] = []
        generated_tool_refs = {tool.tool_ref for tool in spec.local_tools}
        referenced_tool_refs = {
            node.tool_ref
            for node in spec.system_definition.graph.values()
            if isinstance(node, ToolNodeDefinition)
        }
        if mode == "create" and not referenced_tool_refs:
            errors.append("create mode must include at least one tool_node in system_definition")
        if generated_tool_refs and not (generated_tool_refs & referenced_tool_refs):
            errors.append("generated local_tools must be referenced by at least one tool_node")
        return errors

    @staticmethod
    def _is_candidate_ready(invocation_result) -> bool:
        if invocation_result.status.value != "completed" or invocation_result.failed_nodes:
            return False
        for node_result in invocation_result.node_results:
            output = node_result.output
            if not isinstance(output, dict):
                continue
            if output.get("passed") is False:
                return False
            if output.get("valid") is False:
                return False
            if output.get("ready_for_delivery") is False:
                return False
            if output.get("status") == "failed":
                return False
        return True

    @staticmethod
    def _permission_policy() -> PermissionPolicy:
        return PermissionPolicy(
            roles={
                "builder_agent": PermissionRolePolicy(allow=["create_candidate", "create_draft", "run_candidate"]),
                "runtime_agent": PermissionRolePolicy(allow=["invoke_system"]),
                "human_reviewer": PermissionRolePolicy(
                    allow=["promote_candidate", "move_stable_channel", "invoke_system"]
                ),
            }
        )

    @staticmethod
    def _next_version(
        registry: SystemRegistry,
        system_id: str,
        *,
        mode: str,
        base_definition: Optional[SystemDefinition],
    ) -> str:
        if mode == "create":
            return "0.1.0"
        base_version = base_definition.version if base_definition else "0.1.0"
        parts = base_version.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return f"{base_version}.next"
        major, minor, patch = [int(part) for part in parts]
        return f"{major}.{minor + 1}.{patch}"

    def _write_bundle_files(
        self,
        bundle_dir: Path,
        request_text: str,
        spec: BuilderBundleSpec,
        definition: SystemDefinition,
    ) -> list[str]:
        created_files: list[str] = []
        agent_snapshots = self._build_agent_snapshots(definition)
        tool_snapshots = self._build_tool_snapshots(agent_snapshots, spec.local_tools)
        prompt_snapshots = self._build_prompt_snapshots(agent_snapshots)
        bundle_manifest = {
            "request_text": request_text,
            "mode": spec.mode,
            "title": spec.title,
            "description": spec.description,
            "system_id": definition.system_id,
            "version": definition.version,
            "entrypoint": definition.entrypoint,
            "default_agent": self._resolve_default_agent_key(definition),
            "source_of_truth": "config_yaml_and_tools",
            "agent_snapshots": agent_snapshots,
            "tool_snapshots": tool_snapshots,
            "prompt_snapshots": prompt_snapshots,
            "test_payload": spec.test_payload,
            "review_instructions": spec.review_instructions,
            "notes": spec.notes,
        }
        bundle_config = self._build_bundle_config(definition, agent_snapshots, tool_snapshots, prompt_snapshots)

        request_path = bundle_dir / "request.txt"
        spec_path = bundle_dir / "builder_bundle.json"
        bundle_manifest_path = bundle_dir / "bundle_manifest.yaml"
        definition_path = bundle_dir / "system_definition.json"
        definition_yaml_path = bundle_dir / "system_definition.yaml"
        agents_yaml_path = bundle_dir / "agents.yaml"
        prompts_yaml_path = bundle_dir / "prompts.yaml"
        tools_yaml_path = bundle_dir / "tools.yaml"
        review_path = bundle_dir / "review_instructions.md"
        tools_dir = bundle_dir / "tools"
        local_tools_path = bundle_dir / "local_tools.py"

        bundle_artifacts = {
            "request_txt": str(request_path.resolve()),
            "config_yaml": str((bundle_dir / "config.yaml").resolve()),
            "builder_bundle_json": str(spec_path.resolve()),
            "bundle_manifest_yaml": str(bundle_manifest_path.resolve()),
            "system_definition_json": str(definition_path.resolve()),
            "system_definition_yaml": str(definition_yaml_path.resolve()),
            "agents_yaml": str(agents_yaml_path.resolve()),
            "prompts_yaml": str(prompts_yaml_path.resolve()),
            "tools_yaml": str(tools_yaml_path.resolve()),
            "review_instructions_md": str(review_path.resolve()),
            "tools_package_dir": str(tools_dir.resolve()),
            "local_tools_py": str(local_tools_path.resolve()),
        }
        definition.metadata = {
            **definition.metadata,
            "bundle_artifacts": bundle_artifacts,
            "agent_snapshots": agent_snapshots,
            "tool_snapshots": tool_snapshots,
            "prompt_snapshots": prompt_snapshots,
            "source_of_truth": "config_yaml_and_tools",
            "runtime_entry_strategy": "default_agent_response",
            "default_agent_key": self._resolve_default_agent_key(definition),
        }

        request_path.write_text(request_text, encoding="utf-8")
        created_files.append(str(request_path.resolve()))

        spec_path.write_text(json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        created_files.append(str(spec_path.resolve()))

        config_yaml_path = bundle_dir / "config.yaml"
        config_yaml_path.write_text(
            yaml.safe_dump(bundle_config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        created_files.append(str(config_yaml_path.resolve()))

        bundle_manifest_path.write_text(yaml.safe_dump(bundle_manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
        created_files.append(str(bundle_manifest_path.resolve()))

        definition_path.write_text(
            json.dumps(definition.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        created_files.append(str(definition_path.resolve()))

        definition_yaml_path.write_text(
            yaml.safe_dump(definition.model_dump(mode="json", by_alias=True), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        created_files.append(str(definition_yaml_path.resolve()))

        agents_yaml_path.write_text(yaml.safe_dump(agent_snapshots, allow_unicode=True, sort_keys=False), encoding="utf-8")
        created_files.append(str(agents_yaml_path.resolve()))

        prompts_yaml_path.write_text(yaml.safe_dump(prompt_snapshots, allow_unicode=True, sort_keys=False), encoding="utf-8")
        created_files.append(str(prompts_yaml_path.resolve()))

        tools_yaml_path.write_text(yaml.safe_dump(tool_snapshots, allow_unicode=True, sort_keys=False), encoding="utf-8")
        created_files.append(str(tools_yaml_path.resolve()))

        review_path.write_text(spec.review_instructions.strip() + "\n", encoding="utf-8")
        created_files.append(str(review_path.resolve()))

        tools_dir.mkdir(parents=True, exist_ok=True)
        created_files.extend(self._write_tools_package(tools_dir, spec.local_tools))

        local_tools_source = self._compose_local_tools_source(spec.local_tools)
        compile(local_tools_source, str(local_tools_path), "exec")
        local_tools_path.write_text(local_tools_source, encoding="utf-8")
        created_files.append(str(local_tools_path.resolve()))
        return created_files, bundle_artifacts

    def _build_agent_snapshots(self, definition: SystemDefinition) -> Dict[str, Any]:
        snapshots: Dict[str, Any] = {}
        config_agents = self.config.config.agents
        for node in definition.graph.values():
            if not isinstance(node, AgentNodeDefinition):
                continue
            if not node.agent_ref.startswith("agents."):
                continue
            agent_key = node.agent_ref.split(".", 1)[1]
            agent_config = config_agents.get(agent_key)
            snapshots[agent_key] = (
                agent_config.model_dump(mode="json")
                if agent_config is not None
                else {"missing_in_main_config": True, "agent_ref": node.agent_ref}
            )
        return snapshots

    def _build_tool_snapshots(
        self,
        agent_snapshots: Dict[str, Any],
        local_tools: list[GeneratedToolSpec],
    ) -> Dict[str, Any]:
        snapshots: Dict[str, Any] = {}
        config_tools = self.config.config.tools
        referenced_tools = {
            tool_name
            for agent_payload in agent_snapshots.values()
            if isinstance(agent_payload, dict)
            for tool_name in agent_payload.get("tools", [])
        }
        for tool_name in sorted(referenced_tools):
            tool_config = config_tools.get(tool_name)
            snapshots[tool_name] = (
                tool_config.model_dump(mode="json")
                if tool_config is not None
                else {"missing_in_main_config": True, "tool_name": tool_name}
            )
        snapshots["generated_system_tools"] = [
            {
                "tool_ref": tool.tool_ref,
                "function_name": tool.function_name,
                "description": tool.description,
            }
            for tool in local_tools
        ]
        return snapshots

    def _build_prompt_snapshots(self, agent_snapshots: Dict[str, Any]) -> Dict[str, Any]:
        prompt_templates = self.config.config.prompt_templates
        referenced_templates = set()
        for agent_payload in agent_snapshots.values():
            if isinstance(agent_payload, dict):
                base_prompt = agent_payload.get("base_prompt")
                if isinstance(base_prompt, str) and base_prompt:
                    referenced_templates.add(base_prompt)
        return {
            "agent_prompts": {
                agent_key: {
                    "base_prompt": agent_payload.get("base_prompt"),
                    "custom_prompt": agent_payload.get("custom_prompt"),
                }
                for agent_key, agent_payload in agent_snapshots.items()
                if isinstance(agent_payload, dict)
            },
            "prompt_templates": {
                template_key: prompt_templates.get(template_key)
                for template_key in sorted(referenced_templates)
                if template_key in prompt_templates
            },
        }

    def _build_bundle_config(
        self,
        definition: SystemDefinition,
        agent_snapshots: Dict[str, Any],
        tool_snapshots: Dict[str, Any],
        prompt_snapshots: Dict[str, Any],
    ) -> Dict[str, Any]:
        default_agent = self._resolve_default_agent_key(definition)
        used_models = {
            agent_payload.get("model")
            for agent_payload in agent_snapshots.values()
            if isinstance(agent_payload, dict) and agent_payload.get("model")
        }
        tools_section = {
            tool_name: tool_payload
            for tool_name, tool_payload in tool_snapshots.items()
            if tool_name != "generated_system_tools" and isinstance(tool_payload, dict)
        }
        for generated_tool in tool_snapshots.get("generated_system_tools", []):
            tool_name = str(generated_tool.get("function_name") or generated_tool.get("tool_ref"))
            tools_section[tool_name] = {
                "type": "function",
                "description": generated_tool.get("description") or "",
            }

        return {
            "settings": {
                "default_agent": default_agent or "",
                "working_directory": str(self.config.get_working_directory()),
                "config_directory": str(definition.system_id),
                "allow_path_override": True,
                "mcp_enabled": False,
                "project_tools": {
                    "enabled": True,
                    "tools_directory": "tools",
                },
            },
            "models": {
                model_key: self.config.config.models[model_key].model_dump(mode="json")
                for model_key in sorted(used_models)
                if model_key in self.config.config.models
            },
            "tools": tools_section,
            "agents": {
                agent_key: agent_payload
                for agent_key, agent_payload in agent_snapshots.items()
                if isinstance(agent_payload, dict) and not agent_payload.get("missing_in_main_config")
            },
            "prompt_templates": prompt_snapshots.get("prompt_templates", {}),
            "system_runtime": {
                "source_of_truth": "config_yaml_and_tools",
                "default_agent_response_is_output": True,
                "system_definition_ref": "system_definition.yaml",
            },
        }

    @staticmethod
    def _resolve_default_agent_key(definition: SystemDefinition) -> Optional[str]:
        if definition.default_agent:
            return definition.default_agent
        entry_node = definition.graph.get(definition.entrypoint)
        if isinstance(entry_node, AgentNodeDefinition) and entry_node.agent_ref.startswith("agents."):
            return entry_node.agent_ref.split(".", 1)[1]
        return None

    @staticmethod
    def _write_tools_package(tools_dir: Path, local_tools: list[GeneratedToolSpec]) -> list[str]:
        created_files: list[str] = []
        module_path = tools_dir / "generated_tools.py"
        module_source = LiveSystemBuilder._compose_local_tools_source(local_tools)
        compile(module_source, str(module_path), "exec")
        module_path.write_text(module_source, encoding="utf-8")
        created_files.append(str(module_path.resolve()))
        init_source = '\n'.join(
            [
                '"""Generated system tool package."""',
                "",
                "from .generated_tools import LOCAL_TOOL_EXECUTORS",
                "",
                "__all__ = ['LOCAL_TOOL_EXECUTORS']",
                "",
            ]
        )
        init_path = tools_dir / "__init__.py"
        init_path.write_text(init_source, encoding="utf-8")
        created_files.append(str(init_path.resolve()))
        return created_files

    @staticmethod
    def _compose_local_tools_source(local_tools: list[GeneratedToolSpec]) -> str:
        header = [
            '"""Generated system-local tools."""',
            "",
            "from __future__ import annotations",
            "",
            "from typing import Any, Dict",
            "",
        ]
        body: list[str] = []
        mapping_lines = ["LOCAL_TOOL_EXECUTORS = {"]
        for tool in local_tools:
            body.append(tool.python_code.rstrip())
            body.append("")
            mapping_lines.append(f'    "{tool.tool_ref}": {tool.function_name},')
        mapping_lines.append("}")
        body.extend(mapping_lines)
        return "\n".join([*header, *body]).strip() + "\n"

    @staticmethod
    def _load_local_tool_executors(bundle_dir: Path) -> Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]:
        candidate_paths = [
            bundle_dir / "tools" / "generated_tools.py",
            bundle_dir / "tools" / "__init__.py",
            bundle_dir / "local_tools.py",
        ]
        local_tools_path = next((path for path in candidate_paths if path.exists()), None)
        if local_tools_path is None:
            raise SystemBuilderError(f"Cannot find generated tool module in {bundle_dir}")
        module_name = f"generated_bundle_{bundle_dir.parent.name}_{bundle_dir.name}_{local_tools_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, local_tools_path)
        if spec is None or spec.loader is None:
            raise SystemBuilderError(f"Cannot load local tools module from {local_tools_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        executors = getattr(module, "LOCAL_TOOL_EXECUTORS", None)
        if not isinstance(executors, dict):
            raise SystemBuilderError("Generated local tools module does not export LOCAL_TOOL_EXECUTORS")
        return executors

    @staticmethod
    def _build_tool_executor(
        local_tools: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]
    ) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
        def execute(tool_ref: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            executor = local_tools.get(tool_ref)
            if executor is None:
                return {"tool_ref": tool_ref, "input": payload}
            return executor(payload)

        return execute

    @staticmethod
    def _agent_executor(agent_ref: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        task = payload.get("task") or payload.get("goal") or payload.get("request") or "unspecified"
        previous = payload.get("previous") or {}
        if agent_ref == "agents.kimi_engineer":
            return {
                "agent_ref": agent_ref,
                "task": task,
                "subtasks": [
                    f"inspect repo for {task}",
                    f"implement change for {task}",
                ],
            }
        if agent_ref == "agents.coordinator":
            seed = previous.get("subtasks") or []
            return {
                "agent_ref": agent_ref,
                "task": task,
                "subtasks": [
                    *seed,
                    f"plan {task}",
                    f"execute {task}",
                    f"test {task}",
                ],
            }
        if agent_ref == "agents.platform_planner":
            files = payload.get("files") or previous.get("files") or ["README.md", "report.md"]
            return {
                "agent_ref": agent_ref,
                "task": task,
                "files": files,
            }
        if agent_ref == "agents.validator":
            candidate = previous or payload
            return {
                "agent_ref": agent_ref,
                "passed": bool(candidate),
                "summary": "Validated candidate payload",
            }
        return {"agent_ref": agent_ref, "input": payload}
