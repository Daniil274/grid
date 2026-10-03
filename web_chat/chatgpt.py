"""Sign in with ChatGPT: a user's ChatGPT plan as the model provider.

OpenAI lets an open-source app that runs on the user's own machine spend the
user's ChatGPT Plus or Pro allowance on Responses API calls
(https://developers.openai.com/siwc/token-sharing-open-source). This module is
that flow:

1. :meth:`ChatGPTLogin.begin` builds the authorization URL - Authorization Code
   with PKCE and OpenID Connect, a *dynamic* client (``dynamic_agent_client``
   plus a stable ``ext_agent_host_id``) and the scopes that ask for the plan.
   The callback is an HTTP loopback address, ``http://127.0.0.1:PORT/auth/callback``:
   the flow exists for locally hosted apps, so a Grid server on a remote host
   cannot use it. OpenAI offers plan usage to remotely hosted apps only to
   approved partners (the interest form linked from the page above).
2. :meth:`ChatGPTLogin.complete` exchanges the code, checks the granted scopes
   and verifies the ID token (signature against OpenAI's JWKS, issuer, audience,
   expiry, nonce), and returns the state to keep: the issued ``client_id``, the
   tokens and when the access token expires.
3. :meth:`ChatGPTLogin.credential` turns that state into the credential that
   signs a request - refreshing the access token (an hour) first when due - so
   it plugs into web_chat.vault as an :class:`~web_chat.vault.OAuthKind`.

Requests then go to ``POST https://api.openai.com/v1/responses`` with
``store: false`` and ``stream: true`` (core.factory.models shapes them for a
provider with ``auth: chatgpt``). Calls spend the plan's allowance, not money.

``python -m web_chat.chatgpt login|check|logout`` runs the sign-in without the
web chat and checks that the plan answers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.credentials import Credential, CredentialError
from web_chat.vault import ReauthRequired

logger = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
#: The documentation names "the revocation endpoint" without its path; this is the
#: conventional one next to the token endpoint. Revocation is best effort.
REVOKE_URL = f"{ISSUER}/api/accounts/oauth/revoke"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT = "dynamic_agent_client"
CALLBACK_PATH = "/auth/callback"
#: A started sign-in that is not finished within this long is forgotten.
TRANSACTION_TTL = 600
#: An access token this close to its expiry is refreshed first.
REFRESH_MARGIN = 60
JWKS_TTL = 3600
CLOCK_SKEW = 60

#: Refresh failures after which the stored login is of no use: sign in again.
UNUSABLE_GRANT_ERRORS = {"invalid_grant", "token_expired", "refresh_token_reused"}


class ChatGPTError(Exception):
    """The sign-in failed; the message is safe to show the user."""


def loopback_redirect(port: int) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) for one sign-in."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_host_id() -> str:
    """A stable identifier of this installation, for ``ext_agent_host_id`` (RFC-style URN)."""
    return f"urn:uuid:{uuid.uuid4()}"


# -- ID token -----------------------------------------------------------------------


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def verify_id_token(
    token: str, jwks: Mapping[str, Any], *, audience: str, nonce: str, now: float
) -> Dict[str, Any]:
    """The claims of an ID token OpenAI signed for this client, or ChatGPTError.

    Checks the RS256 signature against *jwks*, then issuer, audience, expiry and nonce.
    """
    try:
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_b64url(header_part))
        claims = json.loads(_b64url(payload_part))
        signature = _b64url(signature_part)
    except (ValueError, TypeError):
        raise ChatGPTError("OpenAI returned an ID token that could not be read.") from None
    if header.get("alg") != "RS256":
        raise ChatGPTError("The ID token is signed with an algorithm Grid does not accept.")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid") and k.get("kty") == "RSA"), None)
    if key is None:
        raise ChatGPTError("The ID token was signed with a key OpenAI does not publish.")
    public = rsa.RSAPublicNumbers(
        int.from_bytes(_b64url(key["e"]), "big"), int.from_bytes(_b64url(key["n"]), "big")
    ).public_key()
    try:
        public.verify(signature, f"{header_part}.{payload_part}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise ChatGPTError("The ID token's signature is not valid.") from None
    audiences = claims.get("aud")
    audiences = [audiences] if isinstance(audiences, str) else list(audiences or [])
    if claims.get("iss") != ISSUER:
        raise ChatGPTError("The ID token was not issued by OpenAI.")
    if audience not in audiences:
        raise ChatGPTError("The ID token is for a different client.")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] + CLOCK_SKEW < now:
        raise ChatGPTError("The ID token has expired.")
    if claims.get("nonce") != nonce:
        raise ChatGPTError("The ID token does not answer this sign-in attempt.")
    if not claims.get("sub"):
        raise ChatGPTError("The ID token names no account.")
    return claims


# -- the flow -----------------------------------------------------------------------


@dataclass
class Transaction:
    """A sign-in that was started and not yet finished."""

    user_id: str
    verifier: str
    nonce: str
    redirect_uri: str
    expires_at: float


class ChatGPTLogin:
    """The sign-in and token handling of Sign in with ChatGPT (also an OAuthKind)."""

    def __init__(
        self,
        host_id: str,
        *,
        agent_name: str = "Grid",
        transport: Optional[httpx.AsyncBaseTransport] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._host_id = host_id
        self._agent_name = agent_name
        self._transport = transport
        self._clock = clock
        self._pending: Dict[str, Transaction] = {}
        self._jwks: Optional[tuple[float, Mapping[str, Any]]] = None

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False, transport=self._transport)

    # -- step 1 -------------------------------------------------------------------
    def begin(self, user_id: str, redirect_uri: str, *, login_hint: Optional[str] = None) -> str:
        """The URL to send the user's browser to; the sign-in finishes at *redirect_uri*."""
        now = self._clock()
        self._pending = {state: t for state, t in self._pending.items() if t.expires_at > now}
        verifier, challenge = pkce_pair()
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._pending[state] = Transaction(user_id, verifier, nonce, redirect_uri, now + TRANSACTION_TTL)
        query = {
            "response_type": "code",
            "client_id": DYNAMIC_CLIENT,
            "agent_name_hint": self._agent_name,
            "ext_agent_host_id": self._host_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if login_hint:
            query["login_hint"] = login_hint
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query)}"

    # -- step 2 -------------------------------------------------------------------
    async def complete(
        self, user_id: str, *, state: str, code: str, issued_client_id: str, scope: str = ""
    ) -> Dict[str, Any]:
        """Finish the sign-in the callback reports; the state to store for the user."""
        transaction = self._pending.pop(state, None)
        if transaction is None or transaction.expires_at <= self._clock() or transaction.user_id != user_id:
            raise ChatGPTError("This sign-in was not started here, or took too long. Start it again.")
        if not issued_client_id or issued_client_id == DYNAMIC_CLIENT:
            raise ChatGPTError("OpenAI did not issue a client for this sign-in.")
        tokens = await self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": issued_client_id,
                "code": code,
                "code_verifier": transaction.verifier,
                "redirect_uri": transaction.redirect_uri,
                "resource": RESOURCE,
            }
        )
        scopes = (tokens.get("scope") or scope or "").split()
        if PLAN_SCOPE not in scopes:
            raise ChatGPTError(
                "OpenAI did not grant the use of your ChatGPT plan. It is offered to Plus and Pro "
                "subscribers; check the account you signed in with."
            )
        for field in ("access_token", "refresh_token", "id_token"):
            if not tokens.get(field):
                raise ChatGPTError(f"OpenAI's answer has no {field.replace('_', ' ')}.")
        claims = verify_id_token(
            tokens["id_token"], await self._keys(), audience=issued_client_id, nonce=transaction.nonce, now=self._clock()
        )
        return {
            "client_id": issued_client_id,
            "subject": claims["sub"],
            "email": claims.get("email", ""),
            "id_token": tokens["id_token"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": self._clock() + float(tokens.get("expires_in") or 3600),
            "scopes": scopes,
            "host_id": self._host_id,
        }

    # -- step 3: the OAuthKind ------------------------------------------------------
    async def credential(self, state: Dict[str, Any], now: float) -> tuple[Credential, Optional[Dict[str, Any]]]:
        if PLAN_SCOPE not in state.get("scopes", []):
            raise ReauthRequired()
        renewed: Optional[Dict[str, Any]] = None
        if state["expires_at"] - REFRESH_MARGIN <= now:
            renewed = await self._refresh(state, now)
            state = renewed
        return Credential(state["access_token"], "chatgpt", charged=False, subscription=True), renewed

    async def _refresh(self, state: Dict[str, Any], now: float) -> Dict[str, Any]:
        tokens = await self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": state["client_id"],
                "refresh_token": state["refresh_token"],
                "resource": RESOURCE,
            },
            refreshing=True,
        )
        if not tokens.get("access_token"):
            raise CredentialError("OpenAI did not return a new access token for your ChatGPT sign-in.")
        return {
            **state,
            "access_token": tokens["access_token"],
            # A refresh token may be single use: keep the one that came back.
            "refresh_token": tokens.get("refresh_token") or state["refresh_token"],
            "id_token": tokens.get("id_token") or state["id_token"],
            "expires_at": now + float(tokens.get("expires_in") or 3600),
        }

    async def _token_request(self, form: Mapping[str, str], *, refreshing: bool = False) -> Dict[str, Any]:
        try:
            async with self._http() as http:
                response = await http.post(TOKEN_URL, data=form)
        except httpx.HTTPError as exc:
            raise self._unavailable(refreshing, f"could not reach OpenAI ({type(exc).__name__})") from None
        if response.is_success:
            try:
                return response.json()
            except ValueError:
                raise self._unavailable(refreshing, "OpenAI's answer was not readable") from None
        error = ""
        try:
            error = str(response.json().get("error", ""))
        except (ValueError, AttributeError):
            pass
        if refreshing and error in UNUSABLE_GRANT_ERRORS:
            raise ReauthRequired()
        if error == "invalid_client":
            raise ChatGPTError("OpenAI no longer accepts this app's registration. Sign in with ChatGPT again.")
        raise self._unavailable(refreshing, f"OpenAI answered {response.status_code} {error}".strip())

    @staticmethod
    def _unavailable(refreshing: bool, why: str) -> Exception:
        message = f"ChatGPT sign-in is not available right now: {why}."
        return CredentialError(message) if refreshing else ChatGPTError(message)

    async def _keys(self) -> Mapping[str, Any]:
        now = self._clock()
        if self._jwks is None or self._jwks[0] + JWKS_TTL <= now:
            try:
                async with self._http() as http:
                    response = await http.get(JWKS_URL)
                response.raise_for_status()
                self._jwks = (now, response.json())
            except (httpx.HTTPError, ValueError):
                raise ChatGPTError("Could not fetch OpenAI's signing keys to check the sign-in.") from None
        return self._jwks[1]

    # -- sign out -------------------------------------------------------------------
    async def disconnect(self, state: Mapping[str, Any]) -> None:
        """Revoke the login at OpenAI, best effort: the stored copy is deleted either way."""
        try:
            async with self._http() as http:
                await http.post(
                    REVOKE_URL,
                    data={
                        "token": state["refresh_token"],
                        "token_type_hint": "refresh_token",
                        "client_id": state["client_id"],
                    },
                )
        except (httpx.HTTPError, KeyError):
            logger.warning("Could not revoke the ChatGPT login at OpenAI; it is deleted here", exc_info=True)


def persistent_host_id(path: "Path") -> str:
    """The installation's ``ext_agent_host_id``: made once, kept in *path*, never changed."""
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    made = new_host_id()
    path.write_text(made + "\n", encoding="utf-8")
    return made


# -- command line -----------------------------------------------------------------------


def _local_state(args: Any) -> tuple[Any, ChatGPTLogin]:
    """The vault and login of the one-user server, found as that server finds them."""
    from web_chat.deployment import Deployment
    from web_chat.vault import local_vault

    deployment = Deployment(config_path=args.config, routing_path=args.routing)
    records = Path(deployment.config.get_logs_directory())
    login = ChatGPTLogin(persistent_host_id(records / "chatgpt_host_id"))
    return local_vault(records, oauth={"chatgpt": login}), login


def _wait_for_callback(port: int, timeout: float) -> Dict[str, str]:
    """Serve ``/auth/callback`` once on 127.0.0.1:*port*; the query it was called with."""
    import http.server
    import threading

    received: Dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            parts = urllib.parse.urlparse(self.path)
            if parts.path != CALLBACK_PATH:
                self.send_error(404)
                return
            received.update({k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and return to the terminal.")
            done.set()

        def log_message(self, *args: Any) -> None:  # keep the codes out of the terminal
            return

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while not done.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return received


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import asyncio
    import sys
    import webbrowser

    parser = argparse.ArgumentParser(prog="python -m web_chat.chatgpt", description="Use your ChatGPT plan in Grid")
    parser.add_argument("--config", "-c", default=None, help="The single system the one-user server runs")
    parser.add_argument("--routing", default="routing.yaml", help="The catalog it routes across")
    parser.add_argument("--user-id", default="default_user")
    commands = parser.add_subparsers(dest="command", required=True)
    login_cmd = commands.add_parser("login", help="Sign in with ChatGPT in your browser")
    login_cmd.add_argument("--port", type=int, default=1455)
    check_cmd = commands.add_parser("check", help="Ask your plan which models it offers and make one tiny request")
    check_cmd.add_argument("--model", default=None, help="A model slug (default: the first listed)")
    commands.add_parser("logout", help="Revoke the sign-in and delete it")
    args = parser.parse_args(argv)
    vault, login = _local_state(args)

    async def run() -> int:
        if args.command == "login":
            url = login.begin(args.user_id, loopback_redirect(args.port))
            print("Open this address and sign in (it opens by itself if it can):\n\n  " + url + "\n")
            webbrowser.open(url)
            query = await asyncio.to_thread(_wait_for_callback, args.port, TRANSACTION_TTL)
            if query.get("error") or not query.get("code"):
                print("error: " + (query.get("error_description") or query.get("error") or "no answer from the browser"), file=sys.stderr)
                return 1
            try:
                kept = await login.complete(
                    args.user_id, state=query.get("state", ""), code=query["code"],
                    issued_client_id=query.get("client_id", ""), scope=query.get("scope", ""),
                )
            except ChatGPTError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            vault.put_state(args.user_id, "chatgpt", kept, hint=kept.get("email", ""))
            print(f"Signed in as {kept['email'] or kept['subject']}. Run 'check' to try your plan.")
            return 0
        if args.command == "logout":
            kept = vault.state(args.user_id, "chatgpt")
            if kept is None:
                print("Not signed in.")
                return 0
            await login.disconnect(kept)
            vault.delete(args.user_id, "chatgpt")
            print("Signed out.")
            return 0
        return await _check(vault, args)

    return asyncio.run(run())


async def _check(vault: Any, args: Any) -> int:
    """List the plan's models and make one small streamed request, printing what comes back."""
    import sys

    found = await vault.credential(args.user_id, "chatgpt")
    if found is None:
        print("Not signed in, or the sign-in expired: run 'login'.", file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bearer {found.secret}"}
    async with httpx.AsyncClient(timeout=60, trust_env=False) as http:
        listing = await http.get(f"{RESOURCE}/models", headers=headers)
        print(f"GET /models -> {listing.status_code}")
        slugs: list[str] = []
        if listing.is_success:
            for entry in listing.json().get("models", []):
                if entry.get("visibility") == "list":
                    slugs.append(entry["slug"])
                    print(f"  {entry['slug']}  {entry.get('display_name', '')}")
        model = args.model or (slugs[0] if slugs else None)
        if model is None:
            print("No model to try.", file=sys.stderr)
            return 1
        body = {"model": model, "input": "Reply with the single word OK.", "store": False, "stream": True}
        print(f"POST /responses ({model}) ...")
        async with http.stream("POST", f"{RESOURCE}/responses", headers=headers, json=body) as response:
            print(f"  status {response.status_code}")
            if not response.is_success:
                print("  " + (await response.aread()).decode(errors="replace")[:500])
                return 1
            outcome = None
            async for line in response.aiter_lines():
                if line.startswith("data:") and ("response.completed" in line or "response.failed" in line):
                    outcome = line[5:].strip()
        if outcome is None:
            print("  the stream ended without response.completed or response.failed", file=sys.stderr)
            return 1
        event = json.loads(outcome)
        print(f"  {event['type']}")
        if event["type"] == "response.failed":
            print("  " + json.dumps(event.get("response", {}).get("error"), indent=2))
            return 1
        print("  usage: " + json.dumps(event["response"].get("usage")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
