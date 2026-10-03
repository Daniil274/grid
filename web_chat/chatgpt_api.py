"""The ChatGPT sign-in over HTTP: start it from the settings, finish it at the callback.

``POST /api/chatgpt/connect`` returns the OpenAI URL to send the browser to.
OpenAI sends it back to ``http://127.0.0.1:PORT/auth/callback`` - the callback
has to be on the loopback address (web_chat.chatgpt), so the sign-in is offered
only to a browser that opened Grid at ``http://127.0.0.1:PORT`` on the machine
Grid runs on. A Grid server reached over a network cannot be a callback; for
that OpenAI requires approval of the app, which Grid does not claim.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from web_chat.credentials_api import NO_VAULT
from web_chat.chatgpt import CALLBACK_PATH, ChatGPTError, ChatGPTLogin, loopback_redirect
from web_chat.entitlements import CHATGPT, Plans
from web_chat.identity import User
from web_chat.vault import Vault, VaultUnavailable

logger = logging.getLogger(__name__)

LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
KIND = CHATGPT


def on_the_local_machine(request: Request) -> bool:
    """Whether the browser reached Grid at 127.0.0.1 from this very machine."""
    return request.url.hostname == "127.0.0.1" and (request.client.host if request.client else "") in LOOPBACK_CLIENTS


def register_chatgpt_routes(
    app: FastAPI,
    api: APIRouter,
    current_user: Callable[..., Any],
    *,
    login: ChatGPTLogin,
    vault: Vault,
    plans: Plans,
) -> None:
    def allowed(user: User) -> bool:
        return KIND in plans.resolve(admin=user.is_admin, tier=user.tier).own_credentials

    @api.post("/api/chatgpt/connect")
    async def connect(request: Request, user: User = Depends(current_user)) -> JSONResponse:
        if not allowed(user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your plan does not take a ChatGPT sign-in.")
        if not vault.enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, NO_VAULT)
        if not on_the_local_machine(request):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Sign in with ChatGPT works only for Grid running on your own computer: "
                "open it at http://127.0.0.1:PORT there, not through a network address.",
            )
        return JSONResponse({"url": login.begin(user.id, loopback_redirect(request.url.port or 80))})

    callback = APIRouter()

    @callback.get(CALLBACK_PATH, include_in_schema=False)
    async def finish(
        request: Request,
        code: Optional[str] = None,
        state: Optional[str] = None,
        client_id: Optional[str] = None,
        scope: str = "",
        error: Optional[str] = None,
        error_description: Optional[str] = None,
        user: User = Depends(current_user),
    ) -> RedirectResponse:
        def back(result: str) -> RedirectResponse:
            return RedirectResponse(f"/#chatgpt={quote(result)}", status_code=status.HTTP_303_SEE_OTHER)

        if not on_the_local_machine(request) or not allowed(user):
            return back("error:This sign-in cannot be finished here.")
        if error or not (code and state and client_id):
            return back("error:" + (error_description or error or "OpenAI sent no authorization code."))
        try:
            kept = await login.complete(user.id, state=state, code=code, issued_client_id=client_id, scope=scope)
            vault.put_state(user.id, KIND, kept, hint=kept.get("email", ""))
        except (ChatGPTError, VaultUnavailable) as exc:
            return back(f"error:{exc}")
        return back("connected")

    app.include_router(callback)
