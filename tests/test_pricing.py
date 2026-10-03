from decimal import Decimal

from core.pricing import (
    COMPUTED, ESTIMATED, REPORTED, SUBSCRIPTION, PriceBook, Price, TokenUsage, charge, format_usd, to_micro,
)

PRICE = Price.from_mapping({"input": 2, "output": 10, "cache_read": 0.5})


def test_to_micro_rejects_what_is_not_an_amount():
    assert to_micro("0.000123") == 123
    assert to_micro(0) == 0
    for bad in (None, True, "x", -1, float("nan")):
        assert to_micro(bad) is None


def test_usage_reads_chat_and_responses_shapes():
    chat = TokenUsage.from_payload({
        "prompt_tokens": 100, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10},
        "completion_tokens_details": {"reasoning_tokens": 5},
    })
    assert (chat.input, chat.output, chat.cached, chat.cache_write, chat.reasoning) == (100, 20, 40, 10, 5)
    responses = TokenUsage.from_payload({
        "input_tokens": 7, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 2},
    })
    assert (responses.input, responses.output, responses.cached) == (7, 3, 2)
    assert TokenUsage.from_payload({"prompt_tokens": "x", "completion_tokens": -4}) == TokenUsage()


def test_price_bills_cached_tokens_at_the_cache_rate_inside_the_input():
    usage = TokenUsage(input=1_000_000, output=100_000, cached=400_000)
    # 600k plain * $2 + 400k cached * $0.5 + 100k out * $10
    assert PRICE.cost_micro(usage) == 600_000 * 2 + 400_000 // 2 + 100_000 * 10


def test_a_missing_cache_rate_bills_cached_tokens_as_input():
    price = Price.from_mapping({"input": 1, "output": 1})
    assert price.cost_micro(TokenUsage(input=10, cached=10)) == 10


def test_context_tiers_replace_the_rates_past_their_size():
    price = Price.from_mapping({
        "input": 1, "output": 2,
        "tiers": [{"input": 3, "output": 6, "tier": {"type": "context", "size": 200}}],
    })
    assert price.cost_micro(TokenUsage(input=100, output=10)) == 100 + 20
    assert price.cost_micro(TokenUsage(input=300, output=10)) == 900 + 60


def test_unusable_prices_are_none():
    assert Price.from_mapping({"input": 1}) is None
    assert Price.from_mapping({"input": "x", "output": 1}) is None
    assert Price.from_mapping({"input": -1, "output": 1}) is None


def test_charge_prefers_what_the_provider_reported():
    usage = TokenUsage(input=1000, output=1000)
    assert charge(usage, reported_usd=0.0042, price=PRICE) == charge(usage, reported_usd="0.0042")
    assert charge(usage, reported_usd=0.0042, price=PRICE).basis == REPORTED
    assert charge(usage, reported_usd=0, price=PRICE).micro == 0  # a free model stays free


def test_charge_computes_then_estimates_then_labels_subscriptions():
    usage = TokenUsage(input=1000, output=1000)
    assert charge(usage, price=PRICE).basis == COMPUTED
    estimated = charge(usage, fallback=Price.from_mapping({"input": 5, "output": 15}))
    assert (estimated.micro, estimated.basis) == (20_000, ESTIMATED)
    sub = charge(usage, reported_usd=9, price=PRICE)  # unreported on a subscription: ignored
    assert charge(usage, reported_usd=9, price=PRICE, subscription=True).basis == SUBSCRIPTION
    assert charge(usage, reported_usd=9, price=PRICE, subscription=True).micro == PRICE.cost_micro(usage)
    assert sub.basis == REPORTED


def test_price_book_finds_models_by_endpoint_and_by_vendor_prefix():
    book = PriceBook.from_models_dev({
        "opencode-go": {"api": "https://opencode.ai/zen/go/v1", "models": {"kimi": {"cost": {"input": 1, "output": 2}}}},
        "openai": {"models": {"gpt-5.4": {"cost": {"input": 2.5, "output": 15}}, "free": {}}},
        "openrouter": {"api": "https://openrouter.ai/api/v1", "models": {"x/y": {"cost": {"input": 1, "output": 1}}}},
    })
    assert book.lookup("https://opencode.ai/zen/go/v1/", "kimi").input == Decimal(1)
    assert book.lookup("https://openrouter.ai/api/v1", "openai/gpt-5.4").output == Decimal(15)
    assert book.lookup("https://elsewhere.test/v1", "kimi") is None
    assert book.lookup("https://opencode.ai/zen/go/v1", "unknown") is None
    assert book.models_priced() == 3


def test_missing_snapshot_is_an_empty_book(tmp_path):
    assert PriceBook.load(tmp_path / "none.json").models_priced() == 0
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert PriceBook.load(broken).models_priced() == 0


def test_format_usd():
    assert format_usd(4200) == "$0.0042"
    assert format_usd(12_500_000) == "$12.50"
