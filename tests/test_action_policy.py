"""Contract tests for the policy gate.

The gate mediates every call it is configured for: function tools, delegation to
another agent and MCP. Nothing about a particular tool is special-cased, so the
tests use arbitrary tool names and argument shapes. Decisions are mocked; no
provider key and no network request are needed.
"""

import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from core.action_policy import (
    ActionGate,
    ActionRunState,
    ActionValidator,
    merge,
)
from schemas.action_policy import ActionPolicyConfig, ActionValidatorConfig

TASK = "Find the failing test and report which assertion breaks"


def validator_root_config():
    """Exercise the project's own model/provider registry, not a duplicate."""
    from core.config.config import Config

    root = Config("routing.yaml")
    root.config.settings.action_policy = root.config.settings.action_policy.model_copy(
        update={
            "mode": "enforce",
            "validator": ActionValidatorConfig(model="router"),
        }
    )
    return root


def answer(choice="allow", **changes):
    body = {
        "type": "choice",
        "choice": choice,
        "confidence": 1,
        "probabilities": {"allow": 1, "deny": 0, "review": 0},
    }
    body.update(changes)
    return body


def response_answers(**answers):
    return {"answers": {key: value for key, value in answers.items()}}


def setup_gate(verdicts=("allow", "allow"), task=TASK, **settings):
    """A gate with a stubbed validator and a live run state."""
    action, chain = verdicts
    validator = SimpleNamespace(
        evaluate=AsyncMock(return_value={"action": action, "chain": chain})
    )
    gate = ActionGate(
        ActionPolicyConfig(mode="enforce", **settings), validator=validator
    )
    state = ActionRunState(task=task)
    ctx = SimpleNamespace(context=SimpleNamespace(action_state=state, factory=None))
    return gate, validator, state, ctx


async def echo(ctx, args):
    return f"ran:{args}"


async def call(gate, ctx, *, tool="bash_tool", kind="function", args=None, invoke=echo):
    return await gate.invoke(
        tool, kind, ctx, json.dumps(args or {"command": "pytest -q"}), invoke
    )


async def test_allowed_call_executes_and_audits_without_arguments(caplog):
    gate, validator, state, ctx = setup_gate()
    with caplog.at_level("INFO", logger="grid.action_policy"):
        result = await call(gate, ctx, args={"command": "pytest -q --secret=abc"})
    assert result.startswith("ran:")
    packet = validator.evaluate.call_args.args[0]
    assert packet["trusted_task"] == TASK
    assert packet["untrusted_action"] == {
        "kind": "function",
        "tool": "bash_tool",
        "arguments": {"command": "pytest -q --secret=abc"},
        "tool_metadata": {},
    }
    audit = [r.getMessage() for r in caplog.records]
    assert audit and all("secret" not in line for line in audit)
    assert [(e["tool"], e["outcome"]) for e in state.chain] == [
        ("bash_tool", "executed")
    ]


@pytest.mark.parametrize(
    "tool,kind,args",
    [
        ("bash_tool", "function", {"command": "rm -rf /"}),
        ("call_coordinator", "agent", {"input": "do the whole project"}),
        ("codegraph_search", "mcp", {"query": "AgentFactory"}),
        ("project_specific_tool", "function", {"anything": [1, 2, 3]}),
    ],
)
async def test_every_configured_kind_is_checked(tool, kind, args):
    gate, validator, state, ctx = setup_gate()
    await call(gate, ctx, tool=tool, kind=kind, args=args)
    packet = validator.evaluate.call_args.args[0]
    assert packet["untrusted_action"]["tool"] == tool
    assert packet["untrusted_action"]["kind"] == kind
    assert packet["untrusted_action"]["arguments"] == args


async def test_kinds_not_configured_run_unchecked():
    gate, validator, state, ctx = setup_gate(kinds=("function",))
    assert (
        await call(gate, ctx, tool="worker", kind="agent")
        == 'ran:{"command": "pytest -q"}'
    )
    validator.evaluate.assert_not_awaited()
    assert not state.chain


async def test_denied_call_is_blocked_and_reported_to_the_agent():
    gate, _, state, ctx = setup_gate(("deny", "allow"))
    blocked = json.loads(await call(gate, ctx))
    assert blocked["status"] == "blocked"
    assert blocked["rule"] == "policy_deny"
    assert state.denials == 1
    assert [(e["tool"], e["outcome"]) for e in state.chain] == [
        ("bash_tool", "blocked")
    ]


async def test_chain_verdict_alone_blocks_the_call():
    gate, _, state, ctx = setup_gate(("allow", "deny"))
    assert json.loads(await call(gate, ctx))["rule"] == "policy_deny"


async def test_repeated_denials_stop_the_run():
    gate, _, state, ctx = setup_gate(("deny", "deny"), max_denials_per_run=2)
    await call(gate, ctx)
    assert json.loads(await call(gate, ctx))["run_stopped"] is True
    assert json.loads(await call(gate, ctx))["rule"] == "run_stopped"


async def test_shadow_records_the_verdict_and_keeps_running(caplog):
    gate, _, state, ctx = setup_gate(("deny", "deny"))
    gate.config = gate.config.model_copy(update={"mode": "shadow"})
    with caplog.at_level("INFO", logger="grid.action_policy"):
        assert (await call(gate, ctx)).startswith("ran:")
    decisions = [
        json.loads(r.getMessage().split(" ", 1)[1])["decision"] for r in caplog.records
    ]
    assert decisions == ["deny", "executed"]


async def test_shadow_counts_denials_and_stops_after_the_budgeted_call():
    gate, _, state, ctx = setup_gate(("deny", "deny"), max_denials_per_run=2)
    gate.config = gate.config.model_copy(update={"mode": "shadow"})

    assert (await call(gate, ctx)).startswith("ran:")
    assert state.denials == 1 and state.stopped is False
    assert (await call(gate, ctx)).startswith("ran:")
    assert state.denials == 2 and state.stopped is True
    assert json.loads(await call(gate, ctx))["rule"] == "run_stopped"


async def test_reasoning_is_isolated_between_run_states():
    gate, validator, first, first_ctx = setup_gate()
    second = ActionRunState(task="A different task")
    second_ctx = SimpleNamespace(
        context=SimpleNamespace(action_state=second, factory=None)
    )
    first.reasoning_text = "reasoning for the first run"
    second.reasoning_text = "reasoning for the second run"

    await call(gate, first_ctx)
    first_packet = validator.evaluate.call_args.args[0]
    await call(gate, second_ctx)
    second_packet = validator.evaluate.call_args.args[0]

    assert first_packet["untrusted_chain"]["reasoning_tail"] == first.reasoning_text
    assert second_packet["untrusted_chain"]["reasoning_tail"] == second.reasoning_text


async def test_validator_outage_fails_closed_in_enforce():
    gate, validator, state, ctx = setup_gate()
    validator.evaluate = AsyncMock(side_effect=httpx.ConnectError("no provider"))
    assert json.loads(await call(gate, ctx))["rule"] == "policy_unavailable"


async def test_attempt_limit_stops_the_run():
    gate, _, state, ctx = setup_gate(max_attempts_per_run=1)
    await call(gate, ctx)
    assert json.loads(await call(gate, ctx))["rule"] == "attempt_limit"


async def test_call_without_a_trusted_task_is_refused():
    gate, validator, _, _ = setup_gate()
    bare = SimpleNamespace(context=SimpleNamespace())
    assert json.loads(await call(gate, bare))["rule"] == "missing_trusted_task"
    empty = SimpleNamespace(
        context=SimpleNamespace(action_state=ActionRunState(task=" "))
    )
    assert json.loads(await call(gate, empty))["rule"] == "missing_trusted_task"
    validator.evaluate.assert_not_awaited()


async def test_oversized_arguments_are_rejected_not_truncated():
    gate, validator, _, ctx = setup_gate(max_action_bytes=256)
    blocked = json.loads(await call(gate, ctx, args={"content": "x" * 400}))
    assert blocked["rule"] == "action_size_limit"
    validator.evaluate.assert_not_awaited()


async def test_sensitive_and_large_arguments_are_projected_before_validation():
    gate, validator, state, ctx = setup_gate(max_argument_value_bytes=64)
    await call(
        gate,
        ctx,
        args={
            "filepath": "notes.txt",
            "password": "top-secret",
            "content": "ordinary file content",
            "query": "x" * 100,
            "nested": {"api-key": "credential"},
        },
    )

    arguments = validator.evaluate.call_args.args[0]["untrusted_action"]["arguments"]
    assert arguments["filepath"] == "notes.txt"
    assert arguments["password"]["redacted"] is True
    assert arguments["content"] == "ordinary file content"
    assert arguments["query"]["redacted"] is True
    assert arguments["nested"]["api-key"]["redacted"] is True
    assert state.chain[0]["arguments"] == arguments
    assert "top-secret" not in json.dumps(state.chain[0])


async def test_review_can_be_approved_once_for_the_exact_action():
    gate, validator, state, ctx = setup_gate(("review", "allow"))
    args = {"command": "pytest -q"}
    blocked = json.loads(await call(gate, ctx, args=args))
    approval_id = blocked["approval_id"]

    pending = gate.pending_reviews()
    assert pending == [
        {
            **pending[0],
            "approval_id": approval_id,
            "run_id": state.run_id,
            "tool": "bash_tool",
            "kind": "function",
        }
    ]
    assert "arguments" not in pending[0]
    assert gate.resolve_review(approval_id, approve=True) is True
    assert (await call(gate, ctx, args=args)).startswith("ran:")
    assert json.loads(await call(gate, ctx, args=args))["rule"] == "policy_review"
    assert validator.evaluate.await_count == 3


async def test_approval_does_not_apply_to_a_different_action():
    gate, _, _, ctx = setup_gate(("review", "allow"))
    blocked = json.loads(await call(gate, ctx, args={"command": "safe"}))
    assert gate.resolve_review(blocked["approval_id"], approve=True) is True

    different = json.loads(await call(gate, ctx, args={"command": "different"}))

    assert different["rule"] == "policy_review"
    assert different["approval_id"] != blocked["approval_id"]


async def test_reviewed_attempt_does_not_poison_the_next_chain_decision():
    validator = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                {"action": "review", "chain": "allow"},
                {"action": "allow", "chain": "allow"},
            ]
        )
    )
    gate = ActionGate(ActionPolicyConfig(mode="enforce"), validator=validator)
    state = ActionRunState(task=TASK)
    ctx = SimpleNamespace(context=SimpleNamespace(action_state=state, factory=None))

    blocked = json.loads(
        await call(gate, ctx, tool="bash_tool", args={"command": "git status"})
    )
    allowed = await call(gate, ctx, tool="file_read", args={"filepath": ".gitignore"})

    assert blocked["rule"] == "policy_review"
    assert allowed.startswith("ran:")
    second_packet = validator.evaluate.await_args_list[1].args[0]
    assert second_packet["untrusted_chain"]["executed"] == []
    assert "recent_decisions" not in second_packet["run"]
    assert [event["outcome"] for event in state.chain] == ["blocked", "executed"]


async def test_denied_review_cannot_be_reused():
    gate, _, _, ctx = setup_gate(("review", "allow"))
    blocked = json.loads(await call(gate, ctx))

    assert gate.resolve_review(blocked["approval_id"], approve=False) is True
    assert gate.resolve_review(blocked["approval_id"], approve=True) is False
    assert json.loads(await call(gate, ctx))["rule"] == "policy_review"


def test_tool_metadata_is_forwarded_without_classifying_it():
    tool = SimpleNamespace(
        description="Save a report",
        params_json_schema={
            "type": "object",
            "properties": {"target": {"type": "string"}},
        },
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    )

    descriptor = ActionGate.describe_tool(tool, "save_report", "mcp")

    assert descriptor == {
        "description": "Save a report",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
        },
        "annotations": tool.annotations,
    }


async def test_arbitrary_tool_behavior_is_always_left_to_the_policy_classifier():
    gate, validator, _, ctx = setup_gate(("deny", "allow"))

    blocked = json.loads(await call(gate, ctx, tool="frobnicate_everything"))

    assert blocked["rule"] == "policy_deny"
    validator.evaluate.assert_awaited_once()


async def test_delegation_depth_is_enforced_before_validation():
    gate, validator, state, ctx = setup_gate(max_delegation_depth=1)

    async def outer_delegate(inner_ctx, args):
        nested_ctx = SimpleNamespace(
            context=SimpleNamespace(action_state=state, factory=None, action_depth=1)
        )
        return await call(gate, nested_ctx, tool="nested_worker", kind="agent")

    blocked = json.loads(
        await call(
            gate,
            ctx,
            tool="worker",
            kind="agent",
            invoke=outer_delegate,
        )
    )

    assert blocked["rule"] == "delegation_depth"
    assert validator.evaluate.await_count == 1


async def test_action_hash_binds_tool_kind_and_arguments():
    gate, _, state, ctx = setup_gate()
    await call(gate, ctx, tool="first", args={"value": 1})
    await call(gate, ctx, tool="second", args={"value": 1})

    decisions = [event for event in state.events if event["decision"] == "allow"]
    assert decisions[0]["action_sha256"] != decisions[1]["action_sha256"]


async def test_chain_carries_earlier_calls_and_the_agent_reasoning():
    gate, validator, state, ctx = setup_gate()
    state.reasoning = lambda: "The user asked for a report; I will read the log first."
    await call(gate, ctx, tool="file_read", args={"filepath": "log.txt"})
    await call(gate, ctx, tool="bash_tool", args={"command": "pytest -q"})
    chain = validator.evaluate.call_args.args[0]["untrusted_chain"]
    assert [entry["tool"] for entry in chain["executed"]] == ["file_read"]
    assert chain["reasoning_tail"].endswith("read the log first.")


async def test_chain_window_follows_the_configured_budget():
    gate, validator, state, ctx = setup_gate(max_chain_events=2)
    for index in range(4):
        await call(gate, ctx, args={"command": f"step-{index}"})
    chain = validator.evaluate.call_args.args[0]["untrusted_chain"]
    assert len(chain["executed"]) == 2
    assert chain["dropped_earlier_calls"] == 1
    assert chain["executed"][-1]["arguments"] == {"command": "step-2"}


async def test_chain_bytes_budget_drops_oldest_entries():
    gate, validator, state, ctx = setup_gate(max_chain_bytes=200)
    for index in range(5):
        await call(gate, ctx, args={"command": "x" * 60 + str(index)})
    chain = validator.evaluate.call_args.args[0]["untrusted_chain"]
    assert 0 < len(chain["executed"]) < 5
    assert chain["dropped_earlier_calls"] >= 1


async def test_failed_reasoning_source_does_not_break_the_check():
    gate, validator, state, ctx = setup_gate()

    def broken():
        raise RuntimeError("observer gone")

    state.reasoning = broken
    assert (await call(gate, ctx)).startswith("ran:")
    assert (
        "reasoning_tail" not in validator.evaluate.call_args.args[0]["untrusted_chain"]
    )


async def test_a_mediated_call_may_make_mediated_calls():
    """Delegation runs inside the caller's call; both share one budget and chain."""
    gate, validator, state, ctx = setup_gate()

    async def delegate(inner_ctx, args):
        return await gate.invoke(
            "file_write",
            "function",
            inner_ctx,
            json.dumps({"filepath": "out.md", "content": "done"}),
            echo,
        )

    result = await asyncio.wait_for(
        gate.invoke("worker", "agent", ctx, "{}", delegate), timeout=5
    )
    assert result.startswith("ran:")
    assert [e["tool"] for e in state.chain] == ["file_write", "worker"]
    assert state.attempts == 2


async def test_interrupted_execution_stops_the_run():
    gate, _, state, ctx = setup_gate()

    async def cancelled(inner_ctx, args):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await call(gate, ctx, invoke=cancelled)
    assert state.stopped is True
    assert json.loads(await call(gate, ctx))["rule"] == "run_stopped"


async def test_tool_output_is_never_inspected_or_logged(caplog):
    gate, _, state, ctx = setup_gate()

    async def leaky(inner_ctx, args):
        return "AKIA-SECRET-OUTPUT"

    with caplog.at_level("INFO", logger="grid.action_policy"):
        assert await call(gate, ctx, invoke=leaky) == "AKIA-SECRET-OUTPUT"
    assert all("AKIA" not in record.getMessage() for record in caplog.records)


def test_strictest_verdict_wins():
    assert merge({"action": "allow", "chain": "allow"}) == "allow"
    assert merge({"action": "allow", "chain": "review"}) == "review"
    assert merge({"action": "review", "chain": "deny"}) == "deny"
    assert merge({}) == "review"


async def test_request_uses_the_decisions_contract_with_both_questions(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    seen = {}

    def respond(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json=response_answers(action=answer(), chain=answer())
        )

    validator = ActionValidator.from_config(
        validator_root_config(), transport=httpx.MockTransport(respond)
    )
    assert await validator.evaluate({"trusted_task": "report"}) == {
        "action": "allow",
        "chain": "allow",
    }
    assert seen["url"].endswith("/alpha/decisions")
    assert seen["body"]["state"] == {"trusted_task": "report"}
    assert set(seen["body"]["questions"]) == {"action", "chain"}
    assert (
        seen["body"]["questions"]["action"]["instructions"]
        == validator.prompts.action.instructions
    )
    assert set(seen["body"]["questions"]["chain"]["criteria"]) == {
        "allow",
        "deny",
        "review",
    }


async def test_chain_question_is_omitted_when_disabled(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    seen = {}

    def respond(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response_answers(action=answer()))

    validator = ActionValidator.from_config(
        validator_root_config(), transport=httpx.MockTransport(respond)
    )
    assert await validator.evaluate({}, chain=False) == {"action": "allow"}
    assert set(seen["body"]["questions"]) == {"action"}


@pytest.mark.parametrize(
    "changes",
    [
        {"confidence": True},
        {"confidence": "1"},
        {"choice": "yes"},
        {"type": "text"},
        {"probabilities": {"allow": 0.99}},
        {"probabilities": {"allow": 1, "deny": 1, "review": 0}},
        {"probabilities": {"allow": 0, "deny": 1, "review": 0}},
    ],
)
async def test_malformed_answers_are_rejected(monkeypatch, changes):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    validator = ActionValidator.from_config(
        validator_root_config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=response_answers(action=answer(**changes), chain=answer())
            )
        ),
    )
    with pytest.raises(ValueError):
        await validator.evaluate({})


async def test_classifier_choice_is_not_overridden_by_runtime_thresholds(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    validator = ActionValidator.from_config(
        validator_root_config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=response_answers(
                    action=answer(
                        probabilities={"allow": 0.8, "deny": 0.1, "review": 0.1}
                    ),
                    chain=answer(),
                ),
            )
        ),
    )
    assert (await validator.evaluate({}))["action"] == "allow"


async def test_enabling_the_policy_requires_a_known_validator_model():
    from utils.exceptions import ConfigError

    root = validator_root_config()
    root.config.settings.action_policy = ActionPolicyConfig(mode="enforce")
    with pytest.raises(ConfigError):
        ActionValidator.from_config(root)
    root.config.settings.action_policy = ActionPolicyConfig(
        mode="enforce", validator=ActionValidatorConfig(model="no-such-model")
    )
    with pytest.raises(ConfigError):
        ActionValidator.from_config(root)


async def test_factory_routes_real_tool_calls_through_the_gate(tmp_path, monkeypatch):
    """The gate is wired into the tools an agent actually receives."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    from core.agent_factory import AgentFactory, AutoRunToolContext, GridRunContext
    from core.config.config import Config
    from utils.path_utils import reset_current_factory, set_current_factory

    factory = AgentFactory(
        config=Config("examples/coder/config.yaml", str(tmp_path)),
        working_directory=str(tmp_path),
        tracing_level=None,
        policy_config=validator_root_config(),
    )
    # Tools resolve relative paths through the running factory, as in a real run.
    set_current_factory(factory)
    try:
        gate = factory.action_gate
        assert gate is not None and gate.config.mode == "enforce"
        assert gate.validator.model.model_name == "typesafe/jev-1.13"

        tools = await factory._get_agent_tools(
            factory.config.get_agent(factory.config.get_default_agent())
        )
        by_name = {getattr(tool, "name", ""): tool for tool in tools}
        state = factory._action_state("List the files of this project")
        ctx = AutoRunToolContext(
            GridRunContext(factory=factory, context_id="ctx-test", action_state=state)
        )

        gate.validator = SimpleNamespace(
            evaluate=AsyncMock(return_value={"action": "deny", "chain": "deny"})
        )
        blocked = json.loads(
            await by_name["file_write"].on_invoke_tool(
                ctx, json.dumps({"filepath": "notes.md", "content": "hi"})
            )
        )
        assert blocked["rule"] == "policy_deny"
        assert not (tmp_path / "notes.md").exists()

        gate.validator = SimpleNamespace(
            evaluate=AsyncMock(return_value={"action": "allow", "chain": "allow"})
        )
        await by_name["file_write"].on_invoke_tool(
            ctx, json.dumps({"filepath": "notes.md", "content": "hi"})
        )
        assert (tmp_path / "notes.md").read_text(encoding="utf-8").strip() == "hi"
        assert [entry["outcome"] for entry in state.chain] == ["blocked", "executed"]
    finally:
        reset_current_factory()
        await factory.cleanup()


async def test_mcp_sdk_hook_routes_calls_through_the_context_gate(monkeypatch):
    import core.agent_factory as agent_factory_module

    gate, _, _, ctx = setup_gate(("deny", "deny"))
    ctx.context.factory = SimpleNamespace(action_gate=gate)
    original = AsyncMock(return_value="executed")
    monkeypatch.setattr(agent_factory_module, "_original_invoke_mcp_tool", original)

    result = await agent_factory_module._invoke_mcp_tool_safe.__func__(
        object,
        SimpleNamespace(name="server"),
        SimpleNamespace(name="remote_delete"),
        ctx,
        json.dumps({"path": "out.txt"}),
    )

    assert json.loads(result)["rule"] == "policy_deny"
    original.assert_not_awaited()


async def test_agent_tool_wrapper_routes_delegation_through_the_gate():
    from core.agent_factory import AgentFactory

    gate, validator, _, ctx = setup_gate(("deny", "allow"))
    factory = SimpleNamespace(action_gate=gate)
    ctx.context.factory = factory
    original = AsyncMock(return_value="delegated")
    tool = SimpleNamespace(name="call_worker", on_invoke_tool=original)

    wrapped = AgentFactory._wrap_tool_with_policy(factory, tool, "call_worker", "agent")
    result = await wrapped.on_invoke_tool(ctx, json.dumps({"input": "do it"}))

    assert json.loads(result)["rule"] == "policy_deny"
    action = validator.evaluate.call_args.args[0]["untrusted_action"]
    assert action["kind"] == "agent"
    assert action["tool_metadata"] == {}
    original.assert_not_awaited()


def test_policy_task_includes_prior_user_context_but_not_assistant_text():
    from core.agent_factory import AgentFactory

    factory = object.__new__(AgentFactory)
    factory.action_gate = SimpleNamespace(
        config=ActionPolicyConfig(
            max_task_context_messages=4, max_task_context_bytes=4096
        )
    )
    factory.context_manager = SimpleNamespace(
        get_context_messages=lambda *args, **kwargs: {
            "messages": [
                {"role": "user", "content": "Раздели изменения по темам."},
                {"role": "assistant", "content": "Я изменю ещё и релиз."},
                {"role": "user", "content": "Сделай отдельные коммиты."},
            ]
        }
    )

    task = factory._policy_task("Коммит", "ctx")

    assert "Раздели изменения по темам." in task
    assert "Сделай отдельные коммиты." in task
    assert "Коммит" in task
    assert "изменю ещё и релиз" not in task


def test_policy_task_context_is_bounded_and_keeps_current_instruction():
    from core.agent_factory import AgentFactory

    factory = object.__new__(AgentFactory)
    factory.action_gate = SimpleNamespace(
        config=ActionPolicyConfig(
            max_task_context_messages=2, max_task_context_bytes=256
        )
    )
    factory.context_manager = SimpleNamespace(
        get_context_messages=lambda *args, **kwargs: {
            "messages": [
                {"role": "user", "content": "старое " * 100},
                {"role": "user", "content": "Нужны тематические коммиты."},
            ]
        }
    )

    task = factory._policy_task("Действуй", "ctx")

    assert len(task.encode("utf-8")) <= 256
    assert "Нужны тематические коммиты." in task
    assert "Действуй" in task


async def test_commit_followup_can_run_git_status_with_full_user_context(tmp_path):
    """Regression for ctx-919bf723: no command-name exception is involved."""
    from core.agent_factory import AgentFactory

    subprocess.run(
        ["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True
    )
    validator = SimpleNamespace(
        evaluate=AsyncMock(return_value={"action": "allow", "chain": "allow"})
    )
    gate = ActionGate(ActionPolicyConfig(mode="enforce"), validator=validator)
    factory = object.__new__(AgentFactory)
    factory.action_gate = gate
    factory.context_manager = SimpleNamespace(
        get_context_messages=lambda *args, **kwargs: {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Вынеси временные артефакты в .gitignore, раздели "
                        "изменения по темам и зафиксируй каждую тему."
                    ),
                },
                {"role": "assistant", "content": "Инструменты заблокированы."},
                {"role": "user", "content": "Коммит"},
            ]
        }
    )
    task = factory._policy_task("Работай", "ctx-919bf723")
    state = ActionRunState(task=task)
    ctx = SimpleNamespace(context=SimpleNamespace(action_state=state, factory=factory))

    async def run_git_status(_ctx, raw_args):
        assert json.loads(raw_args)["command"] == "git status --short"
        process = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--short",
            cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        return stdout.decode()

    result = await gate.invoke(
        "bash_tool",
        "function",
        ctx,
        json.dumps({"command": "git status --short"}),
        run_git_status,
    )

    assert result == ""
    packet = validator.evaluate.call_args.args[0]
    assert "Вынеси временные артефакты" in packet["trusted_task"]
    assert "Коммит" in packet["trusted_task"]
    assert "Работай" in packet["trusted_task"]
    assert "Инструменты заблокированы" not in packet["trusted_task"]
    assert state.chain[-1]["outcome"] == "executed"


async def test_policy_stays_off_without_operator_configuration(tmp_path, monkeypatch):
    from core.agent_factory import AgentFactory
    from core.config.config import Config

    monkeypatch.setenv("POLICY_TEST_KEY", "test-key")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
settings:
  default_agent: assistant
providers:
  test:
    name: test
    base_url: https://example.com/v1
    api_key_env: POLICY_TEST_KEY
models:
  assistant:
    name: assistant
    provider: test
agents:
  assistant:
    name: Assistant
    model: assistant
    tools: []
""",
        encoding="utf-8",
    )
    factory = AgentFactory(
        config=Config(str(config_path), str(tmp_path)),
        working_directory=str(tmp_path),
        tracing_level=None,
    )
    try:
        assert factory.action_gate is None
        assert factory._action_state("anything") is None
    finally:
        await factory.cleanup()


def test_action_policy_can_be_loaded_from_a_separate_yaml(tmp_path, monkeypatch):
    from core.config.config import Config

    monkeypatch.setenv("POLICY_TEST_KEY", "test-key")
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    (policy_dir / "actions.yaml").write_text(
        """
version: external-v2
rules:
  - id: workspace
    decision: deny
    when: The action leaves the assigned workspace.
prompts:
  action:
    instructions: Judge the proposed action against every rule.
    criteria:
      allow: Complies with every rule.
      deny: Clearly violates a deny rule.
      review: Authorization is ambiguous.
  chain:
    instructions: Judge the complete action chain against every rule.
    criteria:
      allow: The chain complies with every rule.
      deny: The chain clearly violates a deny rule.
      review: The chain is ambiguous.
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
settings:
  default_agent: assistant
  action_policy:
    policy_file: policies/actions.yaml
    mode: enforce
    validator:
      model: validator
providers:
  test:
    name: test
    base_url: https://example.com/v1
    api_key_env: POLICY_TEST_KEY
models:
  validator:
    name: decisions
    provider: test
agents:
  assistant:
    name: Assistant
    model: validator
    tools: []
""",
        encoding="utf-8",
    )

    config = Config(str(config_path))
    policy = config.config.settings.action_policy

    assert policy.mode == "enforce"
    assert policy.version == "external-v2"
    assert policy.rules[0].id == "workspace"
    assert policy.prompts.action.criteria["deny"].startswith("Clearly")
    assert policy.validator.model == "validator"


def test_inline_policy_remains_supported(tmp_path, monkeypatch):
    from core.config.config import Config

    monkeypatch.setenv("POLICY_TEST_KEY", "test-key")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
settings:
  default_agent: assistant
  action_policy:
    mode: shadow
    version: inline-v1
    validator:
      model: validator
providers:
  test:
    name: test
    base_url: https://example.com/v1
    api_key_env: POLICY_TEST_KEY
models:
  validator:
    name: decisions
    provider: test
agents:
  assistant:
    name: Assistant
    model: validator
    tools: []
""",
        encoding="utf-8",
    )

    policy = Config(str(config_path)).config.settings.action_policy

    assert policy.mode == "shadow"
    assert policy.version == "inline-v1"


async def test_policy_events_carry_the_tool_call_id():
    gate, _, state, ctx = setup_gate()
    ctx.tool_call_id = "call-7"
    await call(gate, ctx)
    checks = [e for e in state.events if e["rule"] == "policy_check"]
    assert checks and all(e["call_id"] == "call-7" for e in checks)


async def test_transient_validator_failure_is_retried_once():
    gate, validator, state, ctx = setup_gate()
    request = httpx.Request("POST", "https://validator.test")
    validator.evaluate = AsyncMock(
        side_effect=[
            httpx.HTTPStatusError("busy", request=request, response=httpx.Response(429, request=request)),
            {"action": "allow", "chain": "allow"},
        ]
    )
    assert (await call(gate, ctx)).startswith("ran:")
    check = next(e for e in state.events if e["rule"] == "policy_check")
    assert check["decision"] == "allow"
    assert check["validator_failures"] == ["http_429"]


async def test_validator_outage_records_why_it_failed():
    gate, validator, state, ctx = setup_gate()
    validator.evaluate = AsyncMock(side_effect=httpx.ConnectError("no provider"))
    await call(gate, ctx)
    assert validator.evaluate.await_count == 2
    check = next(e for e in state.events if e["rule"] == "policy_check")
    assert check["validator_failures"] == ["ConnectError", "ConnectError"]
