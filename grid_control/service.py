"""Small authenticated broker: agents submit commits, never host commands."""

from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import asdict
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .controller import Controller
from .models import Policy
from .repository import MAX_BUNDLE_BYTES, Repository


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    candidate: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    # Base64 git bundle with the candidate's new commits, for a workshop that
    # has its own clone. Without it the candidate must already be in the repository.
    bundle: str | None = Field(default=None, max_length=(MAX_BUNDLE_BYTES // 3 + 1) * 4)


def _without_details(event: dict) -> dict:
    trial = event.get("trial")
    if not isinstance(trial, dict):
        return event
    return {**event, "trial": {k: v for k, v in trial.items() if k != "details"}}


def create_app(
    controller: Controller, repository: Path, policy: Policy, token: str
) -> FastAPI:
    if len(token) < 32:
        raise ValueError("GRID_CONTROL_TOKEN must contain at least 32 characters")
    admission = threading.Lock()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grid-control")

    @asynccontextmanager
    async def lifespan(app):
        # Only one broker may own this journal. Per-experiment locks also protect
        # against concurrent operator CLI commands.
        with controller.store.lease("0" * 32):
            for experiment in controller.store.pending("running"):
                with controller.store.lease(experiment):
                    controller.runtime.cleanup(experiment)
                    controller.store.finish(
                        experiment,
                        "failed",
                        {"error": "Controller restarted during evaluation"},
                    )
            for experiment in controller.store.pending("queued"):
                executor.submit(controller.run, experiment)
            try:
                yield
            finally:
                executor.shutdown(wait=True)

    def authenticate(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(
            authorization.encode(), ("Bearer " + token).encode()
        ):
            raise HTTPException(status_code=401, detail="Invalid controller token")

    app = FastAPI(
        lifespan=lifespan,
        dependencies=[Depends(authenticate)],
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/source")
    def source():
        """Git bundle of stable: the starting point for a workshop's clone."""
        return Response(
            Repository(repository).export(), media_type="application/x-git-bundle"
        )

    def admit(body: Submission, kind: str) -> dict:
        with admission:
            if (
                len(controller.store.pending("queued"))
                + len(controller.store.pending("running"))
                >= 4
            ):
                raise HTTPException(status_code=429, detail="Experiment queue is full")
            try:
                if body.bundle is not None:
                    bundle = base64.b64decode(body.bundle, validate=True)
                    Repository(repository).receive(bundle, body.baseline, body.candidate)
                experiment = controller.prepare(
                    repository, body.baseline, body.candidate, policy, kind=kind
                )
            except (binascii.Error, ValueError, RuntimeError) as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            executor.submit(controller.run, experiment)
        return {"id": experiment, "kind": kind, "status": "queued"}

    @app.post("/experiments", status_code=202)
    def submit(body: Submission):
        """Queue an acceptance experiment: baseline against candidate."""
        return admit(body, "experiment")

    @app.post("/trials", status_code=202)
    def trial(body: Submission):
        """Queue a development trial: the candidate alone on the open scenarios."""
        return admit(body, "trial")

    @app.get("/scenarios")
    def scenarios():
        """The open development scenarios. Acceptance scenarios stay private."""
        return [asdict(scenario) for scenario in policy.dev_scenarios]

    @app.get("/experiments/{experiment}")
    def get(experiment: str):
        try:
            report = controller.store.get(experiment)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="Unknown experiment") from error
        # The administrator needs evidence, not controller host paths or policy.
        view = {
            key: report.get(key)
            for key in ("id", "kind", "status", "baseline", "candidate", "policy_sha256")
        }
        if report.get("kind") == "trial":
            view["events"] = report["events"]
        else:
            # Acceptance scenarios are private: their verdicts leave, the
            # explanations that would reveal the expectations do not.
            view["events"] = [_without_details(event) for event in report["events"]]
        return view

    return app
