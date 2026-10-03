"""Whose key a model call is made with.

A provider's key used to be read from the process environment at the moment a
client was built, which suits one operator and no one else. Here a
:class:`CredentialSource` answers per *request* - the key of the pool the user's
tier draws on, a key the user stored, a subscription's access token - and
:class:`ManagedAuth` puts it on the request as it leaves. A client therefore
never holds a secret, and a changed tier, a rotated key or a refreshed token
applies at once, without rebuilding any agent.

A source that has no credential for a provider raises :class:`CredentialError`.
It never falls through to someone else's: a user with no key of their own is
told to add one, not billed to the operator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Mapping, Optional, Protocol

import httpx

from utils.exceptions import AgentError


class CredentialError(AgentError):
    """No credential may be used for this provider, now."""


@dataclass(frozen=True)
class Credential:
    """What authorizes one request, and who pays for it."""

    secret: str = field(repr=False)
    #: Where it came from: ``env``, ``pool:<name>``, ``own`` or ``chatgpt``.
    source: str = "env"
    #: Whether the operator pays for calls made with it (budgets count only those).
    charged: bool = True
    #: Whether it spends a subscription's allowance rather than money.
    subscription: bool = False
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


class CredentialSource(Protocol):
    def available(self, provider: Any) -> bool:
        """Whether a credential for *provider* (a ProviderConfig) exists now."""

    async def credential(self, provider: Any) -> Credential:
        """The credential for a request to *provider*; CredentialError when none."""


class EnvCredentials:
    """The operator's keys: ``api_key`` in the config, else the ``api_key_env`` variable."""

    def __init__(self, environ: Optional[Mapping[str, str]] = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def _secret(self, provider: Any) -> Optional[str]:
        if getattr(provider, "auth", "api_key") == "chatgpt":
            return None  # a plan login is not an environment variable
        if getattr(provider, "api_key", None):
            return provider.api_key
        name = getattr(provider, "api_key_env", None)
        return (self._environ.get(name) or None) if name else None

    def available(self, provider: Any) -> bool:
        return self._secret(provider) is not None

    async def credential(self, provider: Any) -> Credential:
        secret = self._secret(provider)
        if secret is None:
            if getattr(provider, "auth", "api_key") == "chatgpt":
                raise CredentialError(
                    f"Provider '{getattr(provider, 'name', '')}' uses a ChatGPT plan: sign in with ChatGPT first.",
                    details={"provider": getattr(provider, "name", "")},
                )
            raise CredentialError(
                f"API key not found for provider '{getattr(provider, 'name', '')}'",
                details={"provider": getattr(provider, "name", ""), "env_var": getattr(provider, "api_key_env", None)},
            )
        return Credential(secret, "env")


class ManagedAuth(httpx.Auth):
    """Signs each request with the credential the source gives for *provider*."""

    requires_request_body = False

    def __init__(self, source: CredentialSource, provider: Any) -> None:
        self._source = source
        self._provider = provider

    async def async_auth_flow(self, request: httpx.Request) -> AsyncIterator[httpx.Request]:
        credential = await self._source.credential(self._provider)
        request.headers["Authorization"] = f"Bearer {credential.secret}"
        for name, value in credential.headers.items():
            request.headers[name] = value
        # The metering transport reads who paid for the call from here.
        request.extensions["grid_credential"] = credential
        yield request


def pool_environment(pool: Mapping[str, str], environ: Optional[Mapping[str, str]] = None) -> Dict[str, Optional[str]]:
    """What each variable a provider asks for is replaced by in *pool*, resolved."""
    environ = environ if environ is not None else os.environ
    return {name: (environ.get(replacement) or None) for name, replacement in pool.items()}


#: Providers a user may bring a key for, by where their API is: a key stored for
#: one is only ever sent to that address, whatever a system's config calls the
#: provider or points it at.
PROVIDER_PRESETS: Dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "opencode-go": "https://opencode.ai/zen/go/v1",
}


def preset_for(base_url: str) -> Optional[str]:
    """The preset whose API *base_url* is, or None for any other address."""
    wanted = base_url.rstrip("/").lower()
    for name, url in PROVIDER_PRESETS.items():
        if url.rstrip("/").lower() == wanted:
            return name
    return None
