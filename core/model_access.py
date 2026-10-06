"""How an agent reaches its models: the credentials it signs with, the spend it reports.

One :class:`ModelAccess` serves every client a factory builds. Left default, a
client is the plain one it always was: the provider's key from the environment,
read when the client is built, and nothing measured. Given a credential source
or a spend sink, the client becomes *managed*: it signs each request with the
source's credential (core.credentials) and reports what every call cost
(core.metering, core.pricing) to the sink. A server with accounts always runs
managed, per user.
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional

import httpx
from openai import AsyncOpenAI

from core.credentials import CredentialError, CredentialSource, EnvCredentials, ManagedAuth
from core.metering import MeteredCall, MeteredTransport
from core.pricing import Cost, Price, PriceBook, TokenUsage, charge
from utils.exceptions import AgentError

logger = logging.getLogger(__name__)

#: The client's own ``api_key``: never sent, ManagedAuth replaces the header.
MANAGED_KEY = "managed-by-grid"


@dataclass(frozen=True)
class SpendEvent:
    """What one finished model call cost, and who bore it."""

    provider: str
    model: str
    usage: TokenUsage
    cost: Cost
    source: str
    charged: bool
    subscription: bool


SpendSink = Callable[[SpendEvent], None]


@dataclass
class SpendTally:
    """What the model calls made inside one :func:`tally_spend` scope cost."""

    calls: int = 0
    micro: int = 0
    #: Calls on a subscription: priced as the API would, nothing billed.
    subscription_calls: int = 0

    def add(self, event: SpendEvent) -> None:
        self.calls += 1
        self.micro += event.cost.micro
        self.subscription_calls += event.subscription


_tallies: contextvars.ContextVar[tuple[SpendTally, ...]] = contextvars.ContextVar(
    "_spend_tallies", default=()
)


@contextmanager
def tally_spend() -> Iterator[SpendTally]:
    """Count the spend of every model call made in this context from here on.

    Tasks started inside inherit the scope - the agents and compactions a run
    starts count toward it - and scopes nest: a call counts in each one open.
    Only managed clients see their spend (ModelAccess.managed), so outside one
    the tally stays empty.
    """
    tally = SpendTally()
    token = _tallies.set((*_tallies.get(), tally))
    try:
        yield tally
    finally:
        _tallies.reset(token)
#: Whether a model, by config key and by name, may be used: a plan's model list.
ModelFilter = Callable[[str, str], bool]


class ModelNotInPlan(AgentError):
    """The model is not one the user's plan includes."""


class ModelAccess:
    """Credentials in, spend out, for the clients of one factory."""

    def __init__(
        self,
        credentials: Optional[CredentialSource] = None,
        *,
        on_spend: Optional[SpendSink] = None,
        prices: Optional[PriceBook] = None,
        unknown_price: Optional[Price] = None,
        permits: Optional[ModelFilter] = None,
        signature: Optional[Callable[[], str]] = None,
    ) -> None:
        self._permits = permits
        self._signature = signature
        self._credentials = credentials
        self._on_spend = on_spend
        self._prices = prices or PriceBook()
        self._unknown_price = unknown_price

    @property
    def managed(self) -> bool:
        return self._credentials is not None or self._on_spend is not None

    @property
    def credentials(self) -> CredentialSource:
        return self._credentials or EnvCredentials()

    # -- models -----------------------------------------------------------------
    def check_model(self, config: Any, model_key: str) -> None:
        """Raise ModelNotInPlan unless the user's plan includes *model_key*."""
        if self._permits is None:
            return
        name = config.get_model(model_key).name
        if not self._permits(model_key, name):
            raise ModelNotInPlan(
                f"The model '{model_key}' is not included in your plan.",
                details={"model": model_key},
            )

    def signature(self) -> str:
        """Changes when what the user may use changes; agents built before it are rebuilt."""
        return self._signature() if self._signature is not None else ""

    # -- credentials ------------------------------------------------------------
    def api_key(self, config: Any, provider_key: str) -> str:
        """The key to build a client with; AgentError when the provider has none for this user.

        A managed client holds only a placeholder: the real credential is chosen
        per request.
        """
        provider = config.get_provider(provider_key)
        if not self.managed:
            if getattr(provider, "auth", "api_key") == "chatgpt":
                raise CredentialError(
                    f"Provider '{provider_key}' uses a ChatGPT plan, which needs a signed-in user: "
                    "run it in the web chat and sign in with ChatGPT.",
                    details={"provider": provider_key},
                )
            key = config.get_api_key(provider_key)
            if not key:
                raise AgentError(
                    f"API key not found for provider '{provider_key}'",
                    details={"provider": provider_key, "env_var": provider.api_key_env},
                )
            return key
        if not self.credentials.available(provider):
            raise CredentialError(
                f"No credential for provider '{provider_key}'. Add your own key in the settings, "
                "or ask an admin to change your plan.",
                details={"provider": provider_key},
            )
        return MANAGED_KEY

    # -- clients ----------------------------------------------------------------
    def client(
        self,
        config: Any,
        *,
        api_key: str,
        base_url: str,
        timeout: int = 30,
        max_retries: int = 2,
        provider_key: Optional[str] = None,
    ) -> AsyncOpenAI:
        """An AsyncOpenAI client for a provider; avoids env proxies, uses the configured one."""
        kwargs: Dict[str, Any] = dict(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)
        provider = config.get_provider(provider_key) if provider_key else None
        if provider is not None and provider.default_headers:
            kwargs["default_headers"] = dict(provider.default_headers)
        proxy_url = config.get_proxy_for_provider(provider_key)
        if not self.managed:
            # We control proxy selection explicitly; disable env proxy usage in httpx.
            kwargs["http_client"] = httpx.AsyncClient(proxy=proxy_url or None, timeout=float(timeout), trust_env=False)
            return AsyncOpenAI(**kwargs)
        transport: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport(proxy=proxy_url or None, trust_env=False)
        if self._on_spend is not None:
            transport = MeteredTransport(
                transport, provider_key or "", lambda call: self._spend(config, provider_key, call)
            )
        kwargs["http_client"] = httpx.AsyncClient(
            transport=transport,
            auth=ManagedAuth(self.credentials, provider) if provider is not None else None,
            timeout=float(timeout),
            trust_env=False,
        )
        return AsyncOpenAI(**kwargs)

    # -- spend ------------------------------------------------------------------
    def _spend(self, config: Any, provider_key: Optional[str], call: MeteredCall) -> None:
        provider = config.get_provider(provider_key) if provider_key else None
        usage = TokenUsage.from_payload(call.usage)
        price = self._price(config, provider_key, provider, call.model)
        cost = charge(
            usage,
            reported_usd=call.usage.get("cost"),
            price=price,
            fallback=self._unknown_price,
            subscription=call.subscription,
        )
        if not (usage.total or cost.micro):
            return
        assert self._on_spend is not None
        event = SpendEvent(provider_key or "", call.model, usage, cost, call.source, call.charged, call.subscription)
        for tally in _tallies.get():
            tally.add(event)
        self._on_spend(event)

    def _price(self, config: Any, provider_key: Optional[str], provider: Any, model: str) -> Optional[Price]:
        for candidate in config.config.models.values():
            declared = getattr(candidate, "price", None)
            if declared and candidate.provider == provider_key and candidate.name == model:
                found = Price.from_mapping(declared)
                if found is not None:
                    return found
        if provider is None:
            return None
        return self._prices.lookup(provider.base_url, model, getattr(provider, "price_source", None))
