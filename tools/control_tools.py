"""Experiment tools of the administrator system. Never expose Docker or host commands.

The agent works in a workshop clone (see ``core/workshop.py``): ``control_begin``
opens an experiment on the controller's current stable, ``control_trial`` tries
unfinished work on the open development scenarios, ``control_submit`` commits
the changes and queues the candidate for evaluation, ``control_status`` reads
the result. The agent never handles commit SHAs itself, and all three
refuse to run in a repository that is not a workshop.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict

from agents import function_tool

from core.workshop import ControlClient, Workshop, WorkshopError
from utils.path_utils import resolve_agent_path_auto

PENDING = {"queued", "running"}
MAX_WAIT_SECONDS = 900
POLL_SECONDS = 10


async def _run(action: Callable[[Workshop, ControlClient], Dict[str, Any]]) -> Dict[str, Any]:
    workshop = Workshop(Path(resolve_agent_path_auto(".")))
    try:
        client = ControlClient.from_env()
        return await asyncio.to_thread(action, workshop, client)
    except WorkshopError as error:
        return {"error": str(error)}


@function_tool
async def control_begin() -> dict:
    """Open an experiment: reset the workshop to the controller's current stable commit.

    Call it once before any change. Returns the baseline commit that the
    candidate will be compared with. Refuses when unsubmitted changes exist.
    Local to the workshop: changes nothing outside it.
    """
    return await _run(lambda workshop, client: {"baseline": workshop.begin(client)})


@function_tool
async def control_submit(message: str) -> dict:
    """Commit all changes of the open experiment and queue the candidate for evaluation.

    The controller evaluates it in its own sandboxed containers and never deploys
    it by itself: an accepted candidate waits for the operator's promotion.
    Returns the experiment ID, the baseline and the candidate commit.

    Args:
        message: Commit message: what changed and why, first line under 72 characters.
    """
    return await _run(lambda workshop, client: workshop.submit(client, message))


@function_tool
async def control_trial() -> dict:
    """Try the current work on the open development scenarios, before submitting.

    Sends a snapshot of every change (committed or not) to a one-off run in the
    controller's sandbox; the experiment itself stays open and unchanged. The
    result (read it with control_status) shows, per scenario, where the message
    was routed, which tools ran and the start of the answer. It decides nothing.
    """
    return await _run(lambda workshop, client: workshop.trial(client))


@function_tool
async def control_scenarios() -> dict:
    """List the open development scenarios: messages and what each one expects.

    Acceptance uses a separate private suite, so fix the cause of a failure
    rather than tailoring the system to these exact messages.
    """
    return await _run(lambda workshop, client: {"scenarios": client.scenarios()})


@function_tool
async def control_status(experiment_id: str, wait_seconds: int = 0) -> dict:
    """Read the status and evidence of an experiment or a development trial.

    accepted means tested, not deployed; completed means a trial has finished.

    Args:
        experiment_id: The id returned by control_submit or control_trial.
        wait_seconds: Wait up to this long (max 900) for a queued or running
            one to finish, instead of calling again.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", experiment_id):
        return {"error": "Invalid experiment ID: use the id returned by control_submit or control_trial"}
    deadline = time.monotonic() + max(0, min(wait_seconds, MAX_WAIT_SECONDS))
    while True:
        report = await _run(lambda workshop, client: client.status(experiment_id))
        if report.get("status") not in PENDING or time.monotonic() >= deadline:
            return report
        await asyncio.sleep(POLL_SECONDS)


CONTROL_TOOLS = {
    "control_begin": control_begin,
    "control_trial": control_trial,
    "control_scenarios": control_scenarios,
    "control_submit": control_submit,
    "control_status": control_status,
}
