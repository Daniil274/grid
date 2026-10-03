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
    record(store, cached_in=70, reasoning_out=12)
    record(store, cached_in=70, reasoning_out=12)
    since = report(store)["coverage"]["detailed_since"]
    store.close()
    reopened = UsageStore(path)
    result = report(reopened)
    assert result["summary"] == dict(tokens_in=100, tokens_out=20, total_tokens=120, cached_in=70, reasoning_out=12, responses=1)
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


def config():
    agents = {
        "parent": AgentConfig(name="Parent", model=["primary", "fallback"]),
        "child": AgentConfig(name="Child", model="small"),
    }
    models = {
        "primary": SimpleNamespace(name="alpha", provider="p"),
        "fallback": SimpleNamespace(name="beta", provider="other"),
        "small": SimpleNamespace(name="tiny", provider="q"),
    }
    return SimpleNamespace(get_agent=agents.__getitem__, get_model=models.__getitem__)


def completed(model, usage):
    return RawResponsesStreamEvent(data={"type": "response.completed", "response": {
        "id": "__fake_id__", "model": model, "usage": usage,
    }})


class Session:
    def __init__(self, store):
        self.space = SimpleNamespace(usage=store, user_id="alice")


def test_stream_accounts_every_response_and_nested_agent_with_fallback_provider():
    store = UsageStore()
    session = Session(store)
    turn = AgentTurn(session, "hello", system_key=None, agent_key=None)
    turn._resolution = SimpleNamespace(config=config(), agent="parent")
    usage = {"input_tokens": 100, "output_tokens": 20,
             "input_tokens_details": {"cached_tokens": 70},
             "output_tokens_details": {"reasoning_tokens": 12}}
    turn._observer.handle_event(completed("alpha", usage), agent_key="parent")
    turn._observer.handle_event(completed("beta", usage), agent_key="parent")
    child = turn._observer.nested("Child")
    child.handle_event(completed("", usage), agent_key="child")
    today = datetime.now(timezone.utc).date()
    result = report(store, start=today, end=today)
    assert {(row["model"], row["provider"]) for row in result["models"]} == {("alpha", "p"), ("beta", "other"), ("tiny", "q")}
    assert result["summary"]["total_tokens"] == 360
    assert result["summary"]["responses"] == 3
    assert result["summary"]["cached_in"] == 210
    assert result["summary"]["reasoning_out"] == 36
    assert turn._recorder.tokens_in == 300
    assert turn._recorder.tokens_out == 60


def test_unidentified_models_stay_unknown_and_missing_usage_not_counted():
    store = UsageStore()
    turn = AgentTurn(Session(store), "hello", system_key=None, agent_key=None)
    turn._resolution = SimpleNamespace(config=config(), agent="parent")
    turn._observer.handle_event(completed("alpha", None))
    turn._observer.handle_event(completed("", {"prompt_tokens": 5, "completion_tokens": 2}))
    today = datetime.now(timezone.utc).date()
    result = report(store, start=today, end=today)
    assert result["models"][0]["model"] == UNKNOWN_MODEL
    assert result["summary"]["total_tokens"] == 7
    assert result["summary"]["responses"] == 1


def test_analytics_failure_does_not_interrupt_trace_accounting():
    def fail(**kwargs):
        raise OSError("disk failure")
    turn = AgentTurn(Session(SimpleNamespace(record=fail)), "hello", system_key=None, agent_key=None)
    turn._observer.handle_event(completed("alpha", {"input_tokens": 5, "output_tokens": 2}))
    assert turn._recorder.tokens_in == 5 and turn._recorder.tokens_out == 2
