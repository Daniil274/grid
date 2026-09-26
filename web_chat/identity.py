"""Who is asking: the user a request or a socket acts for.

The server asks an ``Identify`` for the user of every API request and socket,
and serves that user's space (web_chat.spaces). Identification is the only
thing that differs between a server for one person and a server for many:
the routes are the same.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from starlette.requests import HTTPConnection

#: Resolves the user a connection acts for, or raises when there is none.
Identify = Callable[[HTTPConnection], Awaitable[str]]

DEFAULT_USER = "default_user"


def single_user(user_id: str = DEFAULT_USER) -> Identify:
    """Every connection is the one local user: the server has no accounts."""

    async def identify(connection: HTTPConnection) -> str:
        return user_id

    return identify
