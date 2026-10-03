import base64
import hashlib
import json
import urllib.parse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.credentials import CredentialError
from web_chat import chatgpt
from web_chat.chatgpt import ChatGPTError, ChatGPTLogin
from web_chat.vault import ReauthRequired

CLIENT = "oaiapp_test123"
NOW = 1_790_000_000.0
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwks():
    numbers = KEY.public_key().public_numbers()
    return {"keys": [{
        "kty": "RSA", "kid": "k1", "alg": "RS256",
        "n": b64(numbers.n.to_bytes(256, "big")), "e": b64(numbers.e.to_bytes(3, "big")),
    }]}


def id_token(*, nonce, aud=CLIENT, iss=chatgpt.ISSUER, exp=NOW + 3600, sign_with=KEY, kid="k1"):
    header = b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    payload = b64(json.dumps({"iss": iss, "aud": aud, "exp": exp, "nonce": nonce, "sub": "user-1", "email": "me@example.com"}).encode())
    signature = sign_with.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64(signature)}"


class OpenAI:
    """A stand-in for auth.openai.com that records what it is asked."""

    def __init__(self):
        self.requests = []
        self.token_reply = None
        self.token_status = 200
        self.nonce = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=jwks())
        if request.url.path.endswith("/revoke"):
            return httpx.Response(200)
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        if form["grant_type"] == "authorization_code" and self.token_reply is None:
            reply = {
                "access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600,
                "scope": chatgpt.SCOPES, "id_token": id_token(nonce=self.nonce),
            }
            return httpx.Response(self.token_status, json=reply)
        return httpx.Response(self.token_status, json=self.token_reply)


def login(server):
    return ChatGPTLogin("urn:uuid:host-1", transport=httpx.MockTransport(server), clock=lambda: NOW)


async def signed_in(server, **changes):
    flow = login(server)
    url = flow.begin("u1", chatgpt.loopback_redirect(8000))
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    server.nonce = query["nonce"]
    state = await flow.complete("u1", state=query["state"], code="the-code", issued_client_id=CLIENT, scope=chatgpt.SCOPES)
    return flow, state, query


def test_the_authorization_url_asks_for_a_dynamic_client_with_pkce_and_the_plan_scopes():
    flow = login(OpenAI())
    url = flow.begin("u1", "http://127.0.0.1:8000/auth/callback", login_hint="me@example.com")
    parts = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == chatgpt.AUTHORIZE_URL
    assert query["client_id"] == "dynamic_agent_client" and query["ext_agent_host_id"] == "urn:uuid:host-1"
    assert query["agent_name_hint"] == "Grid" and query["resource"] == "https://api.openai.com/v1"
    assert "chatgpt.tokens.use.direct" in query["scope"].split() and "offline_access" in query["scope"].split()
    assert query["redirect_uri"] == "http://127.0.0.1:8000/auth/callback" and query["login_hint"] == "me@example.com"
    verifier = flow._pending[query["state"]].verifier
    expected = b64(hashlib.sha256(verifier.encode()).digest())
    assert query["code_challenge"] == expected and query["code_challenge_method"] == "S256"


@pytest.mark.asyncio
async def test_completing_a_sign_in_exchanges_the_code_and_verifies_the_id_token():
    server = OpenAI()
    flow, state, query = await signed_in(server)
    form = dict(urllib.parse.parse_qsl(server.requests[0].content.decode()))
    assert form["grant_type"] == "authorization_code" and form["client_id"] == CLIENT
    assert form["redirect_uri"] == "http://127.0.0.1:8000/auth/callback" and form["resource"] == chatgpt.RESOURCE
    assert hashlib.sha256(form["code_verifier"].encode()).digest() == base64.urlsafe_b64decode(query["code_challenge"] + "==")
    assert (state["client_id"], state["subject"], state["email"]) == (CLIENT, "user-1", "me@example.com")
    assert state["expires_at"] == NOW + 3600 and state["host_id"] == "urn:uuid:host-1"
    # a sign-in is single use
    with pytest.raises(ChatGPTError, match="not started here"):
        await flow.complete("u1", state=query["state"], code="x", issued_client_id=CLIENT)


@pytest.mark.asyncio
async def test_a_sign_in_of_another_user_an_unknown_state_or_an_unissued_client_is_refused():
    server = OpenAI()
    flow = login(server)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(flow.begin("u1", chatgpt.loopback_redirect(1))).query))
    with pytest.raises(ChatGPTError, match="not started here"):
        await flow.complete("u2", state=query["state"], code="c", issued_client_id=CLIENT)
    with pytest.raises(ChatGPTError, match="not started here"):
        await flow.complete("u1", state="forged", code="c", issued_client_id=CLIENT)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(flow.begin("u1", chatgpt.loopback_redirect(1))).query))
    with pytest.raises(ChatGPTError, match="did not issue"):
        await flow.complete("u1", state=query["state"], code="c", issued_client_id="dynamic_agent_client")
    assert not any(r.url.path.endswith("/token") for r in server.requests)


@pytest.mark.asyncio
async def test_a_sign_in_that_took_too_long_is_forgotten():
    clock = [NOW]
    flow = ChatGPTLogin("h", transport=httpx.MockTransport(OpenAI()), clock=lambda: clock[0])
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(flow.begin("u1", chatgpt.loopback_redirect(1))).query))
    clock[0] += chatgpt.TRANSACTION_TTL + 1
    with pytest.raises(ChatGPTError, match="took too long"):
        await flow.complete("u1", state=query["state"], code="c", issued_client_id=CLIENT)


@pytest.mark.asyncio
async def test_without_the_plan_scope_nothing_is_stored():
    server = OpenAI()
    server.token_reply = {"access_token": "a", "refresh_token": "r", "id_token": "x", "scope": "openid email"}
    flow = login(server)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(flow.begin("u1", chatgpt.loopback_redirect(1))).query))
    with pytest.raises(ChatGPTError, match="Plus and Pro"):
        await flow.complete("u1", state=query["state"], code="c", issued_client_id=CLIENT)


@pytest.mark.parametrize("fault", ["nonce", "aud", "iss", "expired", "signature", "kid"])
@pytest.mark.asyncio
async def test_a_bad_id_token_is_refused(fault):
    server = OpenAI()
    flow = login(server)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(flow.begin("u1", chatgpt.loopback_redirect(1))).query))
    options = {
        "nonce": dict(nonce="someone-elses"), "aud": dict(nonce=query["nonce"], aud="oaiapp_other"),
        "iss": dict(nonce=query["nonce"], iss="https://evil.test"), "expired": dict(nonce=query["nonce"], exp=NOW - 7200),
        "signature": dict(nonce=query["nonce"], sign_with=rsa.generate_private_key(public_exponent=65537, key_size=2048)),
        "kid": dict(nonce=query["nonce"], kid="unknown"),
    }[fault]
    server.token_reply = {
        "access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": chatgpt.SCOPES, "id_token": id_token(**options),
    }
    with pytest.raises(ChatGPTError):
        await flow.complete("u1", state=query["state"], code="c", issued_client_id=CLIENT)


@pytest.mark.asyncio
async def test_a_fresh_access_token_is_the_credential_and_marks_a_subscription_call():
    flow, state, _ = await signed_in(OpenAI())
    found, renewed = await flow.credential(state, NOW + 100)
    assert renewed is None
    assert (found.secret, found.source, found.charged, found.subscription) == ("access-1", "chatgpt", False, True)


@pytest.mark.asyncio
async def test_a_due_access_token_is_refreshed_and_the_rotated_refresh_token_kept():
    server = OpenAI()
    flow, state, _ = await signed_in(server)
    server.token_reply = {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 1800}
    found, renewed = await flow.credential(state, NOW + 3600)
    form = dict(urllib.parse.parse_qsl(server.requests[-1].content.decode()))
    assert form == {"grant_type": "refresh_token", "client_id": CLIENT, "refresh_token": "refresh-1", "resource": chatgpt.RESOURCE}
    assert found.secret == "access-2" and renewed["refresh_token"] == "refresh-2"
    assert renewed["expires_at"] == NOW + 3600 + 1800 and renewed["id_token"] == state["id_token"]


@pytest.mark.parametrize("error", ["invalid_grant", "token_expired", "refresh_token_reused"])
@pytest.mark.asyncio
async def test_a_login_that_cannot_be_refreshed_asks_for_a_new_sign_in(error):
    server = OpenAI()
    flow, state, _ = await signed_in(server)
    server.token_status, server.token_reply = 400, {"error": error}
    with pytest.raises(ReauthRequired):
        await flow.credential(state, NOW + 7200)


@pytest.mark.asyncio
async def test_a_refresh_that_fails_for_other_reasons_is_an_error_not_a_lost_login():
    server = OpenAI()
    flow, state, _ = await signed_in(server)
    server.token_status, server.token_reply = 503, {"error": "temporarily_unavailable"}
    with pytest.raises(CredentialError, match="not available right now"):
        await flow.credential(state, NOW + 7200)
    with pytest.raises(ReauthRequired):
        await flow.credential({**state, "scopes": ["openid"]}, NOW)


@pytest.mark.asyncio
async def test_disconnecting_revokes_the_refresh_token_and_never_fails():
    server = OpenAI()
    flow, state, _ = await signed_in(server)
    await flow.disconnect(state)
    form = dict(urllib.parse.parse_qsl(server.requests[-1].content.decode()))
    assert form == {"token": "refresh-1", "token_type_hint": "refresh_token", "client_id": CLIENT}

    def down(request):
        raise httpx.ConnectError("offline")

    await ChatGPTLogin("h", transport=httpx.MockTransport(down)).disconnect(state)
