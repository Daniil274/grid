"""Who is asking: the user a request or a socket acts for.

The server asks an ``Identify`` for the user of every API request and socket,
and serves that user's space (web_chat.spaces). Identification is the only
thing that differs between a server for one person and a server for many:
the routes are the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from fastapi import Depends, HTTPException, status
from starlette.requests import HTTPConnection

Role = Literal["admin", "user"]
ROLES: tuple[Role, ...] = ("admin", "user")


@dataclass(frozen=True)
class User:
    """An identified user. ``id`` names the user's space and never changes."""

    id: str
    username: str
    role: Role

    @property
    def is_admin(self) -> bool:
        """Admins edit what all users share: the system configs and the accounts."""
        return self.role == "admin"


#: Resolves the user a connection acts for, or raises when there is none.
Identify = Callable[[HTTPConnection], Awaitable[User]]

DEFAULT_USER = "default_user"


def admins_only(current_user: Callable[..., Any]) -> Callable[..., Awaitable[User]]:
    """A dependency passing only admins, on top of *current_user*."""

    async def admin(user: User = Depends(current_user)) -> User:
        if not user.is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only")
        return user

    return admin


def single_user(user_id: str = DEFAULT_USER) -> Identify:
    """Every connection is the one local user, who owns the server."""
    user = User(id=user_id, username=user_id, role="admin")

    async def identify(connection: HTTPConnection) -> User:
        return user

    return identify
