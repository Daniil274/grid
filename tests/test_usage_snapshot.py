"""One usage picture: subscriptions, keys, active reads, and the lazy agents' input."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from schemas.schemas import GridConfig
from tests.test_web_accounts import PASSWORD, SAME_SITE, accounts, cheap_hashing, signed_in  # noqa: F401
from tests.test_web_spaces import FakeSpace
from web_chat import chatgpt_quota
from web_chat.accounts.http import SessionAuth
from web_chat.chatgpt_quota import QuotaUnavailable, account_id, fetch_usage, reading_from_payload
from web_chat.entitlements import Plans
from web_chat.lazy.service import LazyAgents
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool
from web_chat.usage_snapshot import (
    ClaudeQuotaProbe,
    RefreshUnavailable,
    SnapshotError,
    UsageSnapshot,
    fetch_openrouter_key,
)

NOW = 1_800_000_000.0
HOUR = 3600.0


def b64(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def id_token(claim: str = "acct-1") -> str:
    return f"{b64({'alg': 'none'})}.{b64({'sub': 'u', chatgpt_quota.ACCOUNT_CLAIM: claim})}.sig"


# -- the ChatGPT plan's payload -------------------------------------------------------
WHAM = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 12,
            "limit_window_seconds": 5 * 3600,
            "reset_after_seconds": 100,
            "reset_at": int(NOW) + 100,
        },
        "secondary_window": {
            "used_percent": 70,
            "limit_window_seconds": 7 * 24 * 3600,
            "reset_after_seconds": 1000,
            "reset_at": int(NOW) + 1000,
        },
    },
}


def test_the_plan_windows_are_named_by_their_length_and_never_guessed():
    reading = reading_from_payload(WHAM, "u1", NOW)
    assert reading.account == "chatgpt:u1" and reading.status == "allowed"
    assert reading.windows["five_hour"].used == 12
    assert reading.windows["seven_day"].used == 70
    assert reading.windows["seven_day"].resets_at == NOW + 1000
    # An extra metered feature is shown next to the plan windows.
    extra = {
        "rate_limit": {**WHAM["rate_limit"]},
        "additional_rate_limits": [
            {"limit_name": "Code review", "metered_feature": "code_review",
             "rate_limit": {"primary_window": {"used_percent": 3, "limit_window_seconds": 3600}}},
        ],
    }
    names = reading_from_payload(extra, "u1", NOW).windows
    assert "code_review:primary" in names and names["code_review:primary"].used == 3
    with pytest.raises(QuotaUnavailable, match="no quota windows"):
        reading_from_payload({"rate_limit": None}, "u1", NOW)


def test_the_account_id_comes_from_the_id_token_claim():
    assert account_id(id_token("acct-9")) == "acct-9"
    assert account_id("garbage") is None and account_id(None) is None


@pytest.mark.asyncio
async def test_the_chatgpt_usage_call_sends_the_plan_token_and_account_and_returns_windows():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=WHAM)

    vault = SimpleNamespace(
        enabled=True,
        state=lambda user, kind: {"id_token": id_token("acct-9")},
        credential=_credential("access-1"),
    )
    reading = await fetch_usage(vault, "u1", transport=httpx.MockTransport(handler), now=NOW)
    assert reading.windows["five_hour"].used == 12
    request = seen[0]
    assert str(request.url) == chatgpt_quota.USAGE_URL
    assert request.headers["Authorization"] == "Bearer access-1"
    assert request.headers["ChatGPT-Account-Id"] == "acct-9"


@pytest.mark.asyncio
async def test_a_refused_usage_call_is_explained_without_numbers():
    vault = SimpleNamespace(enabled=True, state=lambda u, k: {"id_token": id_token()}, credential=_credential("a"))
    with pytest.raises(QuotaUnavailable, match="did not accept"):
        await fetch_usage(vault, "u1", transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(QuotaUnavailable, match="rate-limiting"):
        await fetch_usage(vault, "u1", transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    no_login = SimpleNamespace(enabled=True, state=lambda u, k: None)
    with pytest.raises(QuotaUnavailable, match="not signed in"):
        await fetch_usage(no_login, "u1", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=WHAM)))


def _credential(secret: str):
    async def credential(user, kind):
        return SimpleNamespace(secret=secret)

    return credential


# -- OpenRouter's key ---------------------------------------------------------------
OPENROUTER = {
    "data": {
        "label": "test", "limit": 10.0, "limit_remaining": 7.5, "usage": 2.5,
        "usage_daily": 0.5, "usage_weekly": 1.0, "usage_monthly": 2.0,
        "limit_reset": "monthly", "is_free_tier": False,
    }
}


@pytest.mark.asyncio
async def test_an_openrouter_key_reports_its_limit_remaining_and_usage():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OPENROUTER)

    vault = SimpleNamespace(enabled=True, credential=_credential("sk-or-secret"))
    limits = await fetch_openrouter_key(vault, "u1", transport=httpx.MockTransport(handler), now=NOW)
    assert limits["limit_usd"] == 10.0 and limits["remaining_usd"] == 7.5
    assert limits["usage_usd"] == 2.5 and limits["usage_monthly_usd"] == 2.0
    assert limits["observed_at"] == NOW
    assert seen[0].headers["Authorization"] == "Bearer sk-or-secret"


@pytest.mark.asyncio
async def test_a_refused_openrouter_key_is_explained():
    vault = SimpleNamespace(enabled=True, credential=_credential("bad"))
    with pytest.raises(RefreshUnavailable, match="did not accept"):
        await fetch_openrouter_key(vault, "u1", transport=httpx.MockTransport(lambda r: httpx.Response(401)))


# -- the picture of every source ----------------------------------------------------
CONFIG = GridConfig(
    providers={
        "claude-main": {"name": "Claude Code", "base_url": "cli", "auth": "claude"},
        "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
        "openai-api": {"name": "OpenAI API", "base_url": "https://api.openai.com/v1"},
        "engine": {"name": "Engine", "base_url": "https://engine.example/v1"},
    },
    models={
        "opus": {"name": "opus", "provider": "claude-main"},
        "or-model": {"name": "or-model", "provider": "openrouter"},
        "gpt": {"name": "gpt", "provider": "openai-api"},
        "engine-model": {"name": "engine-model", "provider": "engine"},
    },
    tiers={"byok": {"label": "Bring your own key", "own_credentials": ["openrouter", "openai", "chatgpt"]}},
    default_tier="byok",
)


class Vault:
    enabled = True

    def __init__(self, stored=()):
        self.stored = {kind: {"kind": kind, "hint": "…1234", "status": "ok", "updated_at": 0} for kind in stored}

    def status(self, user_id, kind):
        return (self.stored.get(kind) or {}).get("status")

    def listing(self, user_id):
        return list(self.stored.values())

    def state(self, user_id, kind):
        return None

    async def credential(self, user_id, kind):
        return None


def user(tier="byok"):
    return SimpleNamespace(id="u1", is_admin=False, tier=tier)


def admin(tier="byok"):
    return SimpleNamespace(id="u1", is_admin=True, tier=tier)


def snapshot_of(vault, **options) -> UsageSnapshot:
    # Most tests describe the local, single-user server: the machine login is the user's.
    options.setdefault("local_cli_login", lambda: True)
    return UsageSnapshot(lambda: Plans(lambda: CONFIG), lambda: CONFIG, vault=vault, clock=lambda: NOW, **options)


def by_id(state):
    return {source["id"]: source for source in state["sources"]}


def test_every_configured_subscription_and_key_is_listed_and_unreadable_ones_say_why():
    vault = Vault(stored=("openrouter", "openai"))
    sources = by_id(snapshot_of(vault).snapshot(admin()))
    assert set(sources) == {
        "claude-code", "chatgpt", "key:kimi", "key:kimi-code", "key:openrouter", "key:openai", "key:opencode-go",
        "server:engine",
    }  # an admin may bring any credential, so every preset key is listed
    assert sources["claude-code"]["status"] == "unknown"
    assert sources["claude-code"]["action"]["costs_quota"] is True
    assert sources["chatgpt"]["status"] == "unavailable" and "Not connected" in sources["chatgpt"]["note"]
    assert sources["key:openrouter"]["status"] == "unknown"
    assert sources["key:openai"]["status"] == "unavailable"
    assert "does not report a key's remaining quota" in sources["key:openai"]["note"]
    assert sources["server:engine"]["status"] == "unavailable"
    assert "belongs to the operator" in sources["server:engine"]["note"]
    assert "1234" in sources["key:openrouter"]["hint"] and "sk-" not in json.dumps(sources)


def test_a_server_key_is_not_shown_when_the_users_own_key_already_pays():
    vault = Vault(stored=("openrouter",))
    sources = by_id(snapshot_of(vault).snapshot(user()))
    assert "key:openrouter" in sources and "server:openrouter" not in sources
    # A connected key of another provider leaves the server's own key in the picture.
    assert "server:engine" in sources


def test_a_stored_key_stays_visible_when_the_plan_no_longer_names_it():
    config = CONFIG.model_copy(deep=True)
    config.tiers["byok"].own_credentials = ["chatgpt"]
    snapshot = UsageSnapshot(
        lambda: Plans(lambda: config), lambda: config, vault=Vault(stored=("openai", "chatgpt")), clock=lambda: NOW,
    )
    sources = by_id(snapshot.snapshot(user()))
    # A key the user already stored is still theirs: it is listed, not hidden.
    assert sources["key:openai"]["connected"] is True
    assert sources["chatgpt"]["connected"] is True
    assert "key:openrouter" not in sources


def test_a_live_reading_is_shown_with_its_windows():
    from core.quota_telemetry import QuotaReading, WindowReading

    snapshot = snapshot_of(Vault(stored=("openrouter",)))
    snapshot._keys[("u1", "openrouter")] = {
        "limit_usd": 10.0, "remaining_usd": 7.5, "usage_usd": 2.5,
        "usage_daily_usd": 0.5, "usage_weekly_usd": 1.0, "usage_monthly_usd": 2.0,
        "limit_reset": "monthly", "observed_at": NOW,
    }
    source = by_id(snapshot.snapshot(user()))["key:openrouter"]
    assert source["status"] == "live" and source["limits"]["remaining_usd"] == 7.5

    # Once the key or the sign-in is gone, its old numbers go with it.
    stale = snapshot_of(Vault())
    stale._chatgpt["u1"] = QuotaReading(
        account="chatgpt:u1",
        windows={"five_hour": WindowReading("five_hour", used=30, resets_at=NOW + HOUR)},
        observed_at=NOW,
    )
    stale._keys[("u1", "openrouter")] = {"limit_usd": 10.0, "remaining_usd": 7.5, "observed_at": NOW}
    gone = by_id(stale.snapshot(user()))
    assert gone["chatgpt"]["status"] == "unavailable"
    assert gone["chatgpt"]["windows"] == [] and gone["chatgpt"]["observed_at"] is None
    assert gone["key:openrouter"]["limits"] is None


@pytest.mark.asyncio
async def test_the_claude_probe_requires_confirmation_and_publishes_a_reading():
    from core.quota_telemetry import QuotaReading, QuotaTelemetry, WindowReading

    quota = QuotaTelemetry()
    calls = []

    async def probe():
        calls.append(1)
        reading = QuotaReading(
            account="claude-code",
            windows={"five_hour": WindowReading("five_hour", used=20, resets_at=NOW + HOUR)},
            observed_at=NOW,
        )
        quota.publish(reading)
        return reading

    snapshot = snapshot_of(Vault(), quota=quota, probe=probe)
    with pytest.raises(SnapshotError, match="confirm"):
        await snapshot.refresh(admin(), "claude-code")
    assert calls == []
    state = await snapshot.refresh(admin(), "claude-code", confirm=True)
    assert calls == [1]
    assert by_id(state)["claude-code"]["status"] == "live"
    assert by_id(state)["claude-code"]["windows"][0]["used"] == 20
    # A second press within the cooldown does not run the paid probe again.
    await snapshot.refresh(admin(), "claude-code", confirm=True)
    assert calls == [1]


@pytest.mark.asyncio
async def test_an_unknown_source_cannot_be_refreshed():
    with pytest.raises(SnapshotError, match="No source"):
        await snapshot_of(Vault()).refresh(user(), "nonsense")


@pytest.mark.asyncio
async def test_the_claude_probe_uses_the_runner_and_keeps_only_the_reading():
    from core.quota_telemetry import QuotaTelemetry

    seen = {}

    class FakeRunner:
        async def stream(self, prompt):
            seen["prompt"] = prompt
            yield {"type": "system", "subtype": "init"}
            yield {"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed",
                "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": NOW + HOUR}},
            }}

    quota = QuotaTelemetry()
    probe = ClaudeQuotaProbe(binary="claude", quota=quota, clock=lambda: NOW, runner=FakeRunner)
    reading = await probe()
    assert seen["prompt"]
    assert reading.windows["five_hour"].used == 20
    assert quota.latest("claude-code") == reading


@pytest.mark.asyncio
async def test_a_silent_claude_run_is_reported_without_numbers():
    class Silent:
        async def stream(self, prompt):
            yield {"type": "result"}

    probe = ClaudeQuotaProbe(quota=None, clock=lambda: NOW, runner=Silent)
    with pytest.raises(RefreshUnavailable, match="without reporting"):
        await probe()


# -- the lazy agents read the same picture ------------------------------------------
def test_lazy_agents_prefer_the_active_chatgpt_reading_over_their_estimate(tmp_path):
    from core.lazy.model import Settings
    from core.lazy.store import LazyStore
    from core.quota_telemetry import QuotaReading, WindowReading

    store = LazyStore(tmp_path / "lazy.db")
    # Windows are a pool's own: the ChatGPT reading answers a ChatGPT pool only.
    settings = Settings(account="chatgpt")
    snapshot = snapshot_of(Vault())
    snapshot._chatgpt["u1"] = QuotaReading(
        account="chatgpt:u1",
        windows={"five_hour": WindowReading("five_hour", used=30, resets_at=NOW + HOUR)},
        observed_at=NOW,
        status="allowed",
    )
    agents = LazyAgents(store, SimpleNamespace(), usage=None, snapshot=snapshot, clock=lambda: NOW)
    situation = agents.situation("u1", settings)
    assert situation.mode == "live"
    assert situation.windows["five_hour"].used == 30
    # A Claude Code pool never borrows another subscription's windows.
    assert agents.situation("u1", Settings()).mode == "unknown"
    store.close()


# -- the HTTP API ---------------------------------------------------------------------
class StubSnapshot:
    def __init__(self):
        self.calls = []
        self.live_calls = 0
        self.state = {"now": NOW, "sources": [{"id": "chatgpt", "status": "unknown"}]}

    def snapshot(self, user):
        return self.state

    async def snapshot_live(self, user):
        self.live_calls += 1
        return self.state

    async def refresh(self, user, source, *, confirm=False):
        if source == "claude-code" and not confirm:
            raise SnapshotError("confirm it")
        self.calls.append((source, confirm))
        self.state = {"now": NOW + 1, "sources": [{"id": source, "status": "live"}]}
        return self.state


class Deployment:
    """Enough for a server with accounts: the injected snapshot builds every other source."""


def test_the_snapshot_api_requires_a_user_and_refreshes_only_known_sources(accounts):
    stub = StubSnapshot()
    server = WebChatServer(
        Deployment(), SpacePool(FakeSpace), auth=SessionAuth(accounts), warm_user=None, snapshot=stub,
        trusted_hosts=("testserver",))
    anonymous = TestClient(server.app, headers=SAME_SITE)
    assert anonymous.get("/api/usage/snapshot").status_code == 401
    accounts.create_user("bob", PASSWORD)
    client = signed_in(server, "bob")
    body = client.get("/api/usage/snapshot").json()
    assert body["sources"][0]["id"] == "chatgpt"
    # Opening the page asks the snapshot for a live picture, not just the cache.
    assert stub.live_calls == 1
    assert client.post("/api/usage/refresh", json={"source": "chatgpt"}).json()["sources"][0]["status"] == "live"
    assert stub.calls == [("chatgpt", False)]
    refused = client.post("/api/usage/refresh", json={"source": "claude-code"})
    assert refused.status_code == 400 and "confirm" in refused.json()["detail"]
    ok = client.post("/api/usage/refresh", json={"source": "claude-code", "confirm": True})
    assert ok.status_code == 200 and stub.calls[-1] == ("claude-code", True)


def test_the_snapshot_api_serves_every_source_without_a_secret(accounts):
    server = WebChatServer(
        Deployment(), SpacePool(FakeSpace), auth=SessionAuth(accounts), warm_user=None,
        snapshot=snapshot_of(Vault(stored=("openrouter",))), trusted_hosts=("testserver",))
    accounts.create_user("bob", PASSWORD)
    body = signed_in(server, "bob").get("/api/usage/snapshot").json()
    sources = by_id(body)
    assert body["now"] == NOW
    assert sources["chatgpt"]["status"] == "unavailable"
    assert sources["key:openrouter"]["status"] == "unknown"
    assert sources["key:openrouter"]["hint"] == "…1234"
    assert "sk-" not in json.dumps(body)


# -- rights: whose sources a page may see and read ------------------------------------
def test_the_machine_claude_login_is_absent_when_this_is_not_the_local_server():
    snapshot = UsageSnapshot(
        lambda: Plans(lambda: CONFIG), lambda: CONFIG, vault=Vault(), clock=lambda: NOW,
    )  # local_cli_login defaults to False: a server with accounts
    sources = by_id(snapshot.snapshot(user()))
    assert "claude-code" not in sources
    assert "key:openrouter" in sources  # the plan's own sources are still there


def test_the_machine_claude_login_is_shown_to_admins_only():
    snapshot = snapshot_of(Vault())  # the local server, as most tests use it
    assert "claude-code" in by_id(snapshot.snapshot(admin()))
    assert "claude-code" not in by_id(snapshot.snapshot(user()))


@pytest.mark.asyncio
async def test_refresh_refuses_a_source_this_account_may_not_see():
    snapshot = UsageSnapshot(
        lambda: Plans(lambda: CONFIG), lambda: CONFIG, vault=Vault(), clock=lambda: NOW,
    )
    with pytest.raises(SnapshotError, match="No source"):
        await snapshot.refresh(user(), "claude-code", confirm=True)
    # A source this account does not have at all, or one that has nothing to read.
    with pytest.raises(SnapshotError, match="No source"):
        await snapshot.refresh(user(), "key:openai")
    with pytest.raises(SnapshotError, match="No source"):
        await snapshot.refresh(user(), "server:engine")
    with pytest.raises(SnapshotError, match="No source"):
        await snapshot.refresh(user(), "nonsense")


@pytest.mark.asyncio
async def test_refreshing_a_listed_but_unconnected_source_explains_itself():
    snapshot = snapshot_of(Vault())  # no key stored
    state = await snapshot.refresh(user(), "key:openrouter")
    source = by_id(state)["key:openrouter"]
    assert source["connected"] is False
    assert "No OpenRouter key is connected" in source["error"]


@pytest.mark.asyncio
async def test_the_claude_probe_is_globally_cooled_down_for_the_machine_login():
    calls = []

    async def probe():
        from core.quota_telemetry import QuotaReading, WindowReading

        calls.append(1)
        return QuotaReading(
            account="claude-code",
            windows={"five_hour": WindowReading("five_hour", used=10, resets_at=NOW + HOUR)},
            observed_at=NOW,
        )

    tick = [NOW]
    snapshot = UsageSnapshot(
        lambda: Plans(lambda: CONFIG), lambda: CONFIG, vault=Vault(), clock=lambda: tick[0],
        probe=probe, local_cli_login=lambda: True,
    )
    await snapshot.refresh(admin(), "claude-code", confirm=True)
    # The machine login is one credential: another account cannot start it again at once.
    await snapshot.refresh(SimpleNamespace(id="u2", is_admin=True, tier="byok"), "claude-code", confirm=True)
    assert len(calls) == 1
    tick[0] += 16
    await snapshot.refresh(SimpleNamespace(id="u2", is_admin=True, tier="byok"), "claude-code", confirm=True)
    assert len(calls) == 2


# -- a view reads the free sources by itself ------------------------------------------
class LiveVault(Vault):
    def __init__(self, stored=("chatgpt", "openrouter")):
        super().__init__(stored=stored)

    def state(self, user_id, kind):
        return {"id_token": id_token()} if kind == "chatgpt" else None

    async def credential(self, user_id, kind):
        return SimpleNamespace(secret="token-for-tests")


def live_snapshot(vault, clock, chatgpt_transport, key_transport, **options):
    options.setdefault("local_cli_login", lambda: True)
    return UsageSnapshot(
        lambda: Plans(lambda: CONFIG), lambda: CONFIG, vault=vault, clock=clock,
        chatgpt_transport=chatgpt_transport, key_transport=key_transport, **options,
    )


@pytest.mark.asyncio
async def test_opening_the_page_reads_the_free_sources_once_per_ttl():
    tick = [NOW]
    chat, keys = [], []

    def chatgpt(request):
        chat.append(request)
        return httpx.Response(200, json=WHAM)

    def key(request):
        keys.append(request)
        return httpx.Response(200, json=OPENROUTER)

    snapshot = live_snapshot(
        LiveVault(), lambda: tick[0],
        httpx.MockTransport(chatgpt), httpx.MockTransport(key),
    )
    state = await snapshot.snapshot_live(user())
    sources = by_id(state)
    assert sources["chatgpt"]["status"] == "live" and sources["chatgpt"]["windows"]
    assert sources["key:openrouter"]["status"] == "live"
    assert (len(chat), len(keys)) == (1, 1)
    # A second look within the TTL is served from the reading just made.
    await snapshot.snapshot_live(user())
    assert (len(chat), len(keys)) == (1, 1)
    tick[0] += 61
    await snapshot.snapshot_live(user())
    assert (len(chat), len(keys)) == (2, 2)


@pytest.mark.asyncio
async def test_a_failed_autoread_is_explained_and_not_retried_at_once():
    tick = [NOW]
    chat, keys = [], []

    def chatgpt(request):
        chat.append(request)
        return httpx.Response(401)

    def key(request):
        keys.append(request)
        raise httpx.ConnectError("no route", request=request)

    snapshot = live_snapshot(
        LiveVault(), lambda: tick[0],
        httpx.MockTransport(chatgpt), httpx.MockTransport(key),
    )
    state = await snapshot.snapshot_live(user())
    sources = by_id(state)
    assert "did not accept" in sources["chatgpt"]["error"]
    assert "Could not reach OpenRouter" in sources["key:openrouter"]["error"]
    assert sources["key:openrouter"]["limits"] is None
    assert sources["chatgpt"]["windows"] == []
    await snapshot.snapshot_live(user())
    assert (len(chat), len(keys)) == (1, 1)


@pytest.mark.asyncio
async def test_a_view_never_runs_the_paid_claude_probe():
    calls = []

    async def probe():
        from core.quota_telemetry import QuotaReading, WindowReading

        calls.append(1)
        return QuotaReading(
            account="claude-code",
            windows={"five_hour": WindowReading("five_hour", used=1, resets_at=NOW + HOUR)},
            observed_at=NOW,
        )

    snapshot = live_snapshot(Vault(), lambda: NOW, None, None, probe=probe)
    state = await snapshot.snapshot_live(admin())
    assert calls == []
    # Only the free sources were read; the paid one keeps its explicit button.
    assert by_id(state)["claude-code"]["action"]["costs_quota"] is True


def test_the_plan_policy_and_its_spend_are_part_of_the_picture():
    snapshot = UsageSnapshot(
        lambda: Plans(lambda: CONFIG), lambda: CONFIG, vault=Vault(), clock=lambda: NOW,
        spent_micro=lambda user_id, since: 2_500_000,
    )
    plan = snapshot.snapshot(user())["plan"]
    assert plan["tier"] == "byok" and plan["label"] == "Bring your own key"
    assert plan["limits"]["usd_per_day"] is None  # the config sets no dollar limit
    assert plan["limits"]["turns_per_day"] is None
    assert plan["spent"] == {"day_usd": 2.5, "month_usd": 2.5}


# -- a lazy plan reads the free source too --------------------------------------------
@pytest.mark.asyncio
async def test_the_lazy_page_refreshes_the_free_sources_before_showing(tmp_path):
    from core.lazy.store import LazyStore

    class Snapshot:
        def __init__(self):
            self.asked = []

        async def refresh_free(self, user):
            self.asked.append(user.id)

    store = LazyStore(tmp_path / "lazy.db")
    snapshot = Snapshot()
    agents = LazyAgents(store, SimpleNamespace(), snapshot=snapshot, clock=lambda: NOW)
    await agents.ensure_fresh(SimpleNamespace(id="u1"))
    assert snapshot.asked == ["u1"]
    store.close()


@pytest.mark.asyncio
async def test_a_due_chatgpt_step_reads_the_plan_but_another_pool_does_not(tmp_path):
    from core.lazy.model import Settings
    from core.lazy.store import LazyStore

    class Snapshot:
        def __init__(self):
            self.asked = []

        async def ensure_reading(self, user_id, source):
            self.asked.append((user_id, source))

    store = LazyStore(tmp_path / "lazy.db")
    snapshot = Snapshot()
    agents = LazyAgents(store, SimpleNamespace(), snapshot=snapshot, clock=lambda: NOW)
    await agents._read_reading("u1", Settings(account="chatgpt"))
    await agents._read_reading("u1", Settings(account="claude-code"))
    assert snapshot.asked == [("u1", "chatgpt")]
    store.close()


@pytest.mark.asyncio
async def test_a_probe_closes_the_cli_stream_as_soon_as_it_has_the_report():
    from core.quota_telemetry import QuotaTelemetry

    class Runner:
        def __init__(self):
            self.closed = False

        async def stream(self, prompt):
            try:
                yield {"type": "system"}
                yield {"type": "rate_limit_event", "rate_limit_info": {
                    "status": "allowed",
                    "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": NOW + HOUR}},
                }}
                yield {"type": "result"}
            finally:
                self.closed = True

    runner = Runner()
    probe = ClaudeQuotaProbe(quota=QuotaTelemetry(), clock=lambda: NOW, runner=runner)
    reading = await probe()
    assert reading.windows["five_hour"].used == 20
    assert runner.closed  # its process is terminated now, not at garbage collection


def test_the_shipped_catalog_hands_the_usage_page_its_systems_models():
    # The catalog names only its router and policy models: the systems' own
    # models live in their configs, and without them the page never showed the
    # providers those systems run on.
    from pathlib import Path

    from web_chat.deployment import Deployment

    routing = Path(__file__).resolve().parents[1] / "deploy" / "routing.yaml"
    merged = Deployment(routing_path=str(routing)).usage_config()
    # The engineer runs Kimi on Kimi Code (what the Plus membership pays for),
    # not through OpenRouter: the page must show the provider whose key pays.
    assert merged.models["kimi-k3.1-pro"].provider == "kimi-code"
    assert merged.providers["kimi-code"].name == "kimi-code"
    # OpenCode is the systems' own provider: the catalog alone does not name it.
    assert merged.providers["opencode"].name == "opencode"
    assert merged.models["deepseek-flash-latest"].provider == "opencode"
    snapshot = UsageSnapshot(
        lambda: Plans(lambda: CONFIG), lambda: merged, vault=Vault(), clock=lambda: NOW,
        local_cli_login=lambda: True,
    )
    assert "server:opencode" in by_id(snapshot.snapshot(admin()))


@pytest.mark.asyncio
async def test_configured_provider_usage_reports_metered_totals_windows_and_server_openrouter():
    from web_chat.usage import UsageStore

    usage = UsageStore()
    usage.record(
        event_id="e1",
        user_id="u1",
        model="k3.1-pro",
        provider="kimi-code",
        tokens_in=40_000,
        tokens_out=10_000,
        occurred_at=NOW - 300,
        cost_micro=250_000,
        cost_basis="computed",
        credential_source="env",
        charged=True,
    )
    usage.record(
        event_id="e2",
        user_id="u1",
        model="deepseek-v4.1-flash",
        provider="opencode",
        tokens_in=150_000,
        tokens_out=50_000,
        occurred_at=NOW - 600,
        cost_micro=100_000,
        cost_basis="computed",
        credential_source="env",
        charged=True,
    )

    config = GridConfig(
        providers={
            "kimi-code": {
                "name": "Kimi Code",
                "base_url": "https://api.kimi.ai/coding/v1",
                "api_key_env": "KIMI_CODE_API_KEY",
                "usage": {
                    "mode": "metered",
                    "windows": {"five_hour": 100_000},
                    "limits": {"day_usd": 5.0},
                },
            },
            "opencode": {
                "name": "OpenCode Go",
                "base_url": "https://opencode.ai/zen/go/v1",
                "api_key_env": "OPENCODE_API_KEY",
                "usage": {"mode": "metered"},
            },
            "openrouter": {
                "name": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "usage": {"mode": "openrouter_key"},
            },
        },
        models={
            "kimi-k3.1-pro": {"name": "k3.1-pro", "provider": "kimi-code"},
            "deepseek-flash-latest": {"name": "deepseek-v4.1-flash", "provider": "opencode"},
            "router": {"name": "typesafe/jev-1.13", "provider": "openrouter"},
        },
    )

    snapshot = UsageSnapshot(
        lambda: Plans(lambda: config),
        lambda: config,
        vault=Vault(),
        usage=usage,
        environ={
            "KIMI_CODE_API_KEY": "sk-kimi-server",
            "OPENCODE_API_KEY": "sk-opencode-server",
            "OPENROUTER_API_KEY": "sk-or-server",
        },
        key_transport=httpx.MockTransport(lambda r: httpx.Response(200, json=OPENROUTER)),
        clock=lambda: NOW,
    )

    state = await snapshot.snapshot_live(admin())
    sources = by_id(state)

    kimi_srv = sources["server:kimi-code"]
    assert kimi_srv["connected"] is True
    assert kimi_srv["status"] == "estimate"
    assert kimi_srv["windows"][0]["used"] == 50.0
    assert kimi_srv["limits"]["usage_daily_usd"] == pytest.approx(0.25)
    assert kimi_srv["limits"]["day_limit_usd"] == 5.0
    assert kimi_srv["metered"]["day"]["total_tokens"] == 50_000
    assert kimi_srv["metered"]["day"]["responses"] == 1

    opencode_srv = sources["server:opencode"]
    assert opencode_srv["connected"] is True
    assert opencode_srv["status"] == "live"
    assert opencode_srv["metered"]["day"]["total_tokens"] == 200_000

    or_srv = sources["server:openrouter"]
    assert or_srv["connected"] is True
    assert or_srv["status"] == "live"
    assert or_srv["limits"]["remaining_usd"] == 7.5
    assert "sk-" not in json.dumps(state)

    # Non-admin users never get the operator's OpenRouter balance or refresh action.
    user_sources = by_id(snapshot.snapshot(user()))
    assert user_sources["server:openrouter"]["action"] is None
    assert user_sources["server:openrouter"]["limits"] is None
    with pytest.raises(SnapshotError, match="No source"):
        await snapshot.refresh(user(), "server:openrouter")
    usage.close()
