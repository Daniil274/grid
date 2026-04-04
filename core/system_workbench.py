"""High-level bounded workflow for building and releasing system versions."""

from __future__ import annotations

from typing import Optional

from schemas.system_platform import SystemDefinition, SystemReleaseState, SystemVersionRecord, SystemVersionStatus

from .system_mutation import MutationSet, SystemMutator
from .system_registry import SystemRegistry
from .system_release_manager import CandidatePromotionFlow


class SystemWorkbench:
    """Practical orchestration layer for the platform MVP lifecycle."""

    def __init__(
        self,
        registry: SystemRegistry,
        *,
        mutator: Optional[SystemMutator] = None,
        promotion_flow: Optional[CandidatePromotionFlow] = None,
    ) -> None:
        self.registry = registry
        self.mutator = mutator or SystemMutator()
        self.promotion_flow = promotion_flow or CandidatePromotionFlow(registry)

    def create_version(
        self,
        definition: SystemDefinition,
        *,
        actor_role: str = "builder_agent",
        created_by: str = "system_workbench",
        status: SystemVersionStatus = SystemVersionStatus.CANDIDATE,
        parent_version: Optional[str] = None,
    ) -> SystemVersionRecord:
        return self.registry.register_system(
            definition,
            actor_role=actor_role,
            created_by=created_by,
            status=status,
            parent_version=parent_version,
        )

    def clone_version(
        self,
        system_id: str,
        *,
        new_version: str,
        source_version: Optional[str] = None,
        source_channel: Optional[str] = "stable",
        actor_role: str = "builder_agent",
        created_by: str = "system_workbench",
        status: SystemVersionStatus = SystemVersionStatus.CANDIDATE,
    ) -> SystemVersionRecord:
        base = self.registry.get_definition(system_id, version=source_version, channel=source_channel)
        cloned = base.model_copy(deep=True)
        cloned.version = new_version
        return self.create_version(
            cloned,
            actor_role=actor_role,
            created_by=created_by,
            status=status,
            parent_version=base.version,
        )

    def mutate_version(
        self,
        system_id: str,
        mutation_set: MutationSet,
        *,
        new_version: str,
        source_version: Optional[str] = None,
        source_channel: Optional[str] = "stable",
        actor_role: str = "builder_agent",
        created_by: str = "system_workbench",
        status: SystemVersionStatus = SystemVersionStatus.CANDIDATE,
    ) -> SystemVersionRecord:
        base = self.registry.get_definition(system_id, version=source_version, channel=source_channel)
        updated = self.mutator.apply(base, mutation_set)
        updated.version = new_version
        return self.create_version(
            updated,
            actor_role=actor_role,
            created_by=created_by,
            status=status,
            parent_version=base.version,
        )

    def send_to_canary(
        self,
        system_id: str,
        version: str,
        *,
        actor_role: str = "human_reviewer",
    ) -> SystemReleaseState:
        return self.promotion_flow.send_to_canary(system_id, version, actor_role=actor_role)

    def promote_to_stable(
        self,
        system_id: str,
        version: str,
        *,
        actor_role: str = "human_reviewer",
        expected_current: Optional[str] = None,
    ) -> SystemReleaseState:
        return self.promotion_flow.promote_to_stable(
            system_id,
            version,
            actor_role=actor_role,
            expected_current=expected_current,
        )

    def reject_version(self, system_id: str, version: str) -> SystemVersionRecord:
        return self.promotion_flow.reject(system_id, version)

    def archive_version(self, system_id: str, version: str) -> SystemVersionRecord:
        return self.promotion_flow.archive(system_id, version)

    def rollback_stable(
        self,
        system_id: str,
        rollback_to_version: str,
        *,
        actor_role: str = "human_reviewer",
    ) -> SystemReleaseState:
        return self.promotion_flow.rollback_stable(
            system_id,
            rollback_to_version,
            actor_role=actor_role,
        )
