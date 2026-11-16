"""Which requests this server answers, and which it refuses before acting.

Two checks stand here, both about a browser: which address it was sent to
(``Host``), and where it came from (``Origin``).

``Host`` is the address the client asked for. A page of another site that a
DNS record points at this server sends *its* name as ``Host``; so does a
request through a proxy that forwards the client's header untouched. A server
that echoes ``Host`` back - as its own origin, its cookies' domain, its links -
hands that other site the server's answers. So ``Host`` is accepted only when
it is one the operator configured (``TrustedHosts``), and only loopback names
are trusted without configuration.

``Origin`` is where the request came from. A browser attaches the site's
cookies to requests that other sites make it send - a form posted by a hostile
page, a websocket it opens. Such a request would act as the signed-in user
(cross-site request forgery). What the browser does not let the other site
forge is where the request comes from:

- ``Origin``: sent with every websocket handshake and every POST, PUT, PATCH
  or DELETE, cross-site or not;
- ``Sec-Fetch-Site``: ``same-origin`` only for the site's own requests.

So a websocket, or a request with a method other than GET/HEAD/OPTIONS, is
accepted only when its full Origin is one listed in ``allowed_origins``, or is
this server's own address (a trusted ``Host`` with the scheme the request
arrived over); without an Origin, only when ``Sec-Fetch-Site`` says
``same-origin``. A client that is not a browser sends ``Origin`` itself. GET
requests change nothing and pass the origin check.

The session cookie is also ``SameSite=Lax``, which keeps browsers from sending
it on cross-site subrequests at all; this check does not rely on that.
``Sec-Fetch-Site`` is a second barrier, never a replacement for the checks
above.
"""

from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, WebSocketException, status
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Names a browser may use for this machine. A page of another site cannot
#: have one of them: its own name is the one its DNS record points from.
LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})

#: Scheme a websocket's Origin carries: a page is http(s), the socket ws(s).
_ORIGIN_SCHEMES = {"ws": "http", "wss": "https"}

#: The port a browser leaves out of ``Host`` for a scheme: ``Host`` carries no
#: scheme, so with no scheme known an entry on either default port is matched.
DEFAULT_PORTS = {"http": "80", "https": "443", "ws": "80", "wss": "443"}


def normalize_origin(origin: str) -> str:
    """``scheme://host[:port]`` in lower case, as browsers send it."""
    parts = urlsplit(origin.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"Not an origin: {origin!r} (expected scheme://host[:port])")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _split_authority(authority: str) -> tuple[str, Optional[str]]:
    """``host`` and its ``port``, if written, from a Host header or a config entry."""
    authority = authority.strip().lower()
    if not authority:
        return "", None
    if authority.startswith("["):  # [::1]:8000
        host, _, rest = authority[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        return host, port or None
    if authority.count(":") == 1:
        host, port = authority.rsplit(":", 1)
        if port.isdigit():
            return host, port
    return authority, None


def _host_entry(value: str) -> str:
    host, port = _split_authority(value)
    if not host:
        raise ValueError(f"Not a host: {value!r} (expected host[:port])")
    return f"{host}:{port}" if port else host


class TrustedHosts:
    """The ``Host`` values this server answers to, besides loopback names.

    An entry without a port matches that name on any port; an entry with one
    matches that port - and the portless ``Host`` a browser sends for it, since
    a browser leaves the default port of the scheme out of ``Host``: the entry
    ``chat.example.com:443`` is matched by ``chat.example.com`` over ``https``
    (with no scheme known, either default port is accepted). Loopback names are
    always accepted - a browser of another site cannot present them.
    """

    def __init__(self, allowed: Iterable[str] = ()) -> None:
        self._allowed = frozenset(_host_entry(value) for value in allowed)

    def allows(self, host: Optional[str], scheme: Optional[str] = None) -> bool:
        """Whether *host* is one of this server's names, over *scheme* if known."""
        host = (host or "").strip().lower()
        if not host:
            return False
        name, port = _split_authority(host)
        if not name:
            return False
        if name in LOOPBACK_HOSTNAMES:
            return True
        if name in self._allowed:  # an entry without a port matches any port
            return True
        if port is not None:
            return f"{name}:{port}" in self._allowed
        # A browser leaves the default port out of Host, so an entry naming
        # this name on the default port of the request's scheme matches it.
        if scheme in DEFAULT_PORTS:
            defaults: Iterable[str] = (DEFAULT_PORTS[scheme],)
        else:
            defaults = tuple(set(DEFAULT_PORTS.values()))
        return any(f"{name}:{default}" in self._allowed for default in defaults)


class OriginGuard:
    """Refuses state-changing requests and sockets from other sites."""

    def __init__(
        self,
        allowed_origins: Iterable[str] = (),
        trusted_hosts: Iterable[str] = (),
    ) -> None:
        """``trusted_hosts``: the Host values to accept, besides loopback names.

        It defaults to no further names - only loopback names are answered - so
        a guard built without configuration still refuses a Host this server was
        never published under. A server reached under another name (a reverse
        proxy's public address, a tunnel) must name it here.
        """
        self._allowed = frozenset(normalize_origin(origin) for origin in allowed_origins)
        self._hosts = TrustedHosts(trusted_hosts)

    def allows(self, connection: HTTPConnection) -> bool:
        headers = connection.headers
        if not self._hosts.allows(headers.get("host"), connection.scope.get("scheme")):
            return False
        if connection.scope["type"] == "http" and connection.scope["method"] in SAFE_METHODS:
            return True
        origin = headers.get("origin")
        if origin is not None:
            try:
                origin = normalize_origin(origin)
            except ValueError:
                return False  # "null" (sandboxed pages, file://) and garbage
            if origin in self._allowed:
                return True
            return self._is_this_server(origin, connection)
        return headers.get("sec-fetch-site") == "same-origin"

    def _is_this_server(self, origin: str, connection: HTTPConnection) -> bool:
        """*origin* is a trusted Host, over the scheme the request arrived on."""
        host = (connection.headers.get("host") or "").strip().lower()
        if not host or urlsplit(origin).netloc != host:
            return False
        scheme = connection.scope.get("scheme")
        if not scheme:
            return True  # a connection double that does not say; the Host matched
        return urlsplit(origin).scheme == _ORIGIN_SCHEMES.get(scheme, scheme)

    def check(self, connection: HTTPConnection) -> None:
        """Raise the refusal fitting the connection unless it :meth:`allows` it."""
        if not self.allows(connection):
            raise refusal(connection, status.HTTP_403_FORBIDDEN, "Cross-site request refused")


class TrustedHostMiddleware:
    """Refuses HTTP requests and websocket handshakes sent to another host.

    A DNS record that points another site's name at this server, or a proxy
    that forwards the client's Host untrusted, would otherwise reach every
    route under a name the server never published. The check is on the real
    ASGI connection, so it covers pages and static files as well as the API.
    """

    def __init__(self, app, allowed: Iterable[str] = ()) -> None:
        self.app = app
        self.hosts = TrustedHosts(allowed)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in {"http", "websocket"}:
            host = Headers(scope=scope).get("host")
            if not self.hosts.allows(host, scope.get("scheme")):
                if scope["type"] == "websocket":
                    await send(
                        {
                            "type": "websocket.close",
                            "code": status.WS_1008_POLICY_VIOLATION,
                            "reason": "Host not trusted",
                        }
                    )
                else:
                    await PlainTextResponse("Host not trusted", status_code=status.HTTP_400_BAD_REQUEST)(
                        scope, receive, send
                    )
                return
        await self.app(scope, receive, send)


def refusal(connection: HTTPConnection, http_status: int, detail: str) -> Exception:
    """The exception that refuses *connection*: an HTTP error, or a socket close."""
    if connection.scope["type"] == "websocket":
        return WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason=detail)
    return HTTPException(status_code=http_status, detail=detail)


def same_site_only(
    allowed_origins: Iterable[str] = (),
    trusted_hosts: Iterable[str] = (),
):
    """An app-wide dependency applying :class:`OriginGuard` - for the local
    tools (timeline, context inspector) that have no sign-in of their own.

    With no *trusted_hosts* only loopback names are answered: these tools bind
    loopback, and one published under another name (a proxy, a tunnel) must
    name that address here, exactly as the web chat does.
    """
    guard = OriginGuard(allowed_origins, trusted_hosts=trusted_hosts)

    async def same_site(connection: HTTPConnection) -> None:
        guard.check(connection)

    return same_site
