"""Tools for interacting with the self-organizing system platform."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from agents import RunContextWrapper, function_tool

from core.event_bus import DomainEventBus
from core.system_registry import SystemRegistry
from core.system_runtime import SystemRuntime
from core.system_mutation import MutationSet
from core.system_workbench import SystemWorkbench
from schemas.system_platform import SystemDefinition, SystemVersionStatus


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


@function_tool
def system_list_systems(context: RunContextWrapper) -> str:
    """List registered systems in the platform registry."""
    registry = _get_registry(context)
    manifests = registry.list_systems()
    return json.dumps([item.model_dump(mode="json") for item in manifests], ensure_ascii=False, indent=2)


@function_tool
def system_get_system_info(
    context: RunContextWrapper,
    system_id: str,
    include_definition: bool = False,
) -> str:
    """Get manifest and optional stable definition for a registered system."""
    registry = _get_registry(context)
    manifest = registry.get_manifest(system_id)
    payload: Dict[str, Any] = {"manifest": manifest.model_dump(mode="json")}
    if include_definition:
        definition = registry.get_definition(system_id, channel="stable")
        payload["definition"] = definition.model_dump(mode="json", by_alias=True)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@function_tool
def system_get_system_versions(
    context: RunContextWrapper,
    system_id: str,
) -> str:
    """List all known versions for a system."""
    registry = _get_registry(context)
    versions = registry.list_versions(system_id)
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
    runtime = SystemRuntime(registry)
    result = runtime.invoke(
        system_id,
        channel=channel,
        version=version,
        input_payload=payload,
        actor_role=actor_role,
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
