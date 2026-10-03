"""Durable token analytics, access control, and real stream accounting."""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from agents import RawResponsesStreamEvent

from schemas.schemas import AgentConfig
from tests.test_web_accounts import accounts, cheap_hashing, client, server, signed_in, PASSWORD
from web_chat.session import AgentTurn
from web_chat.usage import UsageStore, UNKNOWN_MODEL


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def record(store, event="1", user="alice", model="alpha", provider="p", at="2026-10-03T12:00:00", **counts):
    store.record(event_id=event, user_id=user, model=model, provider=provider,
                 occurred_at=stamp(at), tokens_in=counts.pop("tokens_in", 100),
                 tokens_out=counts.pop("tokens_out", 20), **counts)


def report(store, **options):
    return store.report(user_id=options.pop("user_id", "alice"),
                        start=options.pop("start", date(2026, 10, 3)),
                        end=options.pop("end", date(2026, 10, 3)), **options)


def test_persistence_idempotency_and_component_totals(tmp_path):
    path = tmp_path / "usage.db"
    store = UsageStore(path)
    record(store, cached_in=70, reasoning_out=12, cost_micro=2500, cost_basis="reported")
    record(store, cached_in=70, reasoning_out=12, cost_micro=2500, cost_basis="reported")
    since = report(store)["coverage"]["detailed_since"]
    store.close()
    reopened = UsageStore(path)
    result = report(reopened)
    assert result["summary"] == dict(
        tokens_in=100, tokens_out=20, total_tokens=120, cached_in=70, reasoning_out=12, responses=1,
        cost_micro=2500, cost_usd=0.0025, charged_micro=2500, charged_usd=0.0025,
    )
    assert result["coverage"]["detailed_since"] == since
    assert len(result["series"]) == 24
    assert result["series"][12]["total_tokens"] == 120
    reopened.close()


def test_utc_boundaries_user_and_provider_filters():
    store = UsageStore()
    record(store, "before", at="2026-10-02T23:59:59")
    record(store, "first", at="2026-10-03T00:00:00")
    record(store, "last", provider="other", at="2026-10-03T23:59:59")
    record(store, "after", at="2026-10-04T00:00:00")
    record(store, "bob", user="bob", model="secret")
    result = report(store)
    assert result["summary"]["responses"] == 2
    assert len(result["models"]) == 2
    assert all(row["model"] != "secret" for row in result["model_options"])
    assert report(store, model=("other", "alpha"))["summary"]["responses"] == 1
    assert report(store, user_id=None)["summary"]["responses"] == 3
    assert sum(row["total_tokens"] for row in result["series"]) == result["summary"]["total_tokens"]


def test_legacy_baseline_imported_once_and_never_fabricates_hour_or_model(tmp_path):
    path = tmp_path / "usage.db"
    store = UsageStore(path)
    rows = [{"user_id": "alice", "day": "2026-10-03", "tokens_in": 900, "tokens_out": 100}]
    store.import_legacy(rows)
    record(store)
    store.close()
    store = UsageStore(path)
    store.import_legacy([{**rows[0], "tokens_in": 9000}])
    result = report(store)
    assert result["summary"]["total_tokens"] == 1120
    assert result["summary"]["responses"] == 1
    assert result["coverage"]["legacy_omitted_from_chart"] is True
    assert sum(row["total_tokens"] for row in result["series"]) == 120
    assert report(store, bucket="day")["series"][0]["total_tokens"] == 1120
    assert report(store, model=("", UNKNOWN_MODEL))["summary"]["total_tokens"] == 1000


def test_bad_legacy_import_rolls_back_and_can_be_retried():
    store = UsageStore()
    row = {"user_id": "alice", "day": "2026-10-03", "tokens_in": 50, "tokens_out": 10}
    with pytest.raises(ValueError):
        store.import_legacy([row, {**row, "day": "not-a-date"}])
    assert report(store)["summary"]["total_tokens"] == 0
    store.import_legacy([row])
    assert report(store)["summary"]["total_tokens"] == 60


def test_zero_fill_weekly_grouping_and_clamped_detail_counts():
    store = UsageStore()
    record(store, cached_in=900, reasoning_out=900)
    result = report(store, start=date(2026, 10, 1), end=date(2026, 10, 15), bucket="week")
    assert [row["total_tokens"] for row in result["series"]] == [120, 0, 0]
    assert result["summary"]["cached_in"] == 100
    assert result["summary"]["reasoning_out"] == 20
    assert report(store, model=("p", "absent"))["models"] == []


@pytest.mark.parametrize("options", [
    {"end": date(2026, 10, 2)}, {"bucket": "minute"},
    {"start": date(2000, 1, 1)},
    {"start": date(2026, 1, 1), "bucket": "hour"},
    {"start": date(9999, 12, 30), "end": date.max},
])
def test_invalid_ranges_are_rejected(options):
    with pytest.raises(ValueError):
        report(UsageStore(), **options)


def test_api_authentication_and_private_page(client):
    assert client.get("/api/usage").status_code == 401
    page = client.get("/usage", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"


def test_api_user_isolation_and_admin_server_scope(server, accounts):
    alice = accounts.create_user("alice", PASSWORD)
    bob = accounts.create_user("bob", PASSWORD)
    accounts.create_user("root", PASSWORD, role="admin")
    record(server.usage, user=alice.id)
    record(server.usage, "bob", user=bob.id, model="secret")
    user = signed_in(server, "alice")
    params = {"start": "2026-10-03", "end": "2026-10-03", "user_id": bob.id}
    result = user.get("/api/usage", params=params)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["summary"]["total_tokens"] == 120
    assert result.json()["can_view_all"] is False
    assert "secret" not in result.text
    assert user.get("/usage").status_code == 200
    assert user.get("/api/usage", params={**params, "scope": "all"}).status_code == 403
    admin = signed_in(server, "root")
    result = admin.get("/api/usage", params={**params, "scope": "all"}).json()
    assert result["summary"]["total_tokens"] == 240
    assert result["can_view_all"] is True
    filtered = admin.get("/api/usage", params={**params, "scope": "all", "model": json.dumps(["p", "secret"])}).json()
    assert filtered["summary"]["total_tokens"] == 120


@pytest.mark.parametrize("params,code", [
    ({"model": "broken"}, 400), ({"model": '["p", 10]'}, 400),
    ({"model": '"name"'}, 400), ({"start": "invalid"}, 422),
    ({"bucket": "minute"}, 422), ({"start": "2026-10-03", "end": "2026-10-01"}, 400),
])
def test_api_bad_filters_are_client_errors(server, accounts, params, code):
    accounts.create_user("alice", PASSWORD)
    assert signed_in(server, "alice").get("/api/usage", params=params).status_code == code


def completed(model, usage):
    return RawResponsesStreamEvent(data={"type": "response.completed", "response": {
        "id": "__fake_id__", "model": model, "usage": usage,
    }})


class Session:
    def __init__(self, store):
        self.space = SimpleNamespace(usage=store, user_id="alice")


def test_the_turn_still_counts_its_tokens_from_the_stream_but_the_record_comes_from_the_meter():
    store = UsageStore()
    turn = AgentTurn(Session(store), "hello", system_key=None, agent_key=None)
    usage = {"input_tokens": 100, "output_tokens": 20}
    turn._observer.handle_event(completed("alpha", usage), agent_key="parent")
    turn._observer.nested("Child").handle_event(completed("", usage), agent_key="child")
    assert (turn._recorder.tokens_in, turn._recorder.tokens_out) == (200, 40)
    # Money is recorded where it is spent (core.metering), once: not again from the stream.
    today = datetime.now(timezone.utc).date()
    assert report(store, start=today, end=today)["summary"]["responses"] == 0


def test_the_meter_records_each_call_for_the_user_and_adds_it_to_the_turn_in_progress():
    from core.model_access import SpendEvent
    from core.pricing import COMPUTED, Cost, TokenUsage
    from web_chat.spend import SpaceMeter, TurnSpend, tracking

    store = UsageStore()
    meter = SpaceMeter("alice", store)
    own = SpendEvent("p", "alpha", TokenUsage(100, 20, cached=70, reasoning=12), Cost(2500, COMPUTED), "pool:friends", True, False)
    free = SpendEvent("p", "beta", TokenUsage(10, 5), Cost(900, COMPUTED), "own", False, False)
    stops = []
    turn = TurnSpend(stops.append)
    with tracking(turn):
        meter(own)
        meter(free)
    meter(own)  # outside any turn: recorded, counted in no turn
    assert (turn.charged_micro, turn.total_micro, stops) == (2500, 3400, [2500])
    today = datetime.now(timezone.utc).date()
    summary = report(store, start=today, end=today)["summary"]
    assert (summary["responses"], summary["cached_in"], summary["reasoning_out"]) == (3, 140, 24)
    assert (summary["cost_micro"], summary["charged_micro"]) == (5900, 5000)


def test_a_failing_store_never_breaks_a_call():
    from core.model_access import SpendEvent
    from core.pricing import COMPUTED, Cost, TokenUsage
    from web_chat.spend import SpaceMeter

    def fail(**kwargs):
        raise OSError("disk failure")

    SpaceMeter("alice", SimpleNamespace(record=fail))(
        SpendEvent("p", "m", TokenUsage(1, 1), Cost(1, COMPUTED), "env", True, False)
    )


def test_spend_counts_only_what_the_operator_was_charged_since_a_moment():
    store = UsageStore()
    record(store, "a", at="2026-10-03T10:00:00", cost_micro=1000, credential_source="pool:friends")
    record(store, "b", at="2026-10-03T12:00:00", cost_micro=4000, credential_source="pool:friends")
    record(store, "c", at="2026-10-03T12:30:00", cost_micro=9000, credential_source="own", charged=False)
    record(store, "d", at="2026-10-03T12:30:00", user="bob", cost_micro=7000)
    assert store.spent_micro("alice", stamp("2026-10-03T00:00:00")) == 5000
    assert store.spent_micro("alice", stamp("2026-10-03T11:00:00")) == 4000
    assert store.spent_micro("nobody", 0) == 0
    summary = report(store)["summary"]
    assert (summary["cost_micro"], summary["charged_micro"]) == (14000, 5000)


def test_a_database_from_before_money_gains_the_cost_columns(tmp_path):
    import sqlite3

    path = tmp_path / "usage.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE usage_events (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, occurred_at REAL NOT NULL,
            model TEXT NOT NULL, provider TEXT NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL,
            cached_in INTEGER NOT NULL, reasoning_out INTEGER NOT NULL, responses INTEGER NOT NULL, source TEXT NOT NULL);
        INSERT INTO usage_events VALUES ('x', 'alice', 1790000000, 'm', 'p', 10, 5, 0, 0, 1, 'response');
    """)
    old.commit()
    old.close()
    store = UsageStore(path)
    assert store.spent_micro("alice", 0) == 0  # old rows count as free, charged
    record(store, "y", cost_micro=300)
    assert store.spent_micro("alice", 0) == 300
