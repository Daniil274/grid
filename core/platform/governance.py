"""Backwards-compatible structured import for platform governance."""

from core.system_governance import (
    BudgetExceededError,
    BudgetTracker,
    PermissionChecker,
    PermissionDeniedError,
)

__all__ = [
    "BudgetExceededError",
    "BudgetTracker",
    "PermissionChecker",
    "PermissionDeniedError",
]
