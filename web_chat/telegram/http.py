"""The web page's side of the channel: a link code, the state, unlinking, the bot.

    POST   /api/telegram/link-code      a new single-use code, good for ten minutes
    GET    /api/telegram/state          whether this user is linked, to what, reminders
    DELETE /api/telegram/link           disconnect the chat and forget its mapping

    GET    /api/admin/telegram          whether the channel runs, from which token
    PUT    /api/admin/telegram          seal a bot token and run the channel on it
    DELETE /api/admin/telegram          forget the stored token

Linking only ever starts here: a code is made for the signed-in user and
spent once in the chat. The bot never accepts a user id or a permission from
the chat itself.

The routes exist whether the channel runs or not - a page opened on a server
whose channel is off must be told so, not answered with a 404. The admin's
three are registered behind the very dependency the other admin APIs use
(``admins_only`` of the identified user), so a role check and the origin check
of the identified user apply to them unchanged.

The token never comes back out: the admin's page gets a hint (``…5kfWw``) and
where the token comes from. A body that is not ``{"token": "..."}`` is refused
with a reason that does not repeat what was sent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse

from .manager import TelegramManager
from .service import TelegramChannel, TelegramDisabled

#: What a chat with no channel says: the page still draws its Telegram section.
OFF = {"enabled": False, "link": None, "chats": [], "reminders": []}
#: What the admin's page sees before any token is stored.
NO_MANAGER = {
    "enabled": False,
    "source": None,
    "from_env": False,
    "hint": "",
    "can_store": False,
    "error": "",
    "bot_username": "",
}


def _channel(telegram: TelegramManager | TelegramChannel | None) -> TelegramChannel | None:
    """The channel behind *telegram*: a manager owns one; a channel is one."""
    if telegram is None:
        return None
    return telegram.channel if isinstance(telegram, TelegramManager) else telegram


def register_telegram_routes(
    api: APIRouter, current_user: Callable, telegram: TelegramManager | TelegramChannel | None
) -> None:
    """The user's routes: always registered, off or on."""
    identified = Depends(current_user)

    @api.post("/api/telegram/link-code")
    async def link_code(user: Any = identified) -> JSONResponse:
        channel = _channel(telegram)
        if channel is None:
            raise HTTPException(status_code=503, detail="The Telegram channel is off")
        try:
            return JSONResponse(await channel.link_code(user.id))
        except TelegramDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @api.get("/api/telegram/state")
    async def state(user: Any = identified) -> JSONResponse:
        channel = _channel(telegram)
        if channel is None:
            return JSONResponse({**OFF, "available": telegram is not None})
        return JSONResponse({**channel.state(user.id), "available": True})

    @api.delete("/api/telegram/link")
    async def unlink(user: Any = identified) -> JSONResponse:
        channel = _channel(telegram)
        if channel is not None:
            channel.unlink(user.id)
            return JSONResponse(channel.state(user.id))
        return JSONResponse({**OFF, "available": telegram is not None})


def register_telegram_admin_routes(
    api: APIRouter, admin: Callable, telegram: TelegramManager | None
) -> None:
    """The admin's routes: the bot token and whether the channel runs.

    *admin* is ``admins_only(current_user)``, so the role and origin checks of
    every other admin API apply here too.
    """

    @api.get("/api/admin/telegram", dependencies=[Depends(admin)])
    async def telegram_status() -> JSONResponse:
        if telegram is None:
            return JSONResponse(NO_MANAGER)
        return JSONResponse(await telegram.status())

    @api.put("/api/admin/telegram", dependencies=[Depends(admin)])
    async def set_token(body: dict[str, Any] = Body(...)) -> JSONResponse:
        if telegram is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="This server keeps no Telegram store.",
            )
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise HTTPException(status_code=400, detail="A bot token is required.")
        try:
            return JSONResponse(await telegram.apply_token(token))
        except TelegramDisabled as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @api.delete("/api/admin/telegram", dependencies=[Depends(admin)])
    async def forget_token() -> JSONResponse:
        if telegram is None:
            return JSONResponse(NO_MANAGER)
        return JSONResponse(await telegram.forget_token())


#: The page every signed-in user may open: their own link, and for an admin the token.
def register_telegram_page(app: Any, current_user: Callable, telegram: TelegramManager | None) -> None:
    """``/telegram``: the section of a user's settings that is the channel."""
    from pathlib import Path

    page = Path(__file__).with_name("page.html")

    @app.get("/telegram", response_class=HTMLResponse, include_in_schema=False)
    async def telegram_page(user: Any = Depends(current_user)) -> HTMLResponse:
        return HTMLResponse(page.read_text(encoding="utf-8"))
