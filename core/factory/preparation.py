"""Dependency-aware execution of the steps run before an agent is asked.

The steps are ``auto_run_tools`` of an agent and the ``init_tools`` a caller
passes (core.factory.auto_run). Their historical behavior is a plain sequence,
and that stays the default: a batch runs concurrently only when at least one
step explicitly says it may with ``"parallel": true``.

Two optional keys shape a batch:

``parallel``
    This step is safe to run alongside other ``parallel`` steps of the batch.
    A step without it is a barrier: nothing starts while it runs and it does
    not start before everything before it finished, so an operator's ordinary
    steps keep their exact place in the order.

``depends_on``
    Names of earlier steps of the same batch that must finish first. A name
    refers to the nearest earlier step with that name, again earlier, so the
    graph is acyclic by construction. A step whose dependency did not finish
    (failed, was blocked or was itself skipped) is skipped with a reason: its
    input would be a lie.

Results are reported in the batch's order, never in completion order, so the
text injected into the instructions is the same whatever finished first.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"
SKIPPED = "skipped"


class PreparationError(ValueError):
    """A batch that cannot be ordered: a dependency naming no earlier step."""


@dataclass(frozen=True)
class Step:
    """One preparation call, with what decides when it may start."""

    index: int
    name: str
    spec: Mapping[str, Any]
    parallel: bool = False
    depends_on: Tuple[str, ...] = ()


@dataclass
class StepOutcome:
    """What became of one step: its status, text, reason and duration."""

    step: Step
    status: str
    value: str = ""
    detail: str = ""
    seconds: float = 0.0


def plan_steps(specs: Sequence[Mapping[str, Any]]) -> List[Step]:
    """Read a batch's specs into steps; unknown keys are left to the caller."""
    steps: List[Step] = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, Mapping):
            raise PreparationError(f"step {index} is not a mapping")
        depends_on = spec.get("depends_on") or ()
        if isinstance(depends_on, str):
            depends_on = (depends_on,)
        steps.append(
            Step(
                index=index,
                name=str(spec.get("name") or ""),
                spec=spec,
                parallel=bool(spec.get("parallel", False)),
                depends_on=tuple(str(name) for name in depends_on),
            )
        )
    return steps


def is_concurrent(steps: Sequence[Step]) -> bool:
    """True when the batch opted into concurrency; legacy batches stay sequential."""
    return any(step.parallel for step in steps)


def _previous_index_named(steps: Sequence[Step], index: int, name: str) -> Optional[int]:
    for earlier in range(index - 1, -1, -1):
        if steps[earlier].name == name:
            return earlier
    return None


def ordering(steps: Sequence[Step], index: int) -> List[int]:
    """The step indices *index* must wait for before it starts.

    Ordering alone never skips a step: a step that follows a failed one still
    runs, as it always did in the sequence. Only ``depends_on`` makes a
    result needed (see :func:`data_dependencies`).
    """
    step = steps[index]
    if not step.parallel:
        # A step that did not opt in never overlaps anything before it.
        return list(range(index))
    for earlier in range(index - 1, -1, -1):
        if not steps[earlier].parallel:
            # The nearest barrier already waited for everything before it.
            return [earlier]
    return []


def data_dependencies(steps: Sequence[Step], index: int) -> List[int]:
    """The earlier steps whose result *index* reads, from ``depends_on``.

    Raises:
        PreparationError: a ``depends_on`` name has no earlier step.
    """
    step = steps[index]
    needed: List[int] = []
    for name in step.depends_on:
        found = _previous_index_named(steps, index, name)
        if found is None:
            raise PreparationError(
                f"step '{step.name}' depends on '{name}', which is not an earlier step of the batch"
            )
        if found not in needed:
            needed.append(found)
    return needed


async def run_steps(
    steps: Sequence[Step],
    run_one: Callable[[Step], Awaitable[Tuple[str, str]]],
) -> List[StepOutcome]:
    """Run *steps* respecting their order, dependencies and opt-in.

    *run_one* returns ``(text, status)``; status is ``DONE`` when the call
    succeeded, ``FAILED`` when it raised, ``BLOCKED`` when the action policy
    refused it. Only ``DONE`` satisfies a dependency. A step that raises is
    recorded as ``FAILED``; a step whose dependency did not finish is
    ``SKIPPED`` with the reason and never invoked. Cancellation
    cancels every in-flight step and is then re-raised: a stopped turn stops
    the preparation too.

    Returns:
        One outcome per step, in the batch's order.
    """
    if not steps:
        return []

    outcomes: List[Optional[StepOutcome]] = [None] * len(steps)
    finished = [asyncio.Event() for _ in steps]
    if not is_concurrent(steps):
        return await _run_sequential(steps, run_one, outcomes)

    async def one(index: int) -> None:
        step = steps[index]
        started = time.perf_counter()
        try:
            required = data_dependencies(steps, index)
            wait_for = sorted(set(required) | set(ordering(steps, index)))
            if wait_for:
                await asyncio.gather(*(finished[earlier].wait() for earlier in wait_for))
            blocked = [
                earlier
                for earlier in required
                if outcomes[earlier] is None or outcomes[earlier].status != DONE
            ]
            if blocked:
                reason = ", ".join(
                    f"'{steps[earlier].name}' {outcomes[earlier].status if outcomes[earlier] else 'did not run'}"
                    for earlier in blocked
                )
                outcomes[index] = StepOutcome(
                    step, SKIPPED, detail=f"skipped: its dependency {reason}"
                )
                return
            value, status = await run_one(step)
            outcomes[index] = StepOutcome(step, status, value=value)
        except asyncio.CancelledError:
            outcomes[index] = StepOutcome(step, SKIPPED, detail="cancelled")
            raise
        except PreparationError as exc:
            outcomes[index] = StepOutcome(step, SKIPPED, detail=f"skipped: {exc}")
        except Exception as exc:  # a step's own failure, not the batch's
            outcomes[index] = StepOutcome(
                step, FAILED, detail=f"{type(exc).__name__}: {exc}"
            )
        finally:
            outcome = outcomes[index]
            if outcome is not None:
                outcome.seconds = time.perf_counter() - started
            finished[index].set()

    tasks = [asyncio.create_task(one(index)) for index in range(len(steps))]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            # A step cancelled itself: the batch stops with it.
            for other in tasks:
                other.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise result
    return [outcome for outcome in outcomes if outcome is not None]


async def _run_sequential(
    steps: Sequence[Step],
    run_one: Callable[[Step], Awaitable[Tuple[str, str]]],
    outcomes: List[Optional[StepOutcome]],
) -> List[StepOutcome]:
    """The legacy path: one call after another, in the batch's order."""
    for index, step in enumerate(steps):
        started = time.perf_counter()
        try:
            required = data_dependencies(steps, index)
        except PreparationError as exc:
            outcomes[index] = StepOutcome(step, SKIPPED, detail=f"skipped: {exc}")
            outcomes[index].seconds = time.perf_counter() - started
            continue
        blocked = [
            earlier
            for earlier in required
            if outcomes[earlier] is None or outcomes[earlier].status != DONE
        ]
        if blocked:
            reason = ", ".join(
                f"'{steps[earlier].name}' {outcomes[earlier].status if outcomes[earlier] else 'did not run'}"
                for earlier in blocked
            )
            outcomes[index] = StepOutcome(
                step, SKIPPED, detail=f"skipped: its dependency {reason}"
            )
            outcomes[index].seconds = time.perf_counter() - started
            continue
        try:
            value, status = await run_one(step)
            outcomes[index] = StepOutcome(step, status, value=value)
        except asyncio.CancelledError:
            outcomes[index] = StepOutcome(step, SKIPPED, detail="cancelled")
            raise
        except Exception as exc:
            outcomes[index] = StepOutcome(
                step, FAILED, detail=f"{type(exc).__name__}: {exc}"
            )
        finally:
            outcome = outcomes[index]
            if outcome is not None:
                outcome.seconds = time.perf_counter() - started
    return [outcome for outcome in outcomes if outcome is not None]


def timings(outcomes: Sequence[StepOutcome], wall_seconds: float) -> Dict[str, Any]:
    """A record of the batch for logs and for the run's metadata."""
    return {
        "wall_ms": round(wall_seconds * 1000, 1),
        "serial_ms": round(sum(outcome.seconds for outcome in outcomes) * 1000, 1),
        "steps": [
            {
                "name": outcome.step.name,
                "status": outcome.status,
                "seconds": round(outcome.seconds, 4),
                "detail": outcome.detail,
            }
            for outcome in outcomes
        ],
    }
