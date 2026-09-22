"""Narrow administrator client. Never exposes Docker or host commands to agents."""

from __future__ import annotations

import os
import re

import httpx
from agents import function_tool


async def _request(method: str, path: str, body: dict | None = None) -> dict:
    endpoint = os.environ.get("GRID_CONTROL_URL", "").rstrip("/")
    token = os.environ.get("GRID_CONTROL_TOKEN", "")
    if not endpoint or not token:
        return {
            "error": "Administrator requires GRID_CONTROL_URL and GRID_CONTROL_TOKEN"
        }
    async with httpx.AsyncClient(
        timeout=90, trust_env=False, follow_redirects=False
    ) as client:
        try:
            response = await client.request(
                method,
                endpoint + path,
                json=body,
                headers={"Authorization": "Bearer " + token},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as error:
            return {"error": str(error)}


@function_tool
async def control_submit(baseline: str, candidate: str) -> dict:
    """Submit two committed SHA revisions to the independent evaluator. Returns an experiment ID, not a verdict."""
    if not all(
        re.fullmatch(r"[a-f0-9]{40,64}", value) for value in (baseline, candidate)
    ):
        return {"error": "baseline and candidate must be full commit SHAs"}
    return await _request(
        "POST", "/experiments", {"baseline": baseline, "candidate": candidate}
    )


@function_tool
async def control_status(experiment_id: str) -> dict:
    """Read durable evaluation evidence. accepted is a tested artifact, not a deployment."""
    if not re.fullmatch(r"[a-f0-9]{32}", experiment_id):
        return {"error": "Invalid experiment ID"}
    return await _request("GET", "/experiments/" + experiment_id)


CONTROL_TOOLS = {"control_submit": control_submit, "control_status": control_status}
