"""What subscriptions report about their quota windows.

A subscription limits use per window - Claude Code reports a five-hour and a
seven-day window - and tells how much of each is used whenever it answers. This
module keeps the latest of those readings per account, for whoever plans
around the limits (core.lazy). It knows no provider protocol beyond turning a
report into a :class:`QuotaReading`; the bridges publish what they see:

- Claude Code (core.claude_cli) sends a ``rate_limit_event`` with every run:
  ``unifiedWindows`` holds each window's ``utilization`` (0..1) and the moment
  it ``resetsAt`` (Unix seconds), ``overageStatus`` whether paid extra usage
  would be spent beyond the plan.

Quantities are percentage points of a window's capacity (0..100): a provider
that reports fractions or percents maps onto one scale, and windows of
different lengths are never added together.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional

logger = logging.getLogger("grid.quota")

#: The host's Claude Code login: one account, whoever's turn used it.
CLAUDE_CODE = "claude-code"
#: Readings kept per account, newest last.
HISTORY = 200


@dataclass(frozen=True)
class WindowReading:
    """One window of an account as last reported: used points and its reset."""

    name: str
    used: float
    resets_at: Optional[float] = None


@dataclass(frozen=True)
class QuotaReading:
    """An account's windows at one moment, as the provider reported them."""

    account: str
    windows: Mapping[str, WindowReading]
    observed_at: float
    #: The provider's verdict on the request: "allowed", "allowed_warning", "rejected".
    status: str = "allowed"
    #: Whether use beyond the plan would be billed; None when not reported.
    overage_allowed: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "observed_at": self.observed_at,
            "status": self.status,
            "overage_allowed": self.overage_allowed,
            "windows": {
                name: {"used": window.used, "resets_at": window.resets_at}
                for name, window in self.windows.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QuotaReading":
        return cls(
            account=str(data["account"]),
            observed_at=float(data["observed_at"]),
            status=str(data.get("status") or "allowed"),
            overage_allowed=data.get("overage_allowed"),
            windows={
                str(name): WindowReading(
                    name=str(name),
                    used=float(window.get("used", 0.0)),
                    resets_at=float(window["resets_at"]) if window.get("resets_at") is not None else None,
                )
                for name, window in (data.get("windows") or {}).items()
            },
        )


def claude_reading(info: Any, now: Optional[float] = None) -> Optional[QuotaReading]:
    """The reading in a Claude Code ``rate_limit_info``; None when it carries no window."""
    if not isinstance(info, Mapping):
        return None
    windows: Dict[str, WindowReading] = {}
    for name, window in (info.get("unifiedWindows") or {}).items():
        if not isinstance(window, Mapping):
            continue
        try:
            used = float(window.get("utilization"))
        except (TypeError, ValueError):
            continue
        resets_at = window.get("resetsAt")
        windows[str(name)] = WindowReading(
            name=str(name),
            used=max(0.0, min(100.0, used * 100.0)),
            resets_at=float(resets_at) if isinstance(resets_at, (int, float)) else None,
        )
    if not windows:
        return None
    overage = info.get("overageStatus")
    return QuotaReading(
        account=CLAUDE_CODE,
        windows=windows,
        observed_at=time.time() if now is None else now,
        status=str(info.get("status") or "allowed"),
        overage_allowed=None if overage is None else overage == "allowed",
    )


Listener = Callable[[QuotaReading], None]


@dataclass
class QuotaTelemetry:
    """The latest readings per account, and who wants to hear of new ones."""

    _history: Dict[str, Deque[QuotaReading]] = field(default_factory=dict)
    _listeners: List[Listener] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, reading: QuotaReading) -> None:
        with self._lock:
            history = self._history.setdefault(reading.account, deque(maxlen=HISTORY))
            if history and history[-1].observed_at > reading.observed_at:
                return  # an older report arriving late changes nothing
            history.append(reading)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(reading)
            except Exception:  # noqa: BLE001 - a listener never breaks the bridge that reported
                logger.exception("A quota listener failed")

    def latest(self, account: str) -> Optional[QuotaReading]:
        with self._lock:
            history = self._history.get(account)
            return history[-1] if history else None

    def history(self, account: str) -> List[QuotaReading]:
        with self._lock:
            return list(self._history.get(account) or ())

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call *listener* with every new reading; returns the unsubscribe."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe


_telemetry = QuotaTelemetry()


def telemetry() -> QuotaTelemetry:
    """The process's telemetry: every bridge publishes here."""
    return _telemetry


def report_claude(info: Any) -> None:
    """Publish a Claude Code ``rate_limit_info``; a malformed one is ignored."""
    reading = claude_reading(info)
    if reading is not None:
        _telemetry.publish(reading)
