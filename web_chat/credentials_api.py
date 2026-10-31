"""The user's plan and their own credentials, over HTTP.

``GET /api/plan`` tells the signed-in user what their plan is, what it lets them
bring (own keys, a ChatGPT login), what they have connected and what they have
spent. ``PUT``/``DELETE /api/credentials/{kind}`` store and remove a provider
key. A key is checked against its provider before it is kept, and is never
returned: the answer carries only a hint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.credentials import PROVIDER_PRESETS
from web_chat.accounts.limits import AttemptLimiter
from web_chat.entitlements import Plans
from web_chat.identity import User
from web_chat.limits import utc_day_start, utc_month_start
from web_chat.vault import Vault, VaultUnavailable

logger = logging.getLogger(__name__)

NO_VAULT = (
    "This server cannot store credentials: the operator has not set GRID_SECRETS_KEY. "
    "Make one with `python -m web_chat.vault new-key`, add GRID_SECRETS_KEY=<key> to .env and restart the server."
)

#: What each provider preset answers when its key is good. OpenRouter's model list
#: is public, so its key is checked on the endpoint that describes the key.
VERIFY_PATHS = {
    "kimi": "/models",
    "kimi-code": "/models",
    "openrouter": "/key",
    "openai": "/models",
    "opencode-go": "/models",
}
#: A key can be accepted and still run nothing: the account is out of balance,
#: the plan does not include the model, the model id is wrong. A model list
#: answers none of that, so for these presets one tiny completion is sent as
#: well, and "saved" then means the model the systems use actually answers.
VERIFY_MODELS = {"kimi-code": "k3.1-pro"}
LABELS = {
    "kimi": "Kimi (Moonshot AI)",
    "kimi-code": "Kimi Code (Kimi Plus)",
    "openrouter": "OpenRouter",
    "openai": "OpenAI API",
    "opencode-go": "OpenCode Go",
    "chatgpt": "ChatGPT subscription",
}
#: Key checks one user may make per minute.
CHECKS_PER_MINUTE = 10

#: The provider's own words for "the key is fine, the account is not": an empty
#: balance, a suspended account, a plan without the model.
_QUOTA_MARKERS = (
    "insufficient balance",
    "insufficient_quota",
    "exceeded_current_quota",
    "suspended",
    "quota exceeded",
    "credit",
    "billing",
)
_MODEL_MARKERS = ("model_not_found", "model not found", "does not exist", "no access to the model")
_PLAN_MARKERS = ("doesn't have access", "does not have access", "plan does not", "upgrade")


class KeyRejected(Exception):
    """The provider does not know this key: nothing to store."""


class KeyUnusable(Exception):
    """The key is real, but it cannot run the model the systems ask for.

    Carries a machine-readable ``code`` (``quota_exceeded``,
    ``model_unavailable``, ``plan_lacks_model``, ``bad_request``) so the panel
    says what to fix instead of "connected, and nothing works".
    """

    def __init__(self, message: str, code: str = "unusable") -> None:
        super().__init__(message)
        self.code = code


class KeyBody(BaseModel):
    secret: str = Field(min_length=8, max_length=400)


async def verify_key(
    kind: str, secret: str, transport: Optional[httpx.AsyncBaseTransport] = None
) -> None:
    """Ask the provider whether *secret* is a working key; KeyRejected when it says no.

    The address is the preset's, never the user's, and redirects are not followed,
    so the key cannot be sent anywhere else.
    """
    base = PROVIDER_PRESETS[kind].rstrip("/")
    url = base + VERIFY_PATHS[kind]
    headers = {"Authorization": f"Bearer {secret}"}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, transport=transport) as http:
            response = await http.get(url, headers=headers)
            _refusal(kind, response)
            model = VERIFY_MODELS.get(kind)
            if model is not None:
                probe = await http.post(
                    base + "/chat/completions",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 32,
                        "stream": False,
                    },
                )
                _refusal(kind, probe, model)
    except httpx.HTTPError as exc:
        raise ConnectionError(f"Could not reach {LABELS[kind]} to check the key: {type(exc).__name__}.") from exc


def _error_of(response: httpx.Response) -> tuple[str, str]:
    """The provider's error ``type``/``code`` and its message, as far as it gives them."""
    try:
        error = (response.json() or {}).get("error") or {}
    except ValueError:
        return "", response.text[:300]
    kind = str(error.get("type") or error.get("code") or "")
    return kind, str(error.get("message") or "")


def _refusal(kind: str, response: httpx.Response, model: Optional[str] = None) -> None:
    """Raise unless *response* says the key (and, for a probe, the model) works."""
    if response.is_success:
        return
    error_kind, message = _error_of(response)
    text = f"{error_kind} {message}".lower()
    said = f" {message.strip()}" if message.strip() else ""
    if response.status_code in (401, 403):
        # Kimi Code answers 403 for a key whose plan lacks the model, so the
        # body decides: a refusal of the key itself is not a plan problem.
        if any(marker in text for marker in _PLAN_MARKERS) or "access to the model" in text:
            raise KeyUnusable(
                f"{LABELS[kind]} accepted the key, but this plan does not include {model or 'that model'}:{said}",
                code="plan_lacks_model",
            )
        raise KeyRejected(f"{LABELS[kind]} did not accept this key.{said}")
    if any(marker in text for marker in _QUOTA_MARKERS):
        raise KeyUnusable(
            f"{LABELS[kind]} accepted the key, but the account cannot run {model or 'the model'}:"
            f"{said or ' no balance.'} Top up the account or use a key from a plan that includes it.",
            code="quota_exceeded",
        )
    if any(marker in text for marker in _MODEL_MARKERS):
        raise KeyUnusable(
            f"{LABELS[kind]} accepted the key, but has no model '{model}':{said}",
            code="model_unavailable",
        )
    if any(marker in text for marker in _PLAN_MARKERS):
        raise KeyUnusable(
            f"{LABELS[kind]} accepted the key, but this plan does not include {model or 'that model'}:{said}",
            code="plan_lacks_model",
        )
    if response.status_code in (400, 404, 422) and model is not None:
        raise KeyUnusable(
            f"{LABELS[kind]} accepted the key, but refused a request for {model}:{said or f' HTTP {response.status_code}.'}",
            code="bad_request",
        )
    if response.status_code == 429:
        return  # a real rate limit: the key and the model are fine, try later
    # redirects included: they are never followed
    raise ConnectionError(f"{LABELS[kind]} answered {response.status_code} when checking the key.")


def listed(kind: str, stored: Optional[dict]) -> dict:
    """One provider as the interface sees it: what is connected, never the secret."""
    entry = {"kind": kind, "label": LABELS[kind], "connected": stored is not None}
    if stored is not None:
        entry.update({key: stored[key] for key in ("hint", "status", "updated_at")})
    return entry


def register_credential_routes(
    api: APIRouter,
    current_user: Callable[..., Any],
    *,
    plans: Plans,
    vault: Vault,
    spent_micro: Callable[[str, float], int],
    chatgpt: Optional[Any] = None,
    verify: Optional[Callable[..., Any]] = None,
    clock: Callable[[], float] = time.time,
) -> None:
    checks = AttemptLimiter(limit=CHECKS_PER_MINUTE, window=60)

    def entitlement(user: User):
        return plans.resolve(admin=user.is_admin, tier=user.tier)

    @api.get("/api/plan")
    async def plan(user: User = Depends(current_user)) -> JSONResponse:
        mine = entitlement(user)
        now = clock()
        stored = {row["kind"]: row for row in vault.listing(user.id)}
        kinds = [kind for kind in (*PROVIDER_PRESETS, "chatgpt") if kind in mine.own_credentials]
        return JSONResponse({
            "tier": mine.tier,
            "label": mine.label or mine.tier,
            "models": list(mine.models),
            "limits": mine.limits.model_dump(),
            "spent": {
                "day_usd": spent_micro(user.id, utc_day_start(now)) / 1_000_000,
                "month_usd": spent_micro(user.id, utc_month_start(now)) / 1_000_000,
            },
            "vault_enabled": vault.enabled,
            "credentials": [listed(kind, stored.get(kind)) for kind in kinds],
            "chatgpt_available": chatgpt is not None and "chatgpt" in mine.own_credentials,
        })

    @api.put("/api/credentials/{kind}")
    async def store_key(kind: str, body: KeyBody, user: User = Depends(current_user)) -> JSONResponse:
        if kind not in PROVIDER_PRESETS or kind not in entitlement(user).own_credentials:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your plan does not take your own key for this provider.")
        if not vault.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NO_VAULT)
        if checks.retry_after(user.id):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many key checks. Wait a minute.")
        checks.fail(user.id)  # every check counts, good or bad
        secret = body.secret.strip()
        try:
            await (verify or verify_key)(kind, secret)
        except KeyRejected as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        except KeyUnusable as exc:
            # Typed: the panel shows what to fix, and the key is not stored as
            # if it worked - a dead account must not look like a connection.
            return JSONResponse(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                content={"detail": str(exc), "code": exc.code},
            )
        except ConnectionError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
        try:
            hint = await asyncio.to_thread(vault.put_api_key, user.id, kind, secret)
        except VaultUnavailable as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
        return JSONResponse({"kind": kind, "connected": True, "hint": hint, "status": "ok"})

    @api.delete("/api/credentials/{kind}")
    async def remove_credential(kind: str, user: User = Depends(current_user)) -> JSONResponse:
        if kind == "chatgpt" and chatgpt is not None:
            kept = vault.state(user.id, kind)
            if kept is not None:
                await chatgpt.disconnect(kept)  # the stored copy goes either way
        if not vault.delete(user.id, kind):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Nothing stored for that provider.")
        return JSONResponse({"kind": kind, "connected": False})
