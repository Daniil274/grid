"""Where a user's model spend goes: the usage record, and the turn that caused it.

Every model call the user's agents make is measured at the HTTP layer
(core.metering) and arrives here as a :class:`~core.model_access.SpendEvent`.
:class:`SpaceMeter` writes it to the usage store and adds it to the turn in
progress, found through a context variable the turn sets around its run: the
agent's tasks start from that context, so do sub-agents and compaction.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Optional

from core.model_access import SpendEvent

logger = logging.getLogger(__name__)


class TurnSpend:
    """What the calls of one turn cost so far, in micro-dollars."""

    def __init__(self, on_charged: Optional[Callable[[int], None]] = None) -> None:
        #: What the operator paid; the figure a turn's dollar budget limits.
        self.charged_micro = 0
        #: What the calls were worth, whoever paid (own keys, subscriptions).
        self.total_micro = 0
        self._on_charged = on_charged

    def add(self, event: SpendEvent) -> None:
        self.total_micro += event.cost.micro
        if event.charged:
            self.charged_micro += event.cost.micro
            if self._on_charged is not None:
                self._on_charged(self.charged_micro)


_CURRENT: ContextVar[Optional[TurnSpend]] = ContextVar("grid_turn_spend", default=None)


@contextmanager
def tracking(spend: TurnSpend) -> Iterator[TurnSpend]:
    """Count the model calls made inside this block (and the tasks it starts) in *spend*."""
    token = _CURRENT.set(spend)
    try:
        yield spend
    finally:
        _CURRENT.reset(token)


class SpaceMeter:
    """The spend sink of one user's space."""

    def __init__(self, user_id: str, store: Optional[Any]) -> None:
        self._user_id = user_id
        self._store = store

    def __call__(self, event: SpendEvent) -> None:
        if self._store is not None:
            try:
                self._store.record(
                    event_id=uuid.uuid4().hex,
                    user_id=self._user_id,
                    model=event.model,
                    provider=event.provider,
                    tokens_in=event.usage.input,
                    tokens_out=event.usage.output,
                    cached_in=event.usage.cached,
                    reasoning_out=event.usage.reasoning,
                    cost_micro=event.cost.micro,
                    cost_basis=event.cost.basis,
                    credential_source=event.source,
                    charged=event.charged,
                )
            except Exception:  # noqa: BLE001 - accounting must never break a run
                logger.exception("Recording a model call's usage failed")
        turn = _CURRENT.get()
        if turn is not None:
            turn.add(event)
