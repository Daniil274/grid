from types import SimpleNamespace

import pytest

from core.credentials import Credential, CredentialError
from schemas.schemas import GridConfig
from web_chat.entitlements import ADMIN_TIER, IMPLICIT_TIER, Plans, UserCredentials
from web_chat.limits import TurnLimits

PROVIDER = SimpleNamespace(name="or", base_url="https://openrouter.ai/api/v1", api_key=None, api_key_env="OPENROUTER_API_KEY")
ENV = {
    "OPENROUTER_API_KEY": "admin-key",
    "OPENROUTER_API_KEY_FRIENDS": "friends-key",
    "OPENROUTER_API_KEY_STARTER": "starter-key",
}


def plans(**overrides):
    document = {
        "pools": {
            "friends": {"OPENROUTER_API_KEY": "OPENROUTER_API_KEY_FRIENDS"},
            "starter": {"OPENROUTER_API_KEY": "OPENROUTER_API_KEY_STARTER"},
        },
        "tiers": {
            "friend": {"pool": "friends", "own_credentials": ["openrouter"], "limits": {"usd_per_day": 5}},
            "new": {"pool": "starter", "models": ["cheap-*", "free/*"], "limits": {"usd_per_day": 0.5, "usd_per_turn": 0.1}},
            "byok": {"own_credentials": ["openrouter", "openai"]},
            "chatgpt": {"own_credentials": ["chatgpt"], "models": ["gpt-*"]},
        },
        "default_tier": "new",
        "user_limits": {"turns_per_day": 100},
        **overrides,
    }
    config = GridConfig(**document)
    return Plans(lambda: config)


class Vault:
    def __init__(self, **stored):
        self.stored = stored

    def has(self, user_id, kind):
        return kind in self.stored

    async def credential(self, user_id, kind):
        secret = self.stored.get(kind)
        return Credential(secret, "own", charged=False) if secret else None


def credentials(plan, tier, *, admin=False, own=None):
    return UserCredentials("u1", lambda: plan.resolve(admin=admin, tier=tier), plan, own, environ=ENV)


def test_a_server_without_tiers_has_one_plan_for_everybody():
    config = GridConfig(user_limits={"turns_per_day": 7})
    entitlement = Plans(lambda: config).resolve(admin=False, tier="whatever")
    assert (entitlement.tier, entitlement.pool, entitlement.limits.turns_per_day) == (IMPLICIT_TIER, "default", 7)
    assert entitlement.permits_model("any", "thing")


def test_plans_resolve_by_tier_with_the_default_for_unassigned_and_unknown_tiers():
    plan = plans()
    assert plan.resolve(admin=False, tier="friend").pool == "friends"
    assert plan.resolve(admin=False, tier="").tier == "new"
    assert plan.resolve(admin=False, tier="removed-tier").tier == "new"
    assert plan.tier_names() == ["friend", "new", "byok", "chatgpt"]


def test_limits_come_from_the_tier_else_the_root():
    plan = plans()
    assert plan.resolve(admin=False, tier="new").limits.usd_per_turn == 0.1
    assert plan.resolve(admin=False, tier="byok").limits.turns_per_day == 100  # inherits the root


def test_an_admin_without_an_admin_tier_gets_the_operators_keys_and_every_model():
    entitlement = plans().resolve(admin=True, tier="new")
    assert (entitlement.tier, entitlement.pool, entitlement.permits_model("x", "y")) == (ADMIN_TIER, "default", True)
    configured = plans(tiers={"admin": {"pool": "friends", "models": ["only-*"]}, "new": {}})
    assert configured.resolve(admin=True).pool == "friends"


def test_models_are_matched_by_key_and_by_name():
    entitlement = plans().resolve(admin=False, tier="new")
    assert entitlement.permits_model("cheap-fast", "vendor/x")
    assert entitlement.permits_model("k", "free/model-1")
    assert not entitlement.permits_model("big", "vendor/big-model")


@pytest.mark.asyncio
async def test_each_pool_serves_its_own_tier_the_replacement_key():
    plan = plans()
    assert (await credentials(plan, "friend").credential(PROVIDER)).secret == "friends-key"
    assert (await credentials(plan, "new").credential(PROVIDER)).secret == "starter-key"
    admin = await credentials(plan, "new", admin=True).credential(PROVIDER)
    assert (admin.secret, admin.source, admin.charged) == ("admin-key", "pool:default", True)


@pytest.mark.asyncio
async def test_a_plan_without_a_pool_or_key_is_refused_never_given_the_operators_key():
    plan = plans()
    assert not credentials(plan, "byok").available(PROVIDER)
    with pytest.raises(CredentialError, match="No credential"):
        await credentials(plan, "byok").credential(PROVIDER)
    # The pool exists but its variable is not set: still no fallback to the admin key.
    broken = UserCredentials("u1", lambda: plan.resolve(admin=False, tier="new"), plan, environ={"OPENROUTER_API_KEY": "admin-key"})
    with pytest.raises(CredentialError):
        await broken.credential(PROVIDER)


@pytest.mark.asyncio
async def test_a_users_own_key_wins_where_the_plan_allows_it_and_is_not_charged():
    plan = plans()
    own = Vault(openrouter="user-key")
    friend = await credentials(plan, "friend", own=own).credential(PROVIDER)
    assert (friend.secret, friend.source, friend.charged) == ("user-key", "own", False)
    # "new" does not list openrouter among its own credentials: the key is ignored.
    assert (await credentials(plan, "new", own=own).credential(PROVIDER)).secret == "starter-key"
    assert (await credentials(plan, "byok", own=own).credential(PROVIDER)).secret == "user-key"


@pytest.mark.asyncio
async def test_a_user_key_is_sent_only_to_its_providers_own_address():
    plan = plans()
    elsewhere = SimpleNamespace(name="or", base_url="https://evil.test/v1", api_key=None, api_key_env="OPENROUTER_API_KEY")
    assert not credentials(plan, "byok", own=Vault(openrouter="user-key")).available(elsewhere)


@pytest.mark.asyncio
async def test_an_own_credential_that_stopped_working_is_reported_not_replaced_by_the_pool():
    class Dead(Vault):
        async def credential(self, user_id, kind):
            return None

    plan = plans()
    with pytest.raises(CredentialError, match="Reconnect"):
        await credentials(plan, "friend", own=Dead(openrouter="x")).credential(PROVIDER)


def test_config_rejects_a_tier_on_an_undefined_pool_and_an_undefined_default():
    with pytest.raises(ValueError, match="pool"):
        GridConfig(tiers={"new": {"pool": "nowhere"}})
    with pytest.raises(ValueError, match="default_tier"):
        GridConfig(tiers={"friend": {}}, default_tier="new")


class Spend:
    def __init__(self, spent):
        self.spent = spent
        self.since = []

    def spent_micro(self, user_id, since):
        self.since.append(since)
        return self.spent


class Days:
    def count_turn(self, user_id, limit):
        return True

    def tokens_today(self, user_id):
        return 0


def test_dollar_budgets_refuse_a_turn_with_what_was_spent_and_when_it_resets():
    plan = plans()
    policy = plan.resolve(admin=False, tier="new").limits
    now = 1_790_000_000  # 2026-09-21 ~14:13 UTC
    limits = TurnLimits("u1", lambda: policy, Days(), spend=Spend(600_000), clock=lambda: now)
    refusal = limits.admit(0)
    assert "today's $0.5000 budget" in refusal and "$0.6000 spent" in refusal and "00:00 UTC" in refusal
    assert TurnLimits("u1", lambda: policy, Days(), spend=Spend(100_000), clock=lambda: now).admit(0) is None
    assert limits.usd_budget() == 100_000


def test_the_day_and_month_windows_start_at_utc_midnight_and_the_first():
    spend = Spend(0)
    policy = GridConfig(user_limits={"usd_per_day": 1, "usd_per_month": 10}).user_limits
    TurnLimits("u1", lambda: policy, Days(), spend=spend, clock=lambda: 1_790_000_000).admit(0)
    assert spend.since == [1_789_948_800, 1_788_220_800]  # 2026-09-21 00:00 and 2026-09-01 00:00 UTC
