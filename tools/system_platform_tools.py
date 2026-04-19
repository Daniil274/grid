"""Tools for interacting with the self-organizing system platform."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import yaml

from agents import RunContextWrapper, function_tool

from core.platform.events import DomainEventBus
from core.managers.project_tools_loader import ProjectToolsLoader
from core.platform.registry import SystemRegistry
from core.platform.runtime import SystemRuntime
from core.platform.mutation import MutationSet
from core.platform.workbench import SystemWorkbench
from schemas.system_platform import SystemDefinition, SystemVersionStatus

logger = logging.getLogger("grid.system_platform_tools")
verbose_logger = logging.getLogger("grid.verbose")


def _get_factory(context: RunContextWrapper) -> Any:
    raw = getattr(context, "context", None)
    return getattr(raw, "factory", None)


def _resolve_registry_path(context: RunContextWrapper) -> Path:
    factory = _get_factory(context)
    config = getattr(factory, "config", None) if factory is not None else None
    if config is not None:
        getter = getattr(config, "get_system_registry_path", None)
        if callable(getter):
            return Path(getter())
        return Path(config.get_absolute_path("data/system_registry.json"))
    return Path("data/system_registry.json")


def _get_registry(context: RunContextWrapper) -> SystemRegistry:
    return SystemRegistry(_resolve_registry_path(context), event_bus=DomainEventBus())


def _get_workbench(context: RunContextWrapper) -> SystemWorkbench:
    return SystemWorkbench(_get_registry(context))


def _resolve_preferred_definition(registry: SystemRegistry, system_id: str) -> SystemDefinition:
    try:
        return registry.get_definition(system_id, channel="stable")
    except Exception:
        pass
    try:
        return registry.get_definition(system_id, channel="canary")
    except Exception:
        pass
    versions = registry.list_versions(system_id)
    if not versions:
        raise KeyError(f"System '{system_id}' has no registered versions")
    latest = versions[-1]
    return latest.definition


def _candidate_tools_dirs(base_dir: Path, system_id: str, version: str) -> list[Path]:
    """Return candidate tools/ directories for the config-based bundle layout."""
    return [
        base_dir / "workspace" / "generated_systems_from_agent" / system_id / version / "tools",
        base_dir / "workspace" / "generated_systems_live" / system_id / version / "tools",
    ]


def _load_tools_dir(tools_dir: Path, system_id: str) -> Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]:
    """Load @function_tool callables from tools/ dir and wrap them as plain dict->dict executors."""
    bundle_dir = tools_dir.parent
    loader = ProjectToolsLoader(str(bundle_dir), "tools")
    function_tools = loader.load_project_tools()
    executors: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
    for name, obj in function_tools.items():
        # Support both "tool_name" and "system_id.tool_name" refs
        def _make_executor(fn: Any) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
            def executor(payload: Dict[str, Any]) -> Dict[str, Any]:
                try:
                    # FunctionTool from agents SDK — call its underlying function
                    inner = getattr(fn, "__wrapped__", None) or getattr(fn, "fn", None)
                    if inner is not None and callable(inner):
                        return inner(payload) or {}  # type: ignore[arg-type]
                    if callable(fn):
                        return fn(payload) or {}  # type: ignore[arg-type]
                except Exception as exc:  # noqa: BLE001
                    return {"error": str(exc), "tool": name}
                return {}
            return executor
        executors[name] = _make_executor(obj)
        executors[f"{system_id}.{name}"] = executors[name]
    return executors


def _find_bundle_dir(base_dir: Path, system_id: str, version: str) -> Optional[Path]:
    for candidate in (
        base_dir / "workspace" / "generated_systems_live" / system_id / version,
        base_dir / "workspace" / "generated_systems_from_agent" / system_id / version,
    ):
        if candidate.exists():
            return candidate
    return None


def _enrich_definition_with_bundle_artifacts(
    base_dir: Path,
    definition: SystemDefinition,
) -> SystemDefinition:
    bundle_dir = _find_bundle_dir(base_dir, definition.system_id, definition.version)
    if bundle_dir is None:
        return definition

    artifact_paths = {
        "bundle_manifest_yaml": bundle_dir / "bundle_manifest.yaml",
        "builder_bundle_json": bundle_dir / "builder_bundle.json",
        "system_definition_json": bundle_dir / "system_definition.json",
        "system_definition_yaml": bundle_dir / "system_definition.yaml",
        "agents_yaml": bundle_dir / "agents.yaml",
        "prompts_yaml": bundle_dir / "prompts.yaml",
        "tools_yaml": bundle_dir / "tools.yaml",
        "review_instructions_md": bundle_dir / "review_instructions.md",
        "request_txt": bundle_dir / "request.txt",
        "tools_package_dir": bundle_dir / "tools",
        "local_tools_py": bundle_dir / "local_tools.py",
    }
    bundle_artifacts = {
        name: str(path.resolve())
        for name, path in artifact_paths.items()
        if path.exists()
    }
    metadata = dict(definition.metadata or {})
    metadata["bundle_artifacts"] = bundle_artifacts
    for snapshot_name, filename in (
        ("agent_snapshots", "agents.yaml"),
        ("prompt_snapshots", "prompts.yaml"),
        ("tool_snapshots", "tools.yaml"),
    ):
        path = bundle_dir / filename
        if path.exists():
            metadata[snapshot_name] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    definition.metadata = metadata
    return definition


def _build_dynamic_tool_executor(
    registry: SystemRegistry,
    base_dir: Path,
) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    cache: Dict[str, Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = {}

    def execute(tool_ref: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        system_id = tool_ref.split(".", 1)[0]
        if not system_id or "." not in tool_ref:
            return {"tool_ref": tool_ref, "input": payload}

        try:
            version_record = registry.get_version(
                system_id,
                str(payload.get("candidate_version") or payload.get("version") or registry.get_release_state(system_id).channels.get("stable") or ""),
            )
            version = version_record.version
        except Exception:
            try:
                version = registry.get_release_state(system_id).channels.get("stable") or registry.get_release_state(system_id).channels.get("canary")
            except Exception:
                version = None
        if not version:
            return {"tool_ref": tool_ref, "input": payload}

        cache_key = f"{system_id}:{version}"
        executors = cache.get(cache_key)
        if executors is None:
            for tools_dir in _candidate_tools_dirs(base_dir, system_id, version):
                if tools_dir.exists() and tools_dir.is_dir() and any(tools_dir.glob("*.py")):
                    executors = _load_tools_dir(tools_dir, system_id)
                    cache[cache_key] = executors
                    break
        if not executors:
            return {"tool_ref": tool_ref, "input": payload}

        executor = executors.get(tool_ref)
        if executor is None:
            return {"tool_ref": tool_ref, "input": payload}
        return executor(payload)

    return execute


def _build_real_agent_executor(
    context: RunContextWrapper,
) -> Callable[[str, Dict[str, Any]], Dict[str, Any]]:
    """Return an agent executor that routes calls through AgentFactory when available."""
    factory = _get_factory(context)
    if factory is None:
        return _default_agent_executor

    def execute(agent_ref: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        # "agents.chat_agent" → "chat_agent"
        agent_key = agent_ref.split(".", 1)[-1] if "." in agent_ref else agent_ref
        task = str(
            payload.get("task")
            or payload.get("request_text")
            or payload.get("goal")
            or json.dumps(payload, ensure_ascii=False)
        )
        try:
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, factory.run_agent(agent_key, task))
                    response = future.result(timeout=300)
            else:
                response = loop.run_until_complete(factory.run_agent(agent_key, task))
            return {"agent_ref": agent_ref, "response": response}
        except Exception as exc:  # noqa: BLE001
            logger.warning("_build_real_agent_executor: agent_ref=%s error=%s", agent_ref, exc)
            return {"agent_ref": agent_ref, "error": str(exc), "fallback": True}

    return execute


def _default_agent_executor(agent_ref: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    task = str(payload.get("task") or payload.get("request_text") or payload.get("goal") or "unspecified")
    previous = payload.get("previous") if isinstance(payload.get("previous"), dict) else {}

    if agent_ref == "agents.platform_planner":
        plan_steps = [
            f"Analyze requirements for: {task}",
            "Design implementation approach",
            "Implement the required changes",
            "Run validation and testing steps",
        ]
        return {
            "agent_ref": agent_ref,
            "task": task,
            "plan": "\n".join(f"{index + 1}. {step}" for index, step in enumerate(plan_steps)),
            "files": payload.get("files") or previous.get("files") or [],
        }
    if agent_ref == "agents.kimi_engineer":
        return {
            "agent_ref": agent_ref,
            "task": task,
            "implementation_summary": f"Implemented requested task: {task}",
            "deliverables": payload.get("files") or previous.get("files") or [],
            "tests": ["unit tests", "validation checks"],
        }
    if agent_ref == "agents.validator":
        candidate = previous or payload
        return {
            "agent_ref": agent_ref,
            "passed": bool(candidate),
            "summary": "Validated candidate payload",
            "validated_fields": sorted(candidate.keys()) if isinstance(candidate, dict) else [],
        }
    return {"agent_ref": agent_ref, "input": payload}


@function_tool
def system_list_systems(context: RunContextWrapper) -> str:
    """List registered systems in the platform registry."""
    registry = _get_registry(context)
    manifests = registry.list_systems()
    verbose_logger.info("system_list_systems count=%s", len(manifests))
    return json.dumps([item.model_dump(mode="json") for item in manifests], ensure_ascii=False, indent=2)


@function_tool
def system_get_system_info(
    context: RunContextWrapper,
    system_id: str,
    include_definition: bool = False,
    include_artifacts: bool = False,
) -> str:
    """Get manifest and optional stable definition for a registered system."""
    registry = _get_registry(context)
    manifest = registry.get_manifest(system_id)
    verbose_logger.info(
        "system_get_system_info system_id=%s include_definition=%s include_artifacts=%s",
        system_id,
        include_definition,
        include_artifacts,
    )
    payload: Dict[str, Any] = {"manifest": manifest.model_dump(mode="json")}
    if include_definition:
        definition = _resolve_preferred_definition(registry, system_id)
        payload["definition"] = definition.model_dump(mode="json", by_alias=True)
        if include_artifacts:
            artifact_paths = definition.metadata.get("bundle_artifacts") if isinstance(definition.metadata, dict) else {}
            artifacts_payload: Dict[str, Any] = {}
            for artifact_name, artifact_path in (artifact_paths or {}).items():
                path_obj = Path(artifact_path)
                if not path_obj.exists():
                    artifacts_payload[artifact_name] = {"path": artifact_path, "exists": False}
                    continue
                suffix = path_obj.suffix.lower()
                if suffix in {".yaml", ".yml"}:
                    content = yaml.safe_load(path_obj.read_text(encoding="utf-8"))
                elif suffix == ".json":
                    content = json.loads(path_obj.read_text(encoding="utf-8"))
                elif suffix == ".md" or suffix == ".txt":
                    content = path_obj.read_text(encoding="utf-8")
                else:
                    content = {"path": artifact_path}
                artifacts_payload[artifact_name] = {
                    "path": artifact_path,
                    "exists": True,
                    "content": content,
                }
            payload["artifacts"] = artifacts_payload
    return json.dumps(payload, ensure_ascii=False, indent=2)


@function_tool
def system_get_system_versions(
    context: RunContextWrapper,
    system_id: str,
) -> str:
    """List all known versions for a system."""
    registry = _get_registry(context)
    versions = registry.list_versions(system_id)
    verbose_logger.info("system_get_system_versions system_id=%s count=%s", system_id, len(versions))
    return json.dumps([item.model_dump(mode="json") for item in versions], ensure_ascii=False, indent=2)


@function_tool
def system_invoke_system(
    context: RunContextWrapper,
    system_id: str,
    input_payload_json: Optional[str] = None,
    channel: Optional[str] = "stable",
    version: Optional[str] = None,
    actor_role: str = "runtime_agent",
) -> str:
    """Invoke a registered system through the platform runtime."""
    payload = {}
    if input_payload_json:
        payload = json.loads(input_payload_json)
        if not isinstance(payload, dict):
            raise ValueError("input_payload_json must decode to an object")
    registry = _get_registry(context)
    verbose_logger.info(
        "system_invoke_system start system_id=%s channel=%s version=%s actor_role=%s payload_keys=%s",
        system_id,
        channel,
        version,
        actor_role,
        sorted(payload.keys()),
    )
    runtime = SystemRuntime(
        registry,
        agent_executor=_build_real_agent_executor(context),
        tool_executor=_build_dynamic_tool_executor(registry, _resolve_registry_path(context).parent.parent),
    )
    result = runtime.invoke(
        system_id,
        channel=channel,
        version=version,
        input_payload=payload,
        actor_role=actor_role,
    )
    verbose_logger.info(
        "system_invoke_system done system_id=%s resolved_version=%s status=%s failed_nodes=%s",
        system_id,
        result.resolved_version,
        result.status.value,
        result.failed_nodes,
    )
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def system_create_version(
    context: RunContextWrapper,
    definition_json: str,
    actor_role: str = "builder_agent",
    created_by: str = "system_platform_tools",
    status: str = "candidate",
) -> str:
    """Create a new system version from a full system definition payload."""
    definition = SystemDefinition(**json.loads(definition_json))
    definition = _enrich_definition_with_bundle_artifacts(_resolve_registry_path(context).parent.parent, definition)
    verbose_logger.info(
        "system_create_version system_id=%s version=%s status=%s actor_role=%s created_by=%s",
        definition.system_id,
        definition.version,
        status,
        actor_role,
        created_by,
    )
    workbench = _get_workbench(context)
    record = workbench.create_version(
        definition,
        actor_role=actor_role,
        created_by=created_by,
        status=SystemVersionStatus(status),
    )
    return json.dumps(record.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)


@function_tool
def system_clone_version(
    context: RunContextWrapper,
    system_id: str,
    new_version: str,
    source_version: Optional[str] = None,
    source_channel: Optional[str] = "stable",
    actor_role: str = "builder_agent",
    created_by: str = "system_platform_tools",
    status: str = "candidate",
) -> str:
    """Clone an existing system version into a new draft or candidate version."""
    workbench = _get_workbench(context)
    verbose_logger.info(
        "system_clone_version system_id=%s new_version=%s source_version=%s source_channel=%s status=%s",
        system_id,
        new_version,
        source_version,
        source_channel,
        status,
    )
    record = workbench.clone_version(
        system_id,
        new_version=new_version,
        source_version=source_version,
        source_channel=source_channel,
        actor_role=actor_role,
        created_by=created_by,
        status=SystemVersionStatus(status),
    )
    return json.dumps(record.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)


@function_tool
def system_apply_mutations(
    context: RunContextWrapper,
    system_id: str,
    mutations_json: str,
    new_version: str,
    source_version: Optional[str] = None,
    source_channel: Optional[str] = "stable",
    actor_role: str = "builder_agent",
    created_by: str = "system_platform_tools",
    status: str = "candidate",
) -> str:
    """Apply a bounded mutation set to an existing system and register the result as a new version."""
    mutation_list = json.loads(mutations_json)
    if not isinstance(mutation_list, list):
        raise ValueError("mutations_json must decode to a list")
    verbose_logger.info(
        "system_apply_mutations system_id=%s new_version=%s source_version=%s source_channel=%s mutation_count=%s status=%s",
        system_id,
        new_version,
        source_version,
        source_channel,
        len(mutation_list),
        status,
    )
    mutation_set = MutationSet(mutation_list)
    workbench = _get_workbench(context)
    record = workbench.mutate_version(
        system_id,
        mutation_set,
        new_version=new_version,
        source_version=source_version,
        source_channel=source_channel,
        actor_role=actor_role,
        created_by=created_by,
        status=SystemVersionStatus(status),
    )
    return json.dumps(record.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)


@function_tool
def system_promote_version(
    context: RunContextWrapper,
    system_id: str,
    version: str,
    target_channel: str,
    actor_role: str = "human_reviewer",
    expected_current: Optional[str] = None,
) -> str:
    """Promote a system version into canary or stable channel."""
    workbench = _get_workbench(context)
    verbose_logger.info(
        "system_promote_version system_id=%s version=%s target_channel=%s actor_role=%s expected_current=%s",
        system_id,
        version,
        target_channel,
        actor_role,
        expected_current,
    )
    if target_channel == "canary":
        state = workbench.send_to_canary(system_id, version, actor_role=actor_role)
    elif target_channel == "stable":
        state = workbench.promote_to_stable(
            system_id,
            version,
            actor_role=actor_role,
            expected_current=expected_current,
        )
    else:
        raise ValueError("target_channel must be 'canary' or 'stable'")
    return json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)


@function_tool
def system_reject_version(
    context: RunContextWrapper,
    system_id: str,
    version: str,
) -> str:
    """Reject a candidate or canary version."""
    workbench = _get_workbench(context)
    verbose_logger.info("system_reject_version system_id=%s version=%s", system_id, version)
    record = workbench.reject_version(system_id, version)
    return json.dumps(record.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)


@function_tool
def system_rollback_stable(
    context: RunContextWrapper,
    system_id: str,
    rollback_to_version: str,
    actor_role: str = "human_reviewer",
) -> str:
    """Rollback the stable channel to a previous version."""
    workbench = _get_workbench(context)
    verbose_logger.info(
        "system_rollback_stable system_id=%s rollback_to_version=%s actor_role=%s",
        system_id,
        rollback_to_version,
        actor_role,
    )
    state = workbench.rollback_stable(system_id, rollback_to_version, actor_role=actor_role)
    return json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)


SYSTEM_PLATFORM_TOOLS: Dict[str, Any] = {
    "system_list_systems": system_list_systems,
    "system_get_system_info": system_get_system_info,
    "system_get_system_versions": system_get_system_versions,
    "system_invoke_system": system_invoke_system,
    "system_create_version": system_create_version,
    "system_clone_version": system_clone_version,
    "system_apply_mutations": system_apply_mutations,
    "system_promote_version": system_promote_version,
    "system_reject_version": system_reject_version,
    "system_rollback_stable": system_rollback_stable,
}
