"""Transactional-ish file-backed registry for system definitions and releases."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Dict, List, Optional

from core.event_bus import DomainEvent, DomainEventBus
from core.system_compiler import SystemCompiler
from core.system_governance import BudgetTracker, PermissionChecker
from schemas.system_platform import (
    SystemDefinition,
    SystemManifest,
    SystemRegistryState,
    SystemReleaseState,
    SystemVersionRecord,
    SystemVersionStatus,
)


class ChannelConflictError(ValueError):
    """Raised when compare-and-swap channel promotion fails."""


class SystemRegistry:
    """Registry for system versions, manifests, and release channels."""

    def __init__(
        self,
        registry_path: str | Path = "data/system_registry.json",
        *,
        event_bus: Optional[DomainEventBus] = None,
        compiler: Optional[SystemCompiler] = None,
        permission_checker: Optional[PermissionChecker] = None,
        budget_tracker: Optional[BudgetTracker] = None,
    ) -> None:
        self._path = Path(registry_path)
        self._lock = RLock()
        self._system_locks: Dict[str, RLock] = defaultdict(RLock)
        self._event_bus = event_bus or DomainEventBus()
        self._compiler = compiler or SystemCompiler()
        self._permission_checker = permission_checker or PermissionChecker()
        self._budget_tracker = budget_tracker or BudgetTracker()
        self._state = self._load()

    def _load(self) -> SystemRegistryState:
        if not self._path.exists():
            return SystemRegistryState()
        with self._path.open("r", encoding="utf-8") as fh:
            return SystemRegistryState(**json.load(fh))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            json.dump(self._state.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)

    def list_systems(self) -> List[SystemManifest]:
        with self._lock:
            return sorted(self._state.manifests.values(), key=lambda item: item.system_id)

    def get_manifest(self, system_id: str) -> SystemManifest:
        with self._lock:
            if system_id not in self._state.manifests:
                raise KeyError(f"System '{system_id}' not found")
            return self._state.manifests[system_id]

    def get_version(self, system_id: str, version: str) -> SystemVersionRecord:
        with self._lock:
            versions = self._state.versions.get(system_id, {})
            if version not in versions:
                raise KeyError(f"Version '{version}' for system '{system_id}' not found")
            return versions[version]

    def get_definition(
        self,
        system_id: str,
        *,
        version: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> SystemDefinition:
        with self._lock:
            resolved_version = version
            if resolved_version is None:
                if channel is None:
                    channel = "stable"
                release = self._state.releases.get(system_id)
                if release is None or channel not in release.channels:
                    raise KeyError(f"Channel '{channel}' for system '{system_id}' not found")
                resolved_version = release.channels[channel]
            return self.get_version(system_id, resolved_version).definition

    def get_release_state(self, system_id: str) -> SystemReleaseState:
        with self._lock:
            if system_id not in self._state.releases:
                raise KeyError(f"Release state for system '{system_id}' not found")
            return self._state.releases[system_id]

    def register_system(
        self,
        definition: SystemDefinition,
        *,
        actor_role: str = "builder_agent",
        created_by: str = "system",
        status: SystemVersionStatus = SystemVersionStatus.CANDIDATE,
        parent_version: Optional[str] = None,
    ) -> SystemVersionRecord:
        system_id = definition.system_id
        with self._system_locks[system_id]:
            with self._lock:
                create_action = "create_draft" if status == SystemVersionStatus.DRAFT else "create_candidate"
                try:
                    self._permission_checker.assert_allowed(
                        definition.policy.permissions, actor_role, create_action
                    )
                except Exception:
                    if create_action != "create_candidate":
                        self._permission_checker.assert_allowed(
                            definition.policy.permissions, actor_role, "create_candidate"
                        )
                    else:
                        raise
                if system_id not in self._state.versions:
                    self._budget_tracker.assert_can_create_system(definition.policy.budgets)
                self._budget_tracker.assert_can_create_candidate(definition.policy.budgets, system_id)
                self._compiler.compile(
                    definition,
                    dependency_resolver=lambda dep_system_id, dep_version, dep_channel: self._resolve_dependency(
                        dep_system_id,
                        dep_version,
                        dep_channel,
                        pending_definition=definition,
                    ),
                )
                versions = self._state.versions.setdefault(system_id, {})
                if definition.version in versions:
                    raise ValueError(
                        f"System '{system_id}' version '{definition.version}' is already registered"
                    )

                record = SystemVersionRecord(
                    system_id=system_id,
                    version=definition.version,
                    status=status,
                    definition=definition,
                    parent_version=parent_version,
                    created_by=created_by,
                )
                versions[definition.version] = record
                self._state.manifests[system_id] = self._build_manifest(definition)
                self._state.releases.setdefault(system_id, SystemReleaseState(system_id=system_id))
                self._save()

                if len(versions) == 1:
                    self._budget_tracker.record_system_created()
                self._budget_tracker.record_candidate_created(system_id)
                self._event_bus.publish(
                    DomainEvent(
                        event_type="system_version_created",
                        payload={"system_id": system_id, "version": definition.version},
                    )
                )
                return record

    def set_version_status(
        self,
        system_id: str,
        version: str,
        status: SystemVersionStatus,
    ) -> SystemVersionRecord:
        with self._system_locks[system_id]:
            with self._lock:
                record = self.get_version(system_id, version)
                old_status = record.status
                record.status = status
                if old_status in {SystemVersionStatus.CANDIDATE, SystemVersionStatus.CANARY} and status in {
                    SystemVersionStatus.REJECTED,
                    SystemVersionStatus.ARCHIVED,
                    SystemVersionStatus.STABLE,
                }:
                    self._budget_tracker.record_candidate_closed(system_id)
                if old_status == SystemVersionStatus.CANARY and status != SystemVersionStatus.CANARY:
                    self._budget_tracker.record_canary_finished()
                self._save()
                return record

    def promote_channel(
        self,
        system_id: str,
        channel: str,
        target_version: str,
        *,
        actor_role: str = "human_reviewer",
        expected_current: Optional[str] = None,
    ) -> SystemReleaseState:
        with self._system_locks[system_id]:
            with self._lock:
                record = self.get_version(system_id, target_version)
                self._permission_checker.assert_allowed(
                    record.definition.policy.permissions,
                    actor_role,
                    "move_stable_channel" if channel == "stable" else "promote_candidate",
                )
                if channel == "canary":
                    self._budget_tracker.assert_can_start_canary(record.definition.policy.budgets)
                if channel == "stable":
                    self._budget_tracker.assert_can_promote(record.definition.policy.budgets)

                release = self._state.releases.setdefault(system_id, SystemReleaseState(system_id=system_id))
                current = release.channels.get(channel)
                if expected_current is not None and current != expected_current:
                    raise ChannelConflictError(
                        f"Channel '{channel}' conflict for system '{system_id}': expected {expected_current}, got {current}"
                    )

                release.channels[channel] = target_version
                release.revision += 1
                release.updated_at = datetime.utcnow().isoformat()
                if channel == "canary":
                    self._budget_tracker.record_canary_started()
                    record.status = SystemVersionStatus.CANARY
                elif channel == "stable":
                    self._budget_tracker.record_promoted()
                    record.status = SystemVersionStatus.STABLE
                    self._state.manifests[system_id].latest_stable = target_version
                self._save()
                self._event_bus.publish(
                    DomainEvent(
                        event_type="system_version_promoted",
                        payload={"system_id": system_id, "channel": channel, "version": target_version},
                    )
                )
                return release

    def rollback_channel(
        self,
        system_id: str,
        channel: str,
        rollback_to_version: str,
        *,
        actor_role: str = "human_reviewer",
    ) -> SystemReleaseState:
        return self.promote_channel(
            system_id,
            channel,
            rollback_to_version,
            actor_role=actor_role,
            expected_current=None,
        )

    def list_versions(self, system_id: str) -> List[SystemVersionRecord]:
        with self._lock:
            versions = list(self._state.versions.get(system_id, {}).values())
            return sorted(versions, key=lambda item: item.created_at)

    def _build_manifest(self, definition: SystemDefinition) -> SystemManifest:
        return SystemManifest(
            system_id=definition.system_id,
            title=definition.metadata.get("title", definition.system_id),
            description=definition.metadata.get("description", ""),
            capabilities=list(definition.capabilities),
            task_types=list(definition.task_types),
            input_schema=definition.interface.input_schema,
            output_schema=definition.interface.output_schema,
        )

    def _resolve_dependency(
        self,
        system_id: str,
        version: Optional[str],
        channel: Optional[str],
        pending_definition: Optional[SystemDefinition] = None,
    ) -> Optional[SystemDefinition]:
        if pending_definition is not None and pending_definition.system_id == system_id:
            if version is None or pending_definition.version == version:
                return pending_definition
        versions = self._state.versions.get(system_id, {})
        if version is not None and version in versions:
            return versions[version].definition
        if version is None and versions:
            if channel is not None:
                release = self._state.releases.get(system_id)
                if release and channel in release.channels:
                    resolved = release.channels[channel]
                    if resolved in versions:
                        return versions[resolved].definition
            latest = sorted(versions.values(), key=lambda item: item.created_at)[-1]
            return latest.definition
        try:
            return self.get_definition(system_id, version=version, channel=channel)
        except KeyError:
            return None
