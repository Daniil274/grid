"""Immutable contracts shared by the controller and execution adapters."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Check:
    name: str
    path: str
    expected_status: int = 200
    json_pointer: str | None = None
    expected: Any = None

    def __post_init__(self) -> None:
        if not self.name or not self.path.startswith("/") or self.path.startswith("//"):
            raise ValueError("Checks require a name and a relative HTTP path")
        if not 100 <= self.expected_status <= 599:
            raise ValueError("Invalid HTTP status")
        if (
            self.json_pointer is not None
            and self.json_pointer
            and not self.json_pointer.startswith("/")
        ):
            raise ValueError("JSON pointers must be empty or start with /")


@dataclass(frozen=True)
class Policy:
    runtime_image: str
    verifier_image: str
    checks: tuple[Check, ...]
    repetitions: int = 3
    timeout_seconds: int = 180
    build_timeout_seconds: int = 300
    min_improvement: float = 0.0
    allow_network: bool = False
    environment_names: tuple[str, ...] = ()
    # Promote an accepted candidate to stable without waiting for the operator.
    auto_promote: bool = False

    def __post_init__(self) -> None:
        if not self.runtime_image or not self.verifier_image or not self.checks:
            raise ValueError("Runtime, verifier and at least one check are required")
        if len({check.name for check in self.checks}) != len(self.checks):
            raise ValueError("Check names must be unique")
        if not 1 <= self.repetitions <= 20:
            raise ValueError("Repetitions must be between 1 and 20")
        if (
            not 1 <= self.timeout_seconds <= 3600
            or not 1 <= self.build_timeout_seconds <= 3600
        ):
            raise ValueError("Timeouts must be between 1 and 3600 seconds")
        if not 0 <= self.min_improvement <= 1:
            raise ValueError("min_improvement must be a fraction between 0 and 1")
        import re

        if any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            for name in self.environment_names
        ):
            raise ValueError("Invalid environment variable name")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Policy:
        data = dict(value)
        data["checks"] = tuple(Check(**check) for check in data["checks"])
        data["environment_names"] = tuple(data.get("environment_names", ()))
        return cls(**data)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Trial:
    checks: dict[str, bool]
    elapsed_seconds: float


def decide(
    policy: Policy, baseline: list[Trial], candidate: list[Trial]
) -> dict[str, Any]:
    """Fail closed on missing, malformed or regressed results, never on LLM votes."""
    names = {check.name for check in policy.checks}
    for trials in (baseline, candidate):
        if len(trials) != policy.repetitions:
            raise ValueError("Incomplete evaluation")
        for trial in trials:
            if set(trial.checks) != names or any(
                type(v) is not bool for v in trial.checks.values()
            ):
                raise ValueError("Verifier returned an invalid check set")
    size = policy.repetitions * len(names)
    old = sum(sum(trial.checks.values()) for trial in baseline) / size
    new = sum(sum(trial.checks.values()) for trial in candidate) / size
    passed = all(all(trial.checks.values()) for trial in candidate)
    return {
        "accepted": passed and new - old >= policy.min_improvement,
        "baseline_success": old,
        "candidate_success": new,
        "improvement": new - old,
        "reason": "All candidate checks must pass in every repetition and meet the improvement threshold",
    }
