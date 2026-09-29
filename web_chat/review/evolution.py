"""What becomes of a proposal: does its patch apply, and the evolution loop.

A proposal is a review agent's hypothesis, never applied here. The admin can

- check it: ``git apply --check`` against the code this server runs - nothing
  is changed, the answer is whether the diff still fits;
- send it to the evolution loop: a task in the controller's inbox
  (grid_control, ``POST /tasks``) that the workshop's administrator takes,
  turns into an experiment and submits for evaluation; the operator promotes
  what passes. The task carries the proposal - title, cause, summary,
  evidence references, diff, scenario - and the review's id, not the user's
  conversation.

The web server reaches the controller with its own token
(``GRID_CONTROL_TASK_TOKEN``), which opens only the task routes.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Optional

import httpx

from web_chat.review.evidence import PROJECT_ROOT


def check_patch(change: str) -> dict[str, Any]:
    """Whether *change* applies to the code this server runs; nothing is changed."""
    if not change.strip():
        return {"applies": None, "detail": "The proposal has no change."}
    patch = change if change.endswith("\n") else change + "\n"
    try:
        # Bytes, not text: text mode would turn the patch's \n into \r\n on
        # Windows, and no line would match.
        done = subprocess.run(
            ["git", "apply", "--check", "--verbose", "-"],
            cwd=PROJECT_ROOT, input=patch.encode("utf-8"), capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"applies": None, "detail": f"git is not available here: {exc}"}
    detail = (done.stderr or done.stdout).decode("utf-8", errors="replace").strip()
    return {"applies": done.returncode == 0, "detail": detail}


class EvolutionError(RuntimeError):
    """The controller refused a task or could not be reached."""


class EvolutionOutbox:
    """Sends tasks to the controller and reads what became of them."""

    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    @classmethod
    def from_env(cls) -> Optional["EvolutionOutbox"]:
        """The outbox of GRID_CONTROL_URL and GRID_CONTROL_TASK_TOKEN; None without them."""
        url = os.environ.get("GRID_CONTROL_URL", "").rstrip("/")
        token = os.environ.get("GRID_CONTROL_TASK_TOKEN", "")
        if not url or not token:
            return None
        return cls(httpx.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            trust_env=False,
            follow_redirects=False,
        ))

    def send(self, review_id: str, proposal: dict[str, Any]) -> str:
        """Queue *proposal* of review *review_id* as a task; its id."""
        task = {
            "title": proposal.get("title", ""),
            "summary": proposal.get("summary", ""),
            "cause": proposal.get("cause", ""),
            "evidence": list(proposal.get("evidence") or []),
            "change": proposal.get("change", ""),
            "scenario": proposal.get("scenario", ""),
            "source": {"review": review_id, "proposal": proposal.get("id", "")},
        }
        return self._call("POST", "/tasks", json=task)["id"]

    def status(self, task_id: str) -> dict[str, Any]:
        """open or taken, the experiment it became and that experiment's verdict."""
        return self._call("GET", f"/tasks/{task_id}")

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise EvolutionError(f"The controller is unreachable: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise EvolutionError(f"The controller refused ({response.status_code}): {detail}")
        return response.json()
