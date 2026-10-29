"""The ChatGPT plan's quota windows, read from OpenAI on request.

The plan reports what it allows to the official Codex client at
``GET https://chatgpt.com/backend-api/wham/usage``: the same endpoint Codex's
own backend client polls. The request carries the plan's access token and the
account id from the sign-in's ID token, and the answer names each window's
``used_percent``, its length and when it resets. Nothing is spent by reading
it: it is a status call, not a model call.

The endpoint is not part of OpenAI's public API - it is what the first-party
client uses - so a refusal is expected to be possible. It is then reported in
words (:class:`QuotaUnavailable`) and the caller shows no numbers rather than
guessing them. The token never reaches the page: only the windows do.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Mapping, Optional

import httpx

from core.quota_telemetry import QuotaReading, WindowReading
from web_chat.lazy.accounts import CHATGPT, pool_key

logger = logging.getLogger("grid.chatgpt_quota")

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
#: The claim in the sign-in's ID token that names the ChatGPT account to ask about.
ACCOUNT_CLAIM = "https://api.openai.com/auth.chatgpt_account_id"
#: A window no longer than this is the short one; the other is the weekly one.
SHORT_WINDOW = 6 * 3600.0


class QuotaUnavailable(Exception):
    """The plan's windows cannot be read now; the message is safe to show the user."""


def _claims(token: str) -> Mapping[str, Any]:
    """The payload of an ID token, already verified when the sign-in completed."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def account_id(id_token: Optional[str]) -> Optional[str]:
    """The ChatGPT account the sign-in belongs to, from its ID token."""
    if not id_token:
        return None
    claim = _claims(id_token).get(ACCOUNT_CLAIM)
    return str(claim) if claim else None


def _window_name(seconds: Optional[float], fallback: str) -> str:
    if seconds is None:
        return fallback
    return "five_hour" if seconds <= SHORT_WINDOW else "seven_day"


def _window(seconds: Optional[float], used: Any, resets_at: Any, fallback: str) -> Optional[WindowReading]:
    try:
        points = max(0.0, min(100.0, float(used)))
    except (TypeError, ValueError):
        return None
    reset: Optional[float] = None
    if isinstance(resets_at, (int, float)):
        reset = float(resets_at)
    return WindowReading(name=_window_name(seconds, fallback), used=points, resets_at=reset)


def reading_from_payload(payload: Mapping[str, Any], user_id: str, now: Optional[float] = None) -> QuotaReading:
    """The plan's windows from a ``wham/usage`` answer; QuotaUnavailable without any."""
    if not isinstance(payload, Mapping):
        raise QuotaUnavailable("OpenAI's answer was not readable.")
    rate = payload.get("rate_limit")
    rate = rate if isinstance(rate, Mapping) else {}
    windows: dict[str, WindowReading] = {}
    for key, fallback in (("primary_window", "five_hour"), ("secondary_window", "seven_day")):
        snapshot = rate.get(key)
        if not isinstance(snapshot, Mapping):
            continue
        seconds = snapshot.get("limit_window_seconds")
        window = _window(
            float(seconds) if isinstance(seconds, (int, float)) and seconds > 0 else None,
            snapshot.get("used_percent"),
            snapshot.get("reset_at"),
            fallback,
        )
        if window is not None:
            windows[window.name] = window
    # Extra metered features (e.g. a separate code-review allowance) are windows
    # too. They keep the feature's own name: two short windows must never collide.
    for extra in payload.get("additional_rate_limits") or ():
        if not isinstance(extra, Mapping):
            continue
        inner = extra.get("rate_limit") if isinstance(extra.get("rate_limit"), Mapping) else extra
        feature = str(extra.get("metered_feature") or extra.get("limit_name") or "extra")
        for key, fallback in (("primary_window", "primary"), ("secondary_window", "secondary")):
            snapshot = inner.get(key)
            if not isinstance(snapshot, Mapping):
                continue
            try:
                used = max(0.0, min(100.0, float(snapshot.get("used_percent"))))
            except (TypeError, ValueError):
                continue
            resets_at = snapshot.get("reset_at")
            name = f"{feature}:{fallback}"
            windows[name] = WindowReading(
                name=name,
                used=used,
                resets_at=float(resets_at) if isinstance(resets_at, (int, float)) else None,
            )
    if not windows:
        raise QuotaUnavailable("OpenAI's answer has no quota windows for this plan.")
    return QuotaReading(
        account=pool_key(CHATGPT, user_id),
        windows=windows,
        observed_at=time.time() if now is None else now,
        status="rejected" if rate.get("limit_reached") else "allowed",
        overage_allowed=None,
    )


async def fetch_usage(
    vault: Any,
    user_id: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    now: Optional[float] = None,
    timeout: float = 10.0,
) -> QuotaReading:
    """Ask OpenAI for the user's plan windows; QuotaUnavailable when it will not say.

    The access token is refreshed through the vault first (the same path every
    model request takes), and the account id comes from the stored ID token.
    """
    if vault is None or not getattr(vault, "enabled", False):
        raise QuotaUnavailable("This server cannot store the ChatGPT sign-in (GRID_SECRETS_KEY is not set).")
    state = vault.state(user_id, CHATGPT)
    if state is None:
        raise QuotaUnavailable("This account is not signed in with ChatGPT. Connect it on the Plan page.")
    credential = await vault.credential(user_id, CHATGPT)
    if credential is None:
        raise QuotaUnavailable("The ChatGPT sign-in cannot be used now. Sign in with ChatGPT again.")
    account = account_id(state.get("id_token"))
    if account is None:
        raise QuotaUnavailable("The ChatGPT sign-in does not name an account to ask about.")
    headers = {
        "Authorization": f"Bearer {credential.secret}",
        "ChatGPT-Account-Id": account,
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=transport
        ) as http:
            response = await http.get(USAGE_URL, headers=headers)
    except httpx.HTTPError as exc:
        raise QuotaUnavailable(f"Could not reach OpenAI to read the plan: {type(exc).__name__}.") from None
    if response.status_code in (401, 403):
        raise QuotaUnavailable(
            "OpenAI did not accept this plan's token for the usage endpoint. Sign in with ChatGPT again; "
            "if it keeps refusing, OpenAI does not offer this reading to this client."
        )
    if response.status_code == 429:
        raise QuotaUnavailable("OpenAI is rate-limiting the usage check. Try again in a minute.")
    if not response.is_success:
        raise QuotaUnavailable(f"OpenAI answered {response.status_code} when asked for the plan's usage.")
    try:
        payload = response.json()
    except ValueError:
        raise QuotaUnavailable("OpenAI's usage answer was not readable.") from None
    return reading_from_payload(payload, user_id, now)
