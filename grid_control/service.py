"""Small authenticated broker: agents submit commits, never host commands.

Two callers, two tokens:

- the workshop (``GRID_CONTROL_TOKEN``): the stable source, experiments,
  trials, scenarios, and the tasks waiting for it;
- the operator's side (``GRID_CONTROL_TASK_TOKEN``, optional): sends tasks -
  proposals from the web chat's reviews - and follows what became of them.

The workshop cannot send tasks and the operator's side cannot run experiments:
each token opens only its own routes.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import asdict
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import uuid

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from .controller import Controller
from .models import MAX_OWN_SCENARIOS, Policy, Scenario
from .repository import MAX_BUNDLE_BYTES, Repository


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    candidate: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    # Base64 git bundle with the candidate's new commits, for a workshop that
    # has its own clone. Without it the candidate must already be in the repository.
    bundle: str | None = Field(default=None, max_length=(MAX_BUNDLE_BYTES // 3 + 1) * 4)
    # Development trials only: how many times to run the open scenarios, and
    # scenarios of the workshop's own (for instance a user's exact message).
    repetitions: int = Field(default=1, ge=1, le=20)
    scenarios: list[dict] = Field(default_factory=list, max_length=MAX_OWN_SCENARIOS)


class TaskRequest(BaseModel):
    """A change the operator's side asks the workshop to make and evaluate."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=20_000)
    cause: str = Field(default="", max_length=64)
    evidence: list[str] = Field(default_factory=list, max_length=50)
    change: str = Field(default="", max_length=200_000)
    scenario: str = Field(default="", max_length=20_000)
    # Where it came from, for the operator: a review id, never user data.
    source: dict[str, str] = Field(default_factory=dict, max_length=10)


class TaskLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment: str = Field(pattern=r"^[a-f0-9]{32}$")


def _bearer(token: str, name: str):
    """A dependency passing requests that carry *token*."""

    def authenticate(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            raise HTTPException(status_code=401, detail=f"Invalid {name}")

    return authenticate


def _without_details(event: dict) -> dict:
    trial = event.get("trial")
    if not isinstance(trial, dict):
        return event
    return {**event, "trial": {k: v for k, v in trial.items() if k != "details"}}


def create_app(
    controller: Controller, repository: Path, policy: Policy, token: str, task_token: str | None = None
) -> FastAPI:
    """*token* is the workshop's; *task_token*, when given, opens the task
    routes of the operator's side."""
    if len(token) < 32:
        raise ValueError("GRID_CONTROL_TOKEN must contain at least 32 characters")
    if task_token is not None and (len(task_token) < 32 or task_token == token):
        raise ValueError("GRID_CONTROL_TASK_TOKEN must contain at least 32 characters and differ from GRID_CONTROL_TOKEN")
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

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    workshop = APIRouter(dependencies=[Depends(_bearer(token, "controller token"))])

    @workshop.get("/source")
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
                if body.scenarios and kind != "trial":
                    raise ValueError("Only a development trial takes scenarios of its own")
                own = tuple(Scenario.from_dict(item) for item in body.scenarios)
                experiment = controller.prepare(
                    repository, body.baseline, body.candidate, policy,
                    kind=kind, repetitions=body.repetitions, scenarios=own,
                )
            except (binascii.Error, ValueError, TypeError, RuntimeError) as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            executor.submit(controller.run, experiment)
        return {"id": experiment, "kind": kind, "status": "queued"}

    @workshop.post("/experiments", status_code=202)
    def submit(body: Submission):
        """Queue an acceptance experiment: baseline against candidate."""
        return admit(body, "experiment")

    @workshop.post("/trials", status_code=202)
    def trial(body: Submission):
        """Queue a development trial: the candidate alone on the open scenarios."""
        return admit(body, "trial")

    @workshop.get("/scenarios")
    def scenarios():
        """The open development scenarios. Acceptance scenarios stay private."""
        return [asdict(scenario) for scenario in policy.dev_scenarios]

    @workshop.get("/experiments/{experiment}")
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

    @workshop.get("/tasks")
    def open_tasks():
        """Tasks waiting for the workshop, oldest first."""
        return controller.store.open_tasks()

    @workshop.post("/tasks/{task}/take")
    def take_task(task: str):
        """Claim an open task: nobody else gets it. Returns the task."""
        try:
            controller.store.take_task(task)
            return controller.store.task(task)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @workshop.post("/tasks/{task}/link")
    def link_task(task: str, body: TaskLink):
        """Record the experiment a taken task became."""
        try:
            controller.store.get(body.experiment)
            controller.store.link_task(task, body.experiment)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"id": task, "experiment": body.experiment}

    app.include_router(workshop)

    if task_token is not None:
        operator = APIRouter(dependencies=[Depends(_bearer(task_token, "task token"))])

        @operator.post("/tasks", status_code=201)
        def send_task(body: TaskRequest):
            """Queue a task for the workshop."""
            task = uuid.uuid4().hex
            controller.store.add_task(task, body.model_dump())
            return {"id": task, "status": "open"}

        @operator.get("/tasks/{task}")
        def task_status(task: str):
            """What became of a task: open, taken, and its experiment's verdict."""
            try:
                found = controller.store.task(task)
            except ValueError as error:
                raise HTTPException(status_code=404, detail="Unknown task") from error
            verdict = controller.store.get(found["experiment"])["status"] if found["experiment"] else None
            return {"id": task, "status": found["status"], "experiment": found["experiment"], "verdict": verdict}

        app.include_router(operator)

    return app
