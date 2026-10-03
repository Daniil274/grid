"""What a model call costs, in dollars.

A call's cost comes from the best source there is, and is labelled with it:

``reported``      the provider said what it charged (OpenRouter's ``usage.cost``)
``computed``      tokens times the model's price (:class:`Price`)
``estimated``     the model has no price; the operator's ``unknown_price`` stands in,
                  so a budget cannot be dodged by a model nobody priced
``subscription``  the call ran on a subscription, which bills nothing; the cost is
                  what the API would have charged, for analytics only

Money is kept as whole micro-dollars (``1_000_000`` to the dollar) so sums are
exact. A token at $1 per million tokens is exactly one micro-dollar.

Prices are dollars per million tokens, the unit providers publish. They come from
the model's own ``price:`` in the config, else from a :class:`PriceBook` loaded from
a models.dev snapshot (``python -m web_chat.prices refresh``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MICRO = 1_000_000

REPORTED = "reported"
COMPUTED = "computed"
ESTIMATED = "estimated"
SUBSCRIPTION = "subscription"


def to_micro(usd: Any) -> Optional[int]:
    """Whole micro-dollars of a dollar amount; None when it is not a usable amount."""
    if usd is None or isinstance(usd, bool):
        return None
    try:
        amount = Decimal(str(usd))
    except Exception:  # noqa: BLE001 - a provider's odd value is "not reported"
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * MICRO).to_integral_value(rounding=ROUND_HALF_UP))


def format_usd(micro: int) -> str:
    """``$0.0042``-style text, with enough digits for small amounts."""
    usd = Decimal(micro) / MICRO
    if usd >= 1:
        return f"${usd:,.2f}"
    return f"${usd:.4f}"


@dataclass(frozen=True)
class TokenUsage:
    """The tokens of one call, split the way they are billed.

    ``input`` and ``output`` are the provider's totals: cached and reasoning
    tokens are inside them, as in the OpenAI shapes.
    """

    input: int = 0
    output: int = 0
    cached: int = 0
    cache_write: int = 0
    reasoning: int = 0

    @classmethod
    def from_payload(cls, usage: Mapping[str, Any]) -> "TokenUsage":
        """Read a ``usage`` object of chat completions or of the Responses API."""

        def number(value: Any) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

        def detail(*names: str) -> Mapping[str, Any]:
            for name in names:
                value = usage.get(name)
                if isinstance(value, Mapping):
                    return value
            return {}

        prompt = detail("prompt_tokens_details", "input_tokens_details")
        completion = detail("completion_tokens_details", "output_tokens_details")
        return cls(
            input=number(usage.get("prompt_tokens", usage.get("input_tokens"))),
            output=number(usage.get("completion_tokens", usage.get("output_tokens"))),
            cached=number(prompt.get("cached_tokens")),
            cache_write=number(prompt.get("cache_write_tokens")),
            reasoning=number(completion.get("reasoning_tokens")),
        )

    @property
    def total(self) -> int:
        return self.input + self.output


@dataclass(frozen=True)
class PriceTier:
    """Rates that replace the base ones once the prompt passes *over* tokens."""

    over: int
    input: Decimal
    output: Decimal
    cache_read: Optional[Decimal] = None
    cache_write: Optional[Decimal] = None


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens. A missing cache rate bills those tokens as input."""

    input: Decimal
    output: Decimal
    cache_read: Optional[Decimal] = None
    cache_write: Optional[Decimal] = None
    tiers: tuple[PriceTier, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Optional["Price"]:
        """A price from ``{input, output, cache_read, cache_write, tiers}``; None if unusable."""

        def rate(value: Any) -> Optional[Decimal]:
            if value is None or isinstance(value, bool):
                return None
            try:
                amount = Decimal(str(value))
            except Exception:  # noqa: BLE001
                return None
            return amount if amount.is_finite() and amount >= 0 else None

        input_rate, output_rate = rate(raw.get("input")), rate(raw.get("output"))
        if input_rate is None or output_rate is None:
            return None
        tiers = []
        for entry in raw.get("tiers") or ():
            if not isinstance(entry, Mapping):
                continue
            marker = entry.get("tier") if isinstance(entry.get("tier"), Mapping) else {}
            size = marker.get("size", entry.get("over"))
            tier_in, tier_out = rate(entry.get("input")), rate(entry.get("output"))
            if isinstance(size, int) and size > 0 and tier_in is not None and tier_out is not None:
                tiers.append(
                    PriceTier(size, tier_in, tier_out, rate(entry.get("cache_read")), rate(entry.get("cache_write")))
                )
        return cls(
            input_rate, output_rate, rate(raw.get("cache_read")), rate(raw.get("cache_write")),
            tuple(sorted(tiers, key=lambda tier: tier.over)),
        )

    def cost_micro(self, usage: TokenUsage) -> int:
        """What *usage* costs at this price, in micro-dollars."""
        input_rate, output_rate = self.input, self.output
        cache_read, cache_write = self.cache_read, self.cache_write
        for tier in self.tiers:
            if usage.input > tier.over:
                input_rate, output_rate = tier.input, tier.output
                cache_read, cache_write = tier.cache_read, tier.cache_write
        cache_read = input_rate if cache_read is None else cache_read
        cache_write = input_rate if cache_write is None else cache_write
        cached = min(usage.cached, usage.input)
        written = min(usage.cache_write, usage.input - cached)
        plain = usage.input - cached - written
        # Dollars per million tokens times tokens is micro-dollars.
        total = plain * input_rate + cached * cache_read + written * cache_write + usage.output * output_rate
        return int(total.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Cost:
    """What one call cost, and where the figure comes from."""

    micro: int
    basis: str

    @property
    def usd(self) -> Decimal:
        return Decimal(self.micro) / MICRO


def charge(
    usage: TokenUsage,
    *,
    reported_usd: Any = None,
    price: Optional[Price] = None,
    fallback: Optional[Price] = None,
    subscription: bool = False,
) -> Cost:
    """The cost of one call by the best source available.

    A subscription call is priced as the API would price it (``computed`` or
    ``estimated`` rates) and labelled ``subscription``: nothing was billed.
    """
    reported = None if subscription else to_micro(reported_usd)
    if reported is not None:
        return Cost(reported, REPORTED)
    if price is not None:
        micro, basis = price.cost_micro(usage), COMPUTED
    elif fallback is not None:
        micro, basis = fallback.cost_micro(usage), ESTIMATED
    else:
        micro, basis = 0, ESTIMATED
    return Cost(micro, SUBSCRIPTION if subscription else basis)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


@dataclass
class PriceBook:
    """Prices by provider and model name, from a models.dev snapshot.

    ``providers`` maps a models.dev provider id to ``{model id: price mapping}``;
    ``endpoints`` maps its API base URL to that id, so a Grid provider is found by
    where it points, whatever its key is called in a system's config.
    """

    providers: dict[str, dict[str, Price]] = field(default_factory=dict)
    endpoints: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_models_dev(cls, document: Mapping[str, Any]) -> "PriceBook":
        book = cls()
        for provider_id, provider in document.items():
            if not isinstance(provider, Mapping) or not isinstance(provider.get("models"), Mapping):
                continue
            prices: dict[str, Price] = {}
            for model_id, model in provider["models"].items():
                cost = model.get("cost") if isinstance(model, Mapping) else None
                price = Price.from_mapping(cost) if isinstance(cost, Mapping) else None
                if price is not None:
                    prices[str(model_id)] = price
            if prices:
                book.providers[str(provider_id)] = prices
                api = provider.get("api")
                if isinstance(api, str) and api:
                    book.endpoints[api.rstrip("/")] = str(provider_id)
        return book

    @classmethod
    def load(cls, path: Optional[Path]) -> "PriceBook":
        """The snapshot at *path*; an empty book when there is none (not an error)."""
        if path is None or not path.is_file():
            return cls()
        try:
            return cls.from_models_dev(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            logger.exception("Could not read the price snapshot %s; no prices from it", path)
            return cls()

    def lookup(self, base_url: str, model: str, provider_id: Optional[str] = None) -> Optional[Price]:
        """The price of *model* at the provider serving *base_url*.

        Tried by the model id as the provider names it, and for gateways whose ids
        read ``vendor/model`` (OpenRouter) by the vendor's own listing.
        """
        provider_id = provider_id or self.endpoints.get(base_url.rstrip("/"))
        if provider_id is None:
            return None
        listing = self.providers.get(provider_id, {})
        found = listing.get(model)
        if found is None and "/" in model:
            vendor, _, rest = model.partition("/")
            found = self.providers.get(vendor, {}).get(rest)
        return found

    def models_priced(self) -> int:
        return sum(len(listing) for listing in self.providers.values())


def models_dev_subset(document: Mapping[str, Any], wanted: Iterable[str]) -> dict[str, Any]:
    """The part of a models.dev document worth keeping: *wanted* providers, prices only."""
    keep = set(wanted)
    subset: dict[str, Any] = {}
    for provider_id, provider in document.items():
        if provider_id not in keep or not isinstance(provider, Mapping):
            continue
        models = {
            model_id: {"cost": model["cost"]}
            for model_id, model in (provider.get("models") or {}).items()
            if isinstance(model, Mapping) and isinstance(model.get("cost"), Mapping)
        }
        subset[provider_id] = {"id": provider_id, "api": provider.get("api"), "models": models}
    return subset
