"""Permissions, budgets, and governance helpers for system platform."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Dict, Optional

from schemas.system_platform import BudgetPolicy, GovernanceDecision, PermissionPolicy


class PermissionDeniedError(PermissionError):
    """Raised when an actor role cannot perform an action."""


class BudgetExceededError(ValueError):
    """Raised when budget limits are exceeded."""


class PermissionChecker:
    """Capability-based permission checker."""

    def assert_allowed(self, policy: PermissionPolicy, role: str, action: str) -> None:
        role_policy = policy.roles.get(role)
        if role_policy is None:
            raise PermissionDeniedError(f"Role '{role}' is not configured")
        if "*" in role_policy.deny or action in role_policy.deny:
            raise PermissionDeniedError(f"Role '{role}' is explicitly denied action '{action}'")
        if "*" in role_policy.allow or action in role_policy.allow:
            return
        raise PermissionDeniedError(f"Role '{role}' is not allowed to perform '{action}'")


class BudgetTracker:
    """In-memory budget tracker suitable for initial platform foundation."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._window_key = self._current_window_key()
        self._new_systems = 0
        self._promotions = 0
        self._active_canaries = 0
        self._candidate_versions: Dict[str, int] = {}

    def assert_can_create_system(self, policy: BudgetPolicy) -> None:
        with self._lock:
            self._reset_if_needed(policy)
            if self._new_systems >= policy.max_new_systems_per_window:
                raise BudgetExceededError("Maximum number of new systems per budget window reached")

    def record_system_created(self) -> None:
        with self._lock:
            self._new_systems += 1

    def assert_can_create_candidate(self, policy: BudgetPolicy, system_id: str) -> None:
        with self._lock:
            self._reset_if_needed(policy)
            count = self._candidate_versions.get(system_id, 0)
            if count >= policy.max_candidate_versions_per_system:
                raise BudgetExceededError(
                    f"Maximum number of candidate versions reached for system '{system_id}'"
                )

    def record_candidate_created(self, system_id: str) -> None:
        with self._lock:
            self._candidate_versions[system_id] = self._candidate_versions.get(system_id, 0) + 1

    def record_candidate_closed(self, system_id: str) -> None:
        with self._lock:
            if system_id in self._candidate_versions:
                self._candidate_versions[system_id] = max(0, self._candidate_versions[system_id] - 1)

    def assert_can_start_canary(self, policy: BudgetPolicy) -> None:
        with self._lock:
            self._reset_if_needed(policy)
            if self._active_canaries >= policy.max_active_canaries:
                raise BudgetExceededError("Maximum number of active canaries reached")

    def record_canary_started(self) -> None:
        with self._lock:
            self._active_canaries += 1

    def record_canary_finished(self) -> None:
        with self._lock:
            self._active_canaries = max(0, self._active_canaries - 1)

    def assert_can_promote(self, policy: BudgetPolicy) -> None:
        with self._lock:
            self._reset_if_needed(policy)
            if self._promotions >= policy.max_promotions_per_window:
                raise BudgetExceededError("Maximum number of promotions per budget window reached")

    def record_promoted(self) -> None:
        with self._lock:
            self._promotions += 1

    def make_decision(
        self,
        *,
        subject: str,
        decision: str,
        rationale: list[str],
        inputs: Optional[dict] = None,
        next_action: Optional[list[str]] = None,
    ) -> GovernanceDecision:
        return GovernanceDecision(
            decision_id=f"gov-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
            subject=subject,
            decision=decision,
            rationale=rationale,
            inputs=inputs or {},
            next_action=next_action or [],
        )

    def _current_window_key(self) -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    def _reset_if_needed(self, policy: BudgetPolicy) -> None:
        if policy.reset_window != "daily":
            return
        current = self._current_window_key()
        if current == self._window_key:
            return
        self._window_key = current
        self._new_systems = 0
        self._promotions = 0
        self._active_canaries = 0
        self._candidate_versions = {}
