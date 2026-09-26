"""Accounts over HTTP: the session cookie, sign-in and sign-up, and the admin API.

:class:`SessionAuth` is the server's ``Identify`` when it has accounts: the
user of a request is the one whose session the ``grid_session`` cookie opens.
The cookie is ``HttpOnly`` (page scripts cannot read it), ``SameSite=Lax`` and,
over HTTPS, ``Secure``.

Routes::

    POST /api/auth/login      {username, password}          -> sets the cookie
    POST /api/auth/register   {invite, username, password}  -> sets the cookie
    POST /api/auth/logout                                    -> clears it
    GET  /api/auth/me
    POST /api/auth/password   {current, new}

    GET    /api/admin/users              (admins)
    PATCH  /api/admin/users/{id}         {disabled?, role?}
    GET    /api/admin/invites
    POST   /api/admin/invites            {role, days, note}  -> the code, once
    DELETE /api/admin/invites/{id}

Sign-in and sign-up are rate limited (web_chat.accounts.limits). Hashing a
password takes tens of milliseconds of CPU, so it runs in a worker thread.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from web_chat.accounts.limits import AttemptLimiter
from web_chat.accounts.passwords import MAX_LENGTH, WeakPassword
from web_chat.accounts.service import SESSION_MAX_AGE, AccountError, Accounts, SignInFailed
from web_chat.identity import Role, User, admins_only
from web_chat.security import OriginGuard, refusal

COOKIE = "grid_session"

#: Failed sign-ins allowed per username and address, and per address, in the window.
PER_ACCOUNT_LIMIT = 5
PER_ADDRESS_LIMIT = 30
LIMIT_WINDOW = 15 * 60


class Credentials(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=MAX_LENGTH)


class Registration(Credentials):
    invite: str = Field(min_length=1, max_length=128)


class PasswordChange(BaseModel):
    current: str = Field(max_length=MAX_LENGTH)
    new: str = Field(max_length=MAX_LENGTH)


class UserUpdate(BaseModel):
    disabled: Optional[bool] = None
    role: Optional[Literal["admin", "user"]] = None


class InviteRequest(BaseModel):
    role: Literal["admin", "user"] = "user"
    days: float = Field(default=7, gt=0, le=90)
    note: str = Field(default="", max_length=200)


def user_payload(user: User) -> dict[str, Any]:
    return {"id": user.id, "username": user.username, "role": user.role}


class SessionAuth:
    """Identifies users by their session cookie and serves the account routes."""

    def __init__(
        self,
        accounts: Accounts,
        *,
        secure_cookies: Optional[bool] = None,
        limiter_factory: Callable[..., AttemptLimiter] = AttemptLimiter,
    ) -> None:
        """``secure_cookies`` None marks the cookie Secure exactly when the
        request came over HTTPS."""
        self.accounts = accounts
        self._secure_cookies = secure_cookies
        self._per_account = limiter_factory(limit=PER_ACCOUNT_LIMIT, window=LIMIT_WINDOW)
        self._per_address = limiter_factory(limit=PER_ADDRESS_LIMIT, window=LIMIT_WINDOW)

    # -- identification --------------------------------------------------------------
    def user_of(self, connection: HTTPConnection) -> Optional[User]:
        """The signed-in user of *connection*, or None."""
        token = connection.cookies.get(COOKIE)
        return self.accounts.user_for_session(token) if token else None

    async def identify(self, connection: HTTPConnection) -> User:
        user = self.user_of(connection)
        if user is None:
            raise refusal(connection, status.HTTP_401_UNAUTHORIZED, "Sign in to continue")
        return user

    def maintain(self) -> None:
        """Periodic upkeep: expired sessions and stale failure counts go."""
        self.accounts.purge_expired_sessions()
        self._per_account.prune()
        self._per_address.prune()

    # -- cookie ----------------------------------------------------------------------
    def _set_cookie(self, request: Request, response: Response, token: str) -> None:
        secure = self._secure_cookies if self._secure_cookies is not None else request.url.scheme == "https"
        response.set_cookie(
            COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=secure, path="/"
        )

    @staticmethod
    def _clear_cookie(response: Response) -> None:
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")

    # -- rate limits -----------------------------------------------------------------
    def _limit_keys(self, request: Request, username: str) -> tuple[str, str]:
        address = request.client.host if request.client else "unknown"
        return f"{address}|{username.casefold()}", address

    def _refuse_when_limited(self, keys: tuple[str, str]) -> None:
        wait = max(self._per_account.retry_after(keys[0]), self._per_address.retry_after(keys[1]))
        if wait > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts. Try again later.",
                headers={"Retry-After": str(int(wait) + 1)},
            )

    def _failed(self, keys: tuple[str, str]) -> None:
        self._per_account.fail(keys[0])
        self._per_address.fail(keys[1])

    # -- routes ----------------------------------------------------------------------
    def register_routes(self, app: FastAPI, *, guard: OriginGuard, current_user: Callable[..., Any]) -> None:
        """Add the auth and admin routes. *current_user* identifies the caller
        (the server's dependency, origin check included)."""

        async def same_site(request: Request) -> None:
            guard.check(request)

        admin = admins_only(current_user)

        public = APIRouter(prefix="/api/auth", dependencies=[Depends(same_site)])
        signed_in = APIRouter(prefix="/api/auth", dependencies=[Depends(current_user)])
        admins = APIRouter(prefix="/api/admin", dependencies=[Depends(admin)])

        @public.post("/login")
        async def login(body: Credentials, request: Request) -> JSONResponse:
            keys = self._limit_keys(request, body.username)
            self._refuse_when_limited(keys)
            try:
                user, token = await asyncio.to_thread(self.accounts.sign_in, body.username, body.password)
            except SignInFailed as exc:
                self._failed(keys)
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from None
            self._per_account.forget(keys[0])
            response = JSONResponse(user_payload(user))
            self._set_cookie(request, response, token)
            return response

        @public.post("/register")
        async def register(body: Registration, request: Request) -> JSONResponse:
            keys = self._limit_keys(request, body.username)
            self._refuse_when_limited(keys)
            try:
                user = await asyncio.to_thread(self.accounts.register, body.invite, body.username, body.password)
            except (AccountError, WeakPassword) as exc:
                self._failed(keys)
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
            response = JSONResponse(user_payload(user), status_code=status.HTTP_201_CREATED)
            self._set_cookie(request, response, self.accounts.open_session(user.id))
            return response

        @public.post("/logout")
        async def logout(request: Request) -> JSONResponse:
            token = request.cookies.get(COOKIE)
            if token:
                self.accounts.sign_out(token)
            response = JSONResponse({"ok": True})
            self._clear_cookie(response)
            return response

        @signed_in.get("/me")
        async def me(user: User = Depends(current_user)) -> JSONResponse:
            return JSONResponse(user_payload(user))

        @signed_in.post("/password")
        async def change_password(
            body: PasswordChange, request: Request, user: User = Depends(current_user)
        ) -> JSONResponse:
            token = request.cookies.get(COOKIE, "")
            try:
                await asyncio.to_thread(self.accounts.change_password, user, body.current, body.new, token=token)
            except (AccountError, WeakPassword) as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
            return JSONResponse({"ok": True})

        @admins.get("/users")
        async def list_users() -> JSONResponse:
            return JSONResponse([account.to_dict() for account in self.accounts.accounts()])

        @admins.patch("/users/{user_id}")
        async def update_user(user_id: str, body: UserUpdate, actor: User = Depends(admin)) -> JSONResponse:
            try:
                if body.role is not None:
                    self.accounts.set_role(actor, user_id, body.role)
                if body.disabled is not None:
                    self.accounts.set_disabled(actor, user_id, body.disabled)
            except AccountError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
            return JSONResponse({"ok": True})

        @admins.get("/invites")
        async def list_invites() -> JSONResponse:
            return JSONResponse([invite.to_dict() for invite in self.accounts.invites()])

        @admins.post("/invites")
        async def create_invite(body: InviteRequest, actor: User = Depends(admin)) -> JSONResponse:
            role: Role = body.role
            code, invite = self.accounts.create_invite(actor, role=role, ttl=body.days * 86400, note=body.note)
            return JSONResponse({**invite.to_dict(), "code": code}, status_code=status.HTTP_201_CREATED)

        @admins.delete("/invites/{invite_id}")
        async def revoke_invite(invite_id: str) -> JSONResponse:
            if not self.accounts.revoke_invite(invite_id):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No unused invite by that id")
            return JSONResponse({"ok": True})

        for router in (public, signed_in, admins):
            app.include_router(router)
