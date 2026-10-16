"""Executors of one batch side by side in one workspace (orchestrate_batch).

The agents of a batch think, read and search at the same time - that is where
the time goes. What they change is kept apart in two ways, both decided here
from the tools' declared effects (utils.tool_effects), never from what an
agent says about its call:

- A call that changes something - a file, the tracker, a shell command whose
  effect is not known in advance - runs alone: the batch's agents take turns
  for these, so two of them never write at the same moment (a shared index,
  the tracker's database, a half-written file).
- A file write must fall inside the ``write_paths`` its task declared. The
  batch rejects tasks whose paths overlap before any of them starts, so two
  agents never edit the same file from stale reads.

A shell command is serialized, not confined: what it writes is not known
before it runs. The action policy judges every call as before; a lane only
orders and scopes the calls the policy let through.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import posixpath
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Iterator, Optional

from core.action_routing import UNKNOWN, effect_of, is_readonly
from schemas.action_policy import DEFAULT_READONLY_COMMANDS
from utils.tool_effects import DELEGATE, DELEGATE_KIND, EGRESS, EXEC, READ, WRITE, Effect

#: Effects that leave the workspace and the user's stores as they were.
_UNCHANGING = {READ, EGRESS, DELEGATE_KIND}

Invoke = Callable[[Any, Any], Awaitable[Any]]


@dataclass(frozen=True)
class Lane:
    """One executor's place in a batch.

    ``write_paths`` are workspace paths (POSIX, relative) its file writes may
    touch: a file, or a directory and everything under it. ``changes`` is the
    batch's lock for calls that change something.
    """

    label: str
    write_paths: tuple[str, ...]
    changes: asyncio.Lock

    def allows(self, path: str) -> bool:
        return any(covers(scope, path) for scope in self.write_paths)


_lane: contextvars.ContextVar[Optional[Lane]] = contextvars.ContextVar("_parallel_lane", default=None)


@contextmanager
def entering(lane: Lane) -> Iterator[Lane]:
    """Run this context - and the agents and tools it starts - in *lane*."""
    token = _lane.set(lane)
    try:
        yield lane
    finally:
        _lane.reset(token)


def current_lane() -> Optional[Lane]:
    return _lane.get()


def covers(scope: str, path: str) -> bool:
    """Whether workspace path *path* is *scope* or lies under it."""
    return scope in ("", ".") or path == scope or path.startswith(scope.rstrip("/") + "/")


def overlap(first: Iterable[str], second: Iterable[str]) -> Optional[tuple[str, str]]:
    """A pair of paths, one of each, where one contains the other; None if apart."""
    for a in first:
        for b in second:
            if covers(a, b) or covers(b, a):
                return a, b
    return None


def normalize(path: str) -> str:
    """A located workspace path in the form :func:`covers` compares."""
    normalized = posixpath.normpath(path.replace("\\", "/"))
    return "" if normalized == "." else normalized


def guard(
    invoke: Invoke,
    tool_name: str,
    kind: str,
    effect: Optional[Effect],
    policy: Any = None,
    locate: Optional[Callable[[Any], Callable[[str], Optional[str]]]] = None,
) -> Invoke:
    """*invoke* as the lane of the calling run lets it run.

    Outside a batch the call runs as it is. ``policy`` is the action policy
    (its operator-declared effects and read-only commands), ``locate`` makes a
    path locator from the call's run context (ActionGate._locator).
    """
    lane = current_lane()
    if lane is None:
        return invoke

    async def run_in_lane(ctx: Any, raw_args: Any) -> Any:
        arguments = _arguments(raw_args)
        if policy is not None:
            resolved = effect_of(policy, tool_name, kind, effect)
        else:
            # An agent called as a tool only delegates, as effect_of decides with
            # a policy: holding the lock through its run would block its own writes.
            resolved = effect or (DELEGATE if kind == "agent" else None)
        if not _changes(resolved, arguments, policy):
            return await invoke(ctx, raw_args)
        if resolved is not None and resolved.kind == WRITE and locate is not None:
            locator = locate(getattr(ctx, "context", None))
            for name in resolved.paths:
                raw = arguments.get(name)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                located = locator(raw)
                if located is not None and not lane.allows(normalize(located)):
                    return _outside_scope(tool_name, raw, lane)
        async with lane.changes:
            return await invoke(ctx, raw_args)

    return run_in_lane


def _changes(effect: Optional[Effect], arguments: dict, policy: Any) -> bool:
    """Whether the call may change something: anything but a read, a fetch,
    a delegation (its agent's calls are guarded one by one) or a read-only command."""
    kind = effect.kind if effect is not None else UNKNOWN
    if kind in _UNCHANGING:
        return False
    if kind == EXEC and effect is not None and effect.command:
        command = arguments.get(effect.command)
        commands = getattr(policy, "readonly_commands", DEFAULT_READONLY_COMMANDS)
        if isinstance(command, str) and is_readonly(command, commands):
            return False
    return True


def _arguments(raw_args: Any) -> dict:
    if isinstance(raw_args, dict):
        return raw_args
    try:
        parsed = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _outside_scope(tool_name: str, raw: str, lane: Lane) -> str:
    scopes = ", ".join(lane.write_paths) if lane.write_paths else "none"
    return (
        f"❌ {tool_name}: '{raw}' is outside the paths this task may change "
        f"(write_paths: {scopes}). Other agents of the batch are working beside "
        "this one; do not change it. Name the change it needs in your report instead."
    )
