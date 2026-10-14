"""Structured failures for the workflow engine (C3 of the durable-workflows plan).

``StepFailure`` itself is part of the shared contract and lives in
:mod:`core.workflows.spec`. This module owns the *policy* around it:

* ``retryable`` is computed from the failure code and the trusted effect
  classification of the step kind (invariant I8) — never from model text and
  never from a self-declared ``idempotent`` flag. With no step to classify
  against, the default is **unsafe** (``retryable=False``).
* :class:`StepException` and its subclasses are how an injected invoker
  adapter reports a *structured* failure instead of a bare exception.
* :func:`make_failure` / :func:`failure_from_exception` are the single place
  that turns a code (or a raised exception) into the typed failure the engine
  persists.

Nothing here performs effects; it is pure policy and data.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from .spec import (
    FailureCode,
    Step,
    StepFailure,
    is_retryable,
)

__all__ = [
    "StepFailure",
    "StepException",
    "PolicyViolation",
    "UnknownEffect",
    "MalformedResult",
    "InitRequiredFailed",
    "make_failure",
    "failure_from_exception",
]


class StepException(Exception):
    """An invoker adapter reports a *structured* step failure by raising this.

    The engine maps it to a :class:`StepFailure` with the class's ``code``;
    ``retryable`` is still computed by the engine's policy (I8), not trusted
    from the adapter.
    """

    code: FailureCode = "PERMANENT_ERROR"

    def __init__(
        self, detail: str, *, allowed_write_paths: Optional[list[str]] = None
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        #: For POLICY_VIOLATION: paths a coordinator may still approve.
        self.allowed_write_paths = allowed_write_paths


class PolicyViolation(StepException):
    """A step tried to touch a path outside its declared write zone."""

    code = "POLICY_VIOLATION"


class UnknownEffect(StepException):
    """An effect may have happened and cannot be verified by the adapter."""

    code = "UNKNOWN_EFFECT"


class MalformedResult(StepException):
    """The adapter returned something that is not a valid StepResult."""

    code = "MALFORMED"


class InitRequiredFailed(StepException):
    """A required init tool did not run; the run must not start."""

    code = "INIT_REQUIRED_FAILED"


def make_failure(
    step_id: str,
    code: FailureCode,
    detail: str,
    *,
    step: Optional[Step] = None,
    attempt: int = 0,
    allowed_write_paths: Optional[list[str]] = None,
) -> StepFailure:
    """Build a :class:`StepFailure` with its retryable flag computed by policy.

    When *step* is given the flag comes from :func:`spec.is_retryable` (code +
    trusted effect class). Without a step there is nothing to trust, so the
    safe default is ``retryable=False``.
    """
    retryable = is_retryable(step, code) if step is not None else False
    return StepFailure(
        step_id=step_id,
        code=code,
        retryable=retryable,
        detail=detail,
        allowed_write_paths=allowed_write_paths,
        attempt=attempt,
    )


def failure_from_exception(
    step: Step, error: BaseException, *, attempt: int = 0
) -> StepFailure:
    """Map an exception raised by the invoker onto a structured failure.

    ``StepException`` subclasses carry their own code; a cancellation is the
    engine's business (never mapped here); a timeout is ``TIMEOUT``; anything
    else is treated as ``MALFORMED`` — an adapter that cannot classify its own
    failure is not trusted to claim a harmless one.
    """
    if isinstance(error, StepException):
        detail = error.detail or str(error)
        return make_failure(
            step.id,
            error.code,
            detail,
            step=step,
            attempt=attempt,
            allowed_write_paths=error.allowed_write_paths,
        )
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return make_failure(
            step.id, "TIMEOUT", str(error) or "step work deadline exceeded",
            step=step, attempt=attempt,
        )
    return make_failure(
        step.id, "MALFORMED", f"{type(error).__name__}: {error}",
        step=step, attempt=attempt,
    )
