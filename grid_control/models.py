"""Immutable contracts shared by the controller and execution adapters."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

SCENARIO_PREFIX = "scenario:"
_HOST = re.compile(r"(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


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
class Scenario:
    """One user message sent to the candidate's chat, with objective expectations.

    Every expectation is deterministic: where the router sent the message,
    which tools ran and what the answer matches. No model judges the result.
    """

    name: str
    message: str
    system: str | None = None
    agent: str | None = None
    tools_called: tuple[str, ...] = ()
    tools_not_called: tuple[str, ...] = ()
    output_matches: str | None = None
    timeout_seconds: int = 180

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", self.name):
            raise ValueError(f"Invalid scenario name: {self.name!r}")
        if not self.message.strip():
            raise ValueError(f"Scenario {self.name} has no message")
        if not 1 <= self.timeout_seconds <= 1800:
            raise ValueError("Scenario timeouts must be between 1 and 1800 seconds")
        if self.output_matches is not None:
            re.compile(self.output_matches)
        if not (
            self.system or self.agent or self.tools_called
            or self.tools_not_called or self.output_matches
        ):
            raise ValueError(f"Scenario {self.name} expects nothing")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Scenario:
        data = dict(value)
        for key in ("tools_called", "tools_not_called"):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)


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
    # Acceptance scenarios: hidden from the workshop, only their verdicts leave.
    scenarios: tuple[Scenario, ...] = ()
    # Development scenarios: the workshop runs them on demand and sees details.
    dev_scenarios: tuple[Scenario, ...] = ()
    # Hosts the candidate may reach over HTTPS through the controller's proxy.
    egress_hosts: tuple[str, ...] = ()
    # Share of repetitions a scenario must pass. 1.0 demands every repetition;
    # a model-driven router misroutes borderline messages now and then, so with
    # several repetitions a majority (e.g. 0.66 of 3) separates noise from a
    # regression. HTTP checks are deterministic and always need every repetition.
    scenario_pass_rate: float = 1.0

    def __post_init__(self) -> None:
        if not self.runtime_image or not self.verifier_image:
            raise ValueError("Runtime and verifier images are required")
        if not self.checks and not self.scenarios:
            raise ValueError("At least one check or scenario is required")
        if len({check.name for check in self.checks}) != len(self.checks):
            raise ValueError("Check names must be unique")
        for suite in (self.scenarios, self.dev_scenarios):
            if len({scenario.name for scenario in suite}) != len(suite):
                raise ValueError("Scenario names must be unique")
        if any(not _HOST.fullmatch(host) for host in self.egress_hosts):
            raise ValueError("Egress hosts must be lowercase DNS names, optionally *.suffix")
        if not 1 <= self.repetitions <= 20:
            raise ValueError("Repetitions must be between 1 and 20")
        if (
            not 1 <= self.timeout_seconds <= 3600
            or not 1 <= self.build_timeout_seconds <= 3600
        ):
            raise ValueError("Timeouts must be between 1 and 3600 seconds")
        if not 0 <= self.min_improvement <= 1:
            raise ValueError("min_improvement must be a fraction between 0 and 1")
        if not 0.5 < self.scenario_pass_rate <= 1:
            raise ValueError("scenario_pass_rate must be above 0.5 and at most 1")
        if any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            for name in self.environment_names
        ):
            raise ValueError("Invalid environment variable name")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Policy:
        data = dict(value)
        data["checks"] = tuple(Check(**check) for check in data.get("checks", ()))
        data["environment_names"] = tuple(data.get("environment_names", ()))
        data["egress_hosts"] = tuple(data.get("egress_hosts", ()))
        for key in ("scenarios", "dev_scenarios"):
            data[key] = tuple(Scenario.from_dict(item) for item in data.get(key, ()))
        return cls(**data)

    def result_names(self) -> set[str]:
        """Keys a verifier must report: every check and every acceptance scenario."""
        return {check.name for check in self.checks} | {
            SCENARIO_PREFIX + scenario.name for scenario in self.scenarios
        }

    def development(self) -> Policy:
        """The policy of a development trial: one run of the open scenarios."""
        if not self.dev_scenarios:
            raise ValueError("The operator has not configured development scenarios")
        return replace(self, scenarios=self.dev_scenarios, repetitions=1, auto_promote=False)

    def verifier_timeout(self) -> int:
        """Time for the whole verifier run: startup and checks, then every scenario."""
        return self.timeout_seconds + sum(s.timeout_seconds for s in self.scenarios)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Trial:
    checks: dict[str, bool]
    elapsed_seconds: float
    # Why each result came out as it did (routing, tools, answer excerpt).
    details: dict[str, str] = field(default_factory=dict)


def decide(
    policy: Policy, baseline: list[Trial], candidate: list[Trial]
) -> dict[str, Any]:
    """Fail closed on missing, malformed or regressed results, never on LLM votes."""
    names = policy.result_names()
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
    rates = {
        name: sum(trial.checks[name] for trial in candidate) / policy.repetitions
        for name in names
    }
    failing = sorted(
        name for name, rate in rates.items()
        if rate < (policy.scenario_pass_rate if name.startswith(SCENARIO_PREFIX) else 1.0)
    )
    rule = (
        "All candidate checks must pass in every repetition"
        if policy.scenario_pass_rate == 1.0
        else f"HTTP checks must pass in every repetition and scenarios in at least "
        f"{policy.scenario_pass_rate:.0%} of them"
    )
    return {
        "accepted": not failing and new - old >= policy.min_improvement,
        "baseline_success": old,
        "candidate_success": new,
        "improvement": new - old,
        "failing": failing,
        "reason": rule + " and meet the improvement threshold",
    }
