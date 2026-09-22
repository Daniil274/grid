"""Experiment orchestration, with no imports from the code under evaluation."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from .models import Policy, Trial, decide
from .repository import Repository
from .store import Store


class Runtime(Protocol):
    def revision(self, repo: Path, ref: str) -> str: ...
    def image(self, ref: str) -> str: ...
    def build(
        self, repo: Path, commit: str, runtime: str, experiment: str, timeout: int
    ) -> str: ...
    def trial(
        self, image: str, verifier: str, experiment: str, index: int, policy: Policy
    ) -> Trial: ...
    def cleanup(self, experiment: str) -> None: ...


class Controller:
    def __init__(self, store: Store, runtime: Runtime):
        self.store = store
        self.runtime = runtime

    def evaluate(
        self, repo: Path, baseline: str, candidate: str, policy: Policy
    ) -> dict:
        return self.run(self.prepare(repo, baseline, candidate, policy))

    def prepare(self, repo: Path, baseline: str, candidate: str, policy: Policy) -> str:
        experiment = uuid.uuid4().hex
        old = self.runtime.revision(repo, baseline)
        new = self.runtime.revision(repo, candidate)
        if old == new:
            raise ValueError("Baseline and candidate must be different commits")
        runtime = self.runtime.image(policy.runtime_image)
        verifier = self.runtime.image(policy.verifier_image)
        self.store.create(
            experiment,
            {
                "repository": str(repo.resolve()),
                "baseline": old,
                "candidate": new,
                "policy": asdict(policy),
                "policy_sha256": policy.digest,
                "runtime_image": runtime,
                "verifier_image": verifier,
            },
            status="queued",
        )
        return experiment

    def run(self, experiment: str) -> dict:
        with self.store.lease(experiment):
            record = self.store.get(experiment)
            self.store.start(experiment)
            report = self._run(experiment, record)
            if report["status"] == "accepted" and Policy.from_dict(report["policy"]).auto_promote:
                try:
                    self._promote(experiment, report, by="policy")
                except (ValueError, RuntimeError) as error:
                    self.store.event(experiment, {"promotion_error": str(error)})
                report = self.store.get(experiment)
            return report

    def promote(self, experiment: str, *, by: str = "operator") -> dict:
        """Move stable to an accepted candidate; refuses when stable moved meanwhile."""
        with self.store.lease(experiment):
            self._promote(experiment, self.store.get(experiment), by=by)
            return self.store.get(experiment)

    def _promote(self, experiment: str, record: dict, *, by: str) -> None:
        if record["status"] != "accepted":
            raise ValueError("Only an accepted experiment can be promoted")
        Repository(Path(record["repository"])).promote(record["baseline"], record["candidate"])
        self.store.promote(experiment, {"promoted_by": by, "stable": record["candidate"]})

    def _run(self, experiment: str, record: dict) -> dict:
        repo = Path(record["repository"])
        old, new = record["baseline"], record["candidate"]
        runtime, verifier = record["runtime_image"], record["verifier_image"]
        policy = Policy.from_dict(record["policy"])
        result = {}
        status = "failed"
        try:
            images = {}
            for role, commit in (("baseline", old), ("candidate", new)):
                images[role] = self.runtime.build(
                    repo, commit, runtime, experiment, policy.build_timeout_seconds
                )
                self.store.event(experiment, {"built": role, "image": images[role]})
            trials: dict[str, list[Trial]] = {"baseline": [], "candidate": []}
            for repetition in range(policy.repetitions):
                # Alternate order to reduce time/order bias. Every run gets fresh data.
                roles = (
                    ("baseline", "candidate")
                    if repetition % 2 == 0
                    else ("candidate", "baseline")
                )
                for offset, role in enumerate(roles):
                    trial = self.runtime.trial(
                        images[role],
                        verifier,
                        experiment,
                        repetition * 2 + offset,
                        policy,
                    )
                    trials[role].append(trial)
                    self.store.event(
                        experiment,
                        {
                            "role": role,
                            "repetition": repetition,
                            "trial": asdict(trial),
                        },
                    )
                    self.runtime.cleanup(experiment)
            result = {
                "decision": decide(policy, trials["baseline"], trials["candidate"]),
                "images": images,
            }
            status = "accepted" if result["decision"]["accepted"] else "rejected"
        except Exception as error:
            result = {"error": str(error), "error_type": type(error).__name__}
        finally:
            try:
                self.runtime.cleanup(experiment)
            except Exception as error:
                result["cleanup_error"] = str(error)
                status = "failed"
            self.store.finish(experiment, status, result)
        return self.store.get(experiment)
