"""Centralized lifecycle transitions for system versions and release channels."""

from __future__ import annotations

from schemas.system_platform import SystemReleaseState, SystemVersionRecord, SystemVersionStatus

from .system_registry import SystemRegistry


class LifecycleStateMachine:
    """Minimal centralized lifecycle rules for MVP."""

    ALLOWED_TRANSITIONS = {
        SystemVersionStatus.DRAFT: {SystemVersionStatus.CANDIDATE, SystemVersionStatus.REJECTED},
        SystemVersionStatus.CANDIDATE: {
            SystemVersionStatus.CANARY,
            SystemVersionStatus.REJECTED,
            SystemVersionStatus.BLOCKED,
            SystemVersionStatus.ARCHIVED,
        },
        SystemVersionStatus.CANARY: {
            SystemVersionStatus.STABLE,
            SystemVersionStatus.REJECTED,
            SystemVersionStatus.ARCHIVED,
        },
        SystemVersionStatus.STABLE: {SystemVersionStatus.ARCHIVED},
        SystemVersionStatus.BLOCKED: {SystemVersionStatus.CANDIDATE, SystemVersionStatus.REJECTED},
        SystemVersionStatus.REJECTED: set(),
        SystemVersionStatus.ARCHIVED: set(),
    }

    def assert_transition(self, current: SystemVersionStatus, target: SystemVersionStatus) -> None:
        if target not in self.ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"Transition from {current.value} to {target.value} is not allowed")


class CandidatePromotionFlow:
    """Promotion and rollback flow built on top of SystemRegistry."""

    def __init__(self, registry: SystemRegistry, *, state_machine: LifecycleStateMachine | None = None) -> None:
        self.registry = registry
        self.state_machine = state_machine or LifecycleStateMachine()

    def send_to_canary(self, system_id: str, version: str, *, actor_role: str = "human_reviewer") -> SystemReleaseState:
        current = self.registry.get_version(system_id, version)
        self.state_machine.assert_transition(current.status, SystemVersionStatus.CANARY)
        return self.registry.promote_channel(system_id, "canary", version, actor_role=actor_role)

    def promote_to_stable(
        self,
        system_id: str,
        version: str,
        *,
        actor_role: str = "human_reviewer",
        expected_current: str | None = None,
    ) -> SystemReleaseState:
        current = self.registry.get_version(system_id, version)
        self.state_machine.assert_transition(current.status, SystemVersionStatus.STABLE)
        return self.registry.promote_channel(
            system_id,
            "stable",
            version,
            actor_role=actor_role,
            expected_current=expected_current,
        )

    def reject(self, system_id: str, version: str) -> SystemVersionRecord:
        current = self.registry.get_version(system_id, version)
        self.state_machine.assert_transition(current.status, SystemVersionStatus.REJECTED)
        return self.registry.set_version_status(system_id, version, SystemVersionStatus.REJECTED)

    def archive(self, system_id: str, version: str) -> SystemVersionRecord:
        current = self.registry.get_version(system_id, version)
        self.state_machine.assert_transition(current.status, SystemVersionStatus.ARCHIVED)
        return self.registry.set_version_status(system_id, version, SystemVersionStatus.ARCHIVED)

    def rollback_stable(self, system_id: str, rollback_to_version: str, *, actor_role: str = "human_reviewer") -> SystemReleaseState:
        return self.registry.rollback_channel(system_id, "stable", rollback_to_version, actor_role=actor_role)
