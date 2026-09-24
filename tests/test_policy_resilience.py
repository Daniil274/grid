"""Outages must neither authorize tools nor masquerade as policy violations."""

import asyncio
import json
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from core.action_policy import ActionGate, ActionRunState, ActionValidator
from core.decisions import DecisionsModel
from core.policy_resilience import PolicyRunner, failure_reason, retry_after
from schemas.action_policy import (
    ActionPolicyConfig,
    ActionPolicyPrompts,
    ActionPolicyQuestion,
    ActionValidatorConfig,
)

ALLOW = {"action": "allow", "chain": "allow"}


def settings(**kwargs):
    return ActionValidatorConfig(
        **{"model": "primary", "retry_backoff_seconds": 0, **kwargs}
    )


def validator(key="primary", **kwargs):
    return SimpleNamespace(
        config=SimpleNamespace(model=key),
        model=SimpleNamespace(model_name=key),
        evaluate=AsyncMock(**({"return_value": ALLOW} | kwargs)),
        fallbacks=[],
    )


def http_error(status, **headers):
    request = httpx.Request("POST", "https://policy.test")
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError(
        "secret provider body", request=request, response=response
    )


def gate_context(primary, config=None, **kwargs):
    gate = ActionGate(
        ActionPolicyConfig(mode="enforce", validator=config or settings(), **kwargs),
        validator=primary,
    )
    run = ActionRunState(task="Inspect the project")
    ctx = SimpleNamespace(context=SimpleNamespace(action_state=run))
    return gate, run, ctx


async def call(gate, ctx, invoke):
    return await gate.invoke("inspect", "function", ctx, "{}", invoke)


async def test_outages_do_not_exhaust_denials_and_recovery_runs_once():
    primary = validator(side_effect=[http_error(503), http_error(503), ALLOW])
    gate, run, ctx = gate_context(primary, max_denials_per_run=1)
    invoke = AsyncMock(return_value="done")
    blocked = json.loads(await call(gate, ctx, invoke))
    assert blocked["rule"] == "policy_unavailable"
    assert blocked["infrastructure_error"] is True
    assert not blocked["run_stopped"] and run.denials == 0
    invoke.assert_not_awaited()
    assert not any(e["decision"] == "deny" for e in run.events)
    assert await call(gate, ctx, invoke) == "done"
    invoke.assert_awaited_once()
    assert run.attempts == 2  # Outages do not remove the anti-loop budget.


async def test_shadow_outages_do_not_exhaust_denials():
    primary = validator(side_effect=httpx.ConnectError("offline"))
    gate, run, ctx = gate_context(primary, max_denials_per_run=1)
    gate.config = gate.config.model_copy(update={"mode": "shadow"})
    invoke = AsyncMock(return_value="done")
    assert await call(gate, ctx, invoke) == "done"
    assert run.denials == 0 and not run.stopped


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_http_failures_are_not_retried(status):
    primary = validator(side_effect=http_error(status))
    runner = PolicyRunner(settings(), [primary])
    result = await runner.evaluate({}, chain=True)
    assert not result.verdicts
    primary.evaluate.assert_awaited_once()


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_http_failures_can_recover(status):
    primary = validator(side_effect=[http_error(status), ALLOW])
    result = await PolicyRunner(settings(), [primary]).evaluate({}, chain=True)
    assert result.verdicts == ALLOW
    assert result.failures == [f"http_{status}"]


@pytest.mark.parametrize("choice", ["allow", "deny", "review"])
async def test_valid_decision_never_uses_the_reserve(choice):
    primary = validator(return_value={"action": choice, "chain": "allow"})
    reserve = validator("reserve")
    result = await PolicyRunner(settings(), [primary, reserve]).evaluate({}, chain=True)
    assert result.verdicts["action"] == choice
    primary.evaluate.assert_awaited_once()
    reserve.evaluate.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [http_error(401), http_error(503), ValueError("bad answer")]
)
async def test_reserve_gets_identical_context_only_after_a_failure(error):
    primary = validator(side_effect=error)
    reserve = validator("reserve", return_value={"action": "review", "chain": "allow"})
    packet = {"trusted_task": "Inspect", "untrusted_action": {"tool": "inspect"}}
    result = await PolicyRunner(settings(), [primary, reserve]).evaluate(
        packet, chain=True
    )
    assert result.verdicts["action"] == "review"
    assert result.model == "reserve"
    primary.evaluate.assert_awaited_once_with(packet, chain=True)
    reserve.evaluate.assert_awaited_once_with(packet, chain=True)
    assert [event["route"] for event in result.attempts] == ["primary", "reserve"]


async def test_incomplete_answer_cannot_authorize_a_tool():
    primary = validator(return_value={"action": "allow"})
    gate, run, ctx = gate_context(primary)
    invoke = AsyncMock()
    assert json.loads(await call(gate, ctx, invoke))["rule"] == "policy_unavailable"
    invoke.assert_not_awaited()
    check = next(e for e in run.events if e["rule"] == "policy_check")
    assert check["validator_failures"] == ["invalid_answer", "invalid_answer"]


async def test_action_only_is_valid_when_chain_is_disabled():
    primary = validator(return_value={"action": "allow"})
    result = await PolicyRunner(settings(), [primary]).evaluate({}, chain=False)
    assert result.verdicts == {"action": "allow"}


async def test_retry_after_exceeding_budget_does_not_send_an_early_retry():
    primary = validator(side_effect=http_error(429, **{"Retry-After": "10"}))
    result = await PolicyRunner(settings(timeout_seconds=0.04), [primary]).evaluate(
        {}, chain=True
    )
    assert not result.verdicts
    assert result.failures == ["http_429", "budget_timeout"]
    primary.evaluate.assert_awaited_once()


async def test_retry_after_allows_immediate_independent_fallback():
    primary = validator(side_effect=http_error(429, **{"Retry-After": "10"}))
    reserve = validator("reserve")
    runner = PolicyRunner(settings(timeout_seconds=0.1), [primary, reserve])
    assert (await runner.evaluate({}, chain=True)).verdicts == ALLOW
    assert (await runner.evaluate({}, chain=True)).verdicts == ALLOW
    primary.evaluate.assert_awaited_once()  # Cooldown survives across judgments.
    assert reserve.evaluate.await_count == 2


async def test_slow_primary_cannot_spend_the_reserves_time_budget():
    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    primary = validator(side_effect=stalled)
    reserve = validator("reserve")
    runner = PolicyRunner(settings(timeout_seconds=0.1), [primary, reserve])
    result = await runner.evaluate({}, chain=True)
    assert result.verdicts == ALLOW and result.failures == ["timeout"]
    assert [e["outcome"] for e in result.attempts] == ["timeout", "answered"]
    reserve.evaluate.assert_awaited_once()


async def test_open_primary_does_not_halve_the_reserves_budget():
    primary = validator(side_effect=http_error(402))

    async def answer(*args, **kwargs):
        await asyncio.sleep(0.13)
        return ALLOW

    reserve = validator("reserve", side_effect=answer)
    runner = PolicyRunner(settings(timeout_seconds=0.22), [primary, reserve])
    assert (await runner.evaluate({}, chain=True)).verdicts == ALLOW
    assert (await runner.evaluate({}, chain=True)).verdicts == ALLOW
    primary.evaluate.assert_awaited_once()


async def test_exhausted_reserve_does_not_execute_and_cannot_consume_approval():
    primary = validator(return_value={"action": "review", "chain": "allow"})
    reserve = validator("reserve", side_effect=http_error(503))
    primary.fallbacks = [reserve]
    gate, run, ctx = gate_context(primary)
    invoke = AsyncMock(return_value="done")
    review = json.loads(await call(gate, ctx, invoke))
    assert gate.resolve_review(review["approval_id"], approve=True)
    primary.evaluate.side_effect = http_error(503)
    assert json.loads(await call(gate, ctx, invoke))["rule"] == "policy_unavailable"
    invoke.assert_not_awaited()
    reserve.evaluate.assert_awaited_once()
    primary.evaluate.side_effect = None
    assert await call(gate, ctx, invoke) == "done"
    invoke.assert_awaited_once()


def test_retry_after_dates_and_invalid_values(monkeypatch):
    import core.policy_resilience as resilience

    monkeypatch.setattr(resilience, "time", SimpleNamespace(time=lambda: 1000))
    date = format_datetime(datetime.fromtimestamp(1007, timezone.utc), usegmt=True)
    assert retry_after(http_error(503, **{"Retry-After": date})) == 7
    for value in ("garbage", "-10", "NaN", "Infinity"):
        assert retry_after(http_error(429, **{"Retry-After": value})) == 0


async def test_backoff_is_applied_before_retry(monkeypatch):
    import core.policy_resilience as resilience

    tick = [100.0]
    delays = []

    async def sleep(delay):
        delays.append(delay)
        tick[0] += delay

    monkeypatch.setattr(resilience, "time", SimpleNamespace(monotonic=lambda: tick[0]))
    monkeypatch.setattr(
        resilience,
        "asyncio",
        SimpleNamespace(
            Semaphore=asyncio.Semaphore,
            wait_for=asyncio.wait_for,
            sleep=sleep,
            TimeoutError=asyncio.TimeoutError,
            CancelledError=asyncio.CancelledError,
        ),
    )
    primary = validator(side_effect=[http_error(503), ALLOW])
    result = await PolicyRunner(
        settings(retry_backoff_seconds=0.2), [primary]
    ).evaluate({}, chain=True)
    assert result.verdicts == ALLOW
    assert len(delays) == 1 and 0.1 <= delays[0] <= 0.2


async def test_circuit_skips_broken_route_then_allows_one_recovery_probe(monkeypatch):
    import core.policy_resilience as resilience

    tick = [100.0]
    monkeypatch.setattr(resilience, "time", SimpleNamespace(monotonic=lambda: tick[0]))
    primary = validator(side_effect=http_error(503))
    runner = PolicyRunner(
        settings(circuit_failure_threshold=1, circuit_cooldown_seconds=5), [primary]
    )
    assert not (await runner.evaluate({}, chain=True)).verdicts
    assert not (await runner.evaluate({}, chain=True)).verdicts
    primary.evaluate.assert_awaited_once()
    tick[0] += 5
    entered, release = asyncio.Event(), asyncio.Event()

    async def recover(*args, **kwargs):
        entered.set()
        await release.wait()
        return ALLOW

    primary.evaluate.side_effect = recover
    pending = asyncio.create_task(runner.evaluate({}, chain=True))
    await asyncio.wait_for(entered.wait(), 1)
    assert not (await runner.evaluate({}, chain=True)).verdicts
    assert primary.evaluate.await_count == 2
    release.set()
    assert (await pending).verdicts == ALLOW
    assert (await runner.evaluate({}, chain=True)).verdicts == ALLOW


async def test_concurrency_is_bounded():
    active = peak = 0

    async def evaluate(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.01)
        active -= 1
        return ALLOW

    runner = PolicyRunner(
        settings(max_concurrency=2), [validator(side_effect=evaluate)]
    )
    results = await asyncio.gather(*(runner.evaluate({}, chain=True) for _ in range(8)))
    assert peak == 2 and all(r.verdicts == ALLOW for r in results)
    assert any(r.queue_ms > 0 for r in results)


async def test_older_inflight_success_does_not_clear_newer_outage():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def overlapping(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return ALLOW
        raise http_error(429, **{"Retry-After": "10"})

    runner = PolicyRunner(
        settings(max_attempts=1, circuit_failure_threshold=1),
        [validator(side_effect=overlapping)],
    )
    older = asyncio.create_task(runner.evaluate({}, chain=True))
    await asyncio.wait_for(entered.wait(), 1)
    assert not (await runner.evaluate({}, chain=True)).verdicts
    release.set()
    assert (await older).verdicts == ALLOW
    assert not (await runner.evaluate({}, chain=True)).verdicts
    assert calls == 2


async def test_queue_wait_uses_the_total_budget():
    primary = validator()
    runner = PolicyRunner(settings(max_concurrency=1, timeout_seconds=0.03), [primary])
    async with runner._slots:
        result = await runner.evaluate({}, chain=True)
    assert result.failures == ["queue_timeout"]
    assert result.queue_ms >= 20 and not result.attempts
    primary.evaluate.assert_not_awaited()


async def test_cancellation_propagates_without_executing_or_leaking_slot():
    entered = asyncio.Event()

    async def stalled(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    primary = validator(side_effect=stalled)
    gate, run, ctx = gate_context(primary, settings(max_concurrency=1))
    invoke = AsyncMock(return_value="done")
    pending = asyncio.create_task(call(gate, ctx, invoke))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    invoke.assert_not_awaited()
    assert not run.events and run.denials == 0
    primary.evaluate.side_effect = None
    assert await call(gate, ctx, invoke) == "done"


async def test_audit_excludes_provider_messages_and_includes_attempt_metrics(caplog):
    primary = validator(side_effect=[ValueError("private-payload"), ALLOW])
    gate, run, ctx = gate_context(primary)
    with caplog.at_level("INFO", logger="grid.action_policy"):
        await call(gate, ctx, AsyncMock(return_value="done"))
    check = next(e for e in run.events if e["rule"] == "policy_check")
    assert check["validator_model"] == "primary"
    assert len(check["validator_attempts"]) == 2
    assert all(e["latency_ms"] >= 0 for e in check["validator_attempts"])
    assert check["packet_bytes"] > 0
    assert "private-payload" not in caplog.text
    assert failure_reason(KeyError("private")) == "invalid_answer"


async def test_http_client_is_reused_and_closed_even_with_injected_proxy():
    question = ActionPolicyQuestion(
        instructions="Decide", criteria={"allow": "Yes", "deny": "No", "review": "Ask"}
    )
    choice = {
        "type": "choice",
        "choice": "allow",
        "confidence": 1,
        "probabilities": {"allow": 1, "deny": 0, "review": 0},
    }
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200, json={"answers": {"action": choice, "chain": choice}}
        )

    primary = ActionValidator(
        settings(),
        DecisionsModel(
            "https://policy.test", "test", "primary", proxy="http://127.0.0.1:1"
        ),
        ActionPolicyPrompts(action=question, chain=question),
        transport=httpx.MockTransport(respond),
    )
    gate, _, ctx = gate_context(primary)
    try:
        await call(gate, ctx, AsyncMock())
        client = primary._client
        await call(gate, ctx, AsyncMock())
        assert primary._client is client and len(seen) == 2
    finally:
        await gate.aclose()
    assert client.is_closed
    await gate.aclose()  # Cleanup is idempotent.


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fallback_models": ("primary",)},
        {"fallback_models": ("",)},
        {"fallback_models": ("one", "two"), "max_attempts": 2},
        {"max_concurrency": 0},
        {"max_attempts": 0},
        {"retry_backoff_seconds": -1},
    ],
)
def test_invalid_delivery_configuration_is_rejected(kwargs):
    with pytest.raises(ValidationError):
        settings(**kwargs)


def test_fallbacks_are_loaded_from_operator_registry_and_unknown_keys_fail():
    from core.config.config import Config
    from utils.exceptions import ConfigError

    root = Config("routing.yaml")
    policy = root.config.settings.action_policy
    root.config.settings.action_policy = policy.model_copy(
        update={
            "validator": settings(model="policy", fallback_models=("router",)),
        }
    )
    primary = ActionValidator.from_config(root)
    assert len(primary.fallbacks) == 1
    assert primary.fallbacks[0].config.model == "router"
    assert primary.fallbacks[0].prompts == primary.prompts
    root.config.settings.action_policy = policy.model_copy(
        update={
            "validator": settings(model="policy", fallback_models=("missing-reserve",)),
        }
    )
    with pytest.raises(ConfigError):
        ActionValidator.from_config(root)


async def test_reserve_decisions_transport_blocks_and_both_clients_are_closed():
    question = ActionPolicyQuestion(
        instructions="Decide",
        criteria={"allow": "Yes", "deny": "No", "review": "Ask"},
    )
    prompts = ActionPolicyPrompts(action=question, chain=question)
    config = settings(fallback_models=("reserve",))
    seen = []
    deny = {
        "type": "choice",
        "choice": "deny",
        "confidence": 1,
        "probabilities": {"allow": 0, "deny": 1, "review": 0},
    }

    def respond(request):
        seen.append((request.url.host, json.loads(request.content)))
        if request.url.host == "primary.test":
            return httpx.Response(503)
        return httpx.Response(200, json={"answers": {"action": deny, "chain": deny}})

    primary = ActionValidator(
        config,
        DecisionsModel("https://primary.test/alpha/decisions", "test", "primary"),
        prompts,
        transport=httpx.MockTransport(respond),
    )
    reserve = ActionValidator(
        settings(model="reserve"),
        DecisionsModel("https://reserve.test/alpha/decisions", "test", "reserve"),
        prompts,
        transport=httpx.MockTransport(respond),
    )
    primary.fallbacks = [reserve]
    gate, run, ctx = gate_context(primary, config)
    invoke = AsyncMock()
    try:
        blocked = json.loads(await call(gate, ctx, invoke))
        assert blocked["rule"] == "policy_deny" and run.denials == 1
        invoke.assert_not_awaited()
        assert [host for host, _ in seen] == ["primary.test", "reserve.test"]
        assert seen[0][1]["state"] == seen[1][1]["state"]
        assert seen[0][1]["questions"] == seen[1][1]["questions"]
        clients = [primary._client, reserve._client]
    finally:
        await gate.aclose()
    assert all(client.is_closed for client in clients)
