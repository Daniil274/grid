"""Requests that change something must come from the chat's own pages.

A browser attaches the site's cookies to requests that other sites make it
send - a form posted by a hostile page, a websocket it opens. Such a request
would act as the signed-in user (cross-site request forgery). What the browser
does not let the other site forge is where the request comes from:

- ``Origin``: sent with every websocket handshake and every POST, PUT, PATCH
  or DELETE, cross-site or not;
- ``Sec-Fetch-Site``: ``same-origin`` only for the site's own requests.

So a websocket, or a request with a method other than GET/HEAD/OPTIONS, is
accepted only when its Origin is this server (the ``Host`` it was sent to) or
one listed in ``allowed_origins``; without an Origin, only when
``Sec-Fetch-Site`` says ``same-origin``. A client that is not a browser sends
``Origin`` itself. GET requests change nothing and pass.

The session cookie is also ``SameSite=Lax``, which keeps browsers from sending
it on cross-site subrequests at all; this check does not rely on that.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit

from fastapi import HTTPException, WebSocketException, status
from starlette.requests import HTTPConnection

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def normalize_origin(origin: str) -> str:
    """``scheme://host[:port]`` in lower case, as browsers send it."""
    parts = urlsplit(origin.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"Not an origin: {origin!r} (expected scheme://host[:port])")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


class OriginGuard:
    """Refuses state-changing requests and sockets from other sites."""

    def __init__(self, allowed_origins: Iterable[str] = ()) -> None:
        self._allowed = frozenset(normalize_origin(origin) for origin in allowed_origins)

    def allows(self, connection: HTTPConnection) -> bool:
        if connection.scope["type"] == "http" and connection.scope["method"] in SAFE_METHODS:
            return True
        headers = connection.headers
        origin = headers.get("origin")
        if origin is not None:
            try:
                origin = normalize_origin(origin)
            except ValueError:
                return False  # "null" (sandboxed pages, file://) and garbage
            host = (headers.get("host") or "").lower()
            return origin in self._allowed or (bool(host) and urlsplit(origin).netloc == host)
        return headers.get("sec-fetch-site") == "same-origin"

    def check(self, connection: HTTPConnection) -> None:
        """Raise the refusal fitting the connection unless it :meth:`allows` it."""
        if not self.allows(connection):
            raise refusal(connection, status.HTTP_403_FORBIDDEN, "Cross-site request refused")


def refusal(connection: HTTPConnection, http_status: int, detail: str) -> Exception:
    """The exception that refuses *connection*: an HTTP error, or a socket close."""
    if connection.scope["type"] == "websocket":
        return WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason=detail)
    return HTTPException(status_code=http_status, detail=detail)
