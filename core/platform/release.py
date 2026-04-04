"""Backwards-compatible structured import for platform release flow."""

from core.system_release_manager import CandidatePromotionFlow, LifecycleStateMachine

__all__ = ["CandidatePromotionFlow", "LifecycleStateMachine"]
