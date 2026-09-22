"""Experiment tools of the administrator system. Never expose Docker or host commands.

The agent works in a workshop clone (see ``core/workshop.py``): ``control_begin``
opens an experiment on the controller's current stable, ``control_submit``
commits the changes and queues the candidate for evaluation, ``control_status``
reads the verdict. The agent never handles commit SHAs itself, and all three
refuse to run in a repository that is not a workshop.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Callable, Dict

from agents import function_tool

from core.workshop import ControlClient, Workshop, WorkshopError
from utils.path_utils import resolve_agent_path_auto


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
async def control_status(experiment_id: str) -> dict:
    """Read an experiment's status and evidence. accepted means tested, not deployed."""
    if not re.fullmatch(r"[a-f0-9]{32}", experiment_id):
        return {"error": "Invalid experiment ID: use the id returned by control_submit"}
    return await _run(lambda workshop, client: client.status(experiment_id))


CONTROL_TOOLS = {
    "control_begin": control_begin,
    "control_submit": control_submit,
    "control_status": control_status,
}
