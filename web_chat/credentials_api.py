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
VERIFY_PATHS = {"openrouter": "/key", "openai": "/models", "opencode-go": "/models"}
LABELS = {"openrouter": "OpenRouter", "openai": "OpenAI API", "opencode-go": "OpenCode Go", "chatgpt": "ChatGPT subscription"}
#: Key checks one user may make per minute.
CHECKS_PER_MINUTE = 10


class KeyRejected(Exception):
    pass


class KeyBody(BaseModel):
    secret: str = Field(min_length=8, max_length=400)


async def verify_key(
    kind: str, secret: str, transport: Optional[httpx.AsyncBaseTransport] = None
) -> None:
    """Ask the provider whether *secret* is a working key; KeyRejected when it says no.

    The address is the preset's, never the user's, and redirects are not followed,
    so the key cannot be sent anywhere else.
    """
    url = PROVIDER_PRESETS[kind].rstrip("/") + VERIFY_PATHS[kind]
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, transport=transport) as http:
            response = await http.get(url, headers={"Authorization": f"Bearer {secret}"})
    except httpx.HTTPError as exc:
        raise ConnectionError(f"Could not reach {LABELS[kind]} to check the key: {type(exc).__name__}.") from exc
    if response.status_code in (401, 403):
        raise KeyRejected(f"{LABELS[kind]} did not accept this key.")
    if not response.is_success:  # redirects included: they are never followed
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
