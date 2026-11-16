"""The tracker's mutations as plain coroutines, for callers that are not tools.

``tools.beads_tools`` exposes the same writes to the model, but an agent tool
is a particular transport: its entry point is ``on_invoke_tool`` with a
``RunContextWrapper``, and its answer is text meant for a model. A backend
that serves GRID's own UI needs the same writes with the same guarantees -
the operation journal, the markers, reconciliation, the task bindings - and
no second copy of that logic. This module is that entry point: the functions
below are the very coroutines the ``@function_tool`` wrappers call, without
the agent framework around them.

What a caller passes
--------------------

``context`` is the host object the write runs for. Anything with a ``context``
attribute carrying ``user_id``, ``run_id``, ``agent_id``, ``context_id`` and
``container_id`` works - an HTTP handler can build a ``SimpleNamespace`` with
exactly those fields, and the write is journaled under that actor and bound
to that run. The tools pass their ``RunContextWrapper``, which is the same
shape: this module imports no agent machinery.

What a caller gets
------------------

A :class:`Receipt`: the text the tool would have returned, and, when that
text is JSON, its parsed payload - so a backend can read ``isError``,
``committed``, ``operation_id`` and the ids without parsing a sentence. The
payload is produced by the same code that produces the tool's answer, so the
two can never disagree.

Repeats and lost answers are not special here: pass the caller's
``request_id`` and the journal answers a repeat (``duplicate``) or reports
``committed: null`` honestly instead of repeating a write nobody can prove.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Receipt:
    """One mutation's answer: the tool's text and, if JSON, its payload."""

    text: str
    payload: Optional[Dict[str, Any]] = None

    @property
    def is_error(self) -> bool:
        return bool(self.payload and self.payload.get("isError"))

    @property
    def committed(self) -> Optional[bool]:
        """Yes, no, or - honestly - nobody can tell yet."""
        if not self.payload:
            return None
        return self.payload.get("committed")

    @property
    def operation_id(self) -> Optional[str]:
        return (self.payload or {}).get("operation_id")

    @property
    def data(self) -> Dict[str, Any]:
        return dict(self.payload or {})


def _receipt(text: Any) -> Receipt:
    """Wrap the implementation's answer; JSON payloads ride along parsed."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    payload: Optional[Dict[str, Any]] = None
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    return Receipt(text=text, payload=payload)


def _tools():
    """The implementations, imported lazily (this module must stay framework-free)."""
    from tools import beads_tools

    return beads_tools


async def create_bead(
    *,
    context: Any,
    title: str,
    priority: int = 1,
    type: str = "task",
    description: str = "",
    acceptance: str = "",
    assignee: str = "",
    depends_on: Optional[List[str]] = None,
    directory: str = ".",
    request_id: str = "",
) -> Receipt:
    """Create one bead under the operation protocol (see ``beads_create``)."""
    return _receipt(
        await _tools().create_bead(
            context, title, priority, type, description, acceptance, assignee,
            depends_on, directory, request_id,
        )
    )


async def update_bead(
    *,
    context: Any,
    bead_id: str,
    status: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    notes: Optional[str] = None,
    claim: bool = False,
    directory: str = ".",
    request_id: str = "",
    acceptance: Optional[str] = None,
    priority: Optional[int] = None,
    type: Optional[str] = None,
    assignee: Optional[str] = None,
) -> Receipt:
    """Change one bead under the operation protocol (see ``beads_update``)."""
    return _receipt(
        await _tools().update_bead(
            context, bead_id, status, title, description, notes, claim, directory, request_id,
            acceptance, priority, type, assignee,
        )
    )


async def close_bead(
    *,
    context: Any,
    bead_id: str,
    reason: str = "Completed",
    directory: str = ".",
    request_id: str = "",
) -> Receipt:
    """Close one bead under the operation protocol (see ``beads_close``)."""
    return _receipt(await _tools().close_bead(context, bead_id, reason, directory, request_id))


async def plan(
    *,
    context: Any,
    tasks: List[Any],
    directory: str = ".",
    request_id: str = "",
) -> Receipt:
    """Create a whole plan - tasks and edges - in one call (see ``beads_plan``).

    ``tasks`` is what the tool takes: ``PlanTask`` instances, or the same
    fields as plain dicts (validated by the tool's own model, so a bad plan is
    still refused before bd is asked).
    """
    tools = _tools()
    prepared = [task if isinstance(task, tools.PlanTask) else tools.PlanTask(**task) for task in tasks]
    return _receipt(await tools.plan(context, prepared, directory, request_id))


async def dep(
    *,
    context: Any,
    action: str,
    child_id: str,
    parent_id: str,
    directory: str = ".",
    request_id: str = "",
) -> Receipt:
    """Add or remove one dependency edge (see ``beads_dep``)."""
    return _receipt(
        await _tools().dep_bead(context, action, child_id, parent_id, directory, request_id)
    )


__all__ = ["Receipt", "create_bead", "update_bead", "close_bead", "dep", "plan"]
