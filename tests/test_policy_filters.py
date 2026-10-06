"""The policy switch: routing calls by declared effect, and asking the user.

Calls are routed before any model is asked (core.action_routing): what the
tool declares it does, the paths it names and what the run took in decide,
under the user's filter, whether a call runs, is judged, waits for the user or
is refused. Decisions are mocked; nothing here reaches a provider.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.action_policy import ActionGate, ActionRunState
from core.action_routing import (
    SECRET,
    UNTRUSTED,
    command_paths,
    is_readonly,
    matches,
    route_call,
)
from schemas.action_policy import ActionPolicyConfig, DEFAULT_READONLY_COMMANDS
from utils.tool_effects import DELEGATE, egress, external, read, run, write

POLICY = ActionPolicyConfig(tool_effects={"codegraph_*": "read"})


def route(effect, arguments=None, *, filter_name="balanced", flows=()):
    _, selected = POLICY.filter(filter_name)
    return route_call(POLICY, selected, effect, arguments or {}, flows, lambda path: path)


# -- routing ---------------------------------------------------------------
@pytest.mark.parametrize(
    "effect,arguments,expected",
    [
        (read("filepath"), {"filepath": "core/app.py"}, "allow"),
        (write("filepath"), {"filepath": "core/app.py"}, "allow"),
        (DELEGATE, {}, "allow"),
        (run("command"), {"command": "pytest -q"}, "judge"),
        (run("command"), {"command": "git status --short && git diff | tail -5"}, "allow"),
        (egress(untrusted=True), {"url": "https://example.com"}, "allow"),
        (external(), {}, "judge"),
        (None, {}, "judge"),
    ],
)
def test_the_balanced_filter_runs_ordinary_work_and_judges_the_rest(effect, arguments, expected):
    assert route(effect, arguments).route == expected


@pytest.mark.parametrize("filter_name,expected", [("balanced", "judge"), ("read_only", "deny"), ("trusted", "judge")])
def test_readonly_shell_external_paths_use_selected_external_route(filter_name, expected):
    import posixpath

    root = "/workspace/project"

    def fake_locator(raw):
        normalized = posixpath.normpath(raw.replace("\\", "/"))
        if normalized == root or normalized.startswith(root + "/"):
            return normalized[len(root):].lstrip("/")
        if not posixpath.isabs(normalized) and not normalized.startswith("../"):
            return normalized
        return None

    _, selected = POLICY.filter(filter_name)
    routing = route_call(POLICY, selected, run("command"), {"command": "cat /etc/shadow"}, (), fake_locator)
    assert routing.effect == "external"
    assert routing.route == expected


def test_readonly_shell_keeps_absolute_workspace_reads_and_secret_review():
    import posixpath

    root = "/workspace/project"
    locate = lambda raw: posixpath.normpath(raw).removeprefix(root + "/")
    _, selected = POLICY.filter("balanced")
    workspace = route_call(POLICY, selected, run("command"), {"command": "cat /workspace/project/src/a.py"}, (), locate)
    secret = route_call(POLICY, selected, run("command"), {"command": "cat /home/example/.ssh/id_rsa"}, (), lambda _: None)
    traversal = route_call(POLICY, selected, run("command"), {"command": "cat /workspace/project/../../etc/shadow"}, (), lambda _: None)
    assert workspace.effect == "read" and workspace.route == "allow"
    assert secret.route == "review" and any(reason.startswith("secret: ") for reason in secret.reasons)
    assert traversal.effect == "external" and traversal.route == "judge"


@pytest.mark.parametrize(
    "effect,arguments,expected",
    [
        (read("filepath"), {"filepath": "core/app.py"}, "allow"),
        (write("filepath"), {"filepath": "core/app.py"}, "deny"),
        (run("command"), {"command": "ls -la"}, "allow"),
        (run("command"), {"command": "pytest -q"}, "deny"),
        (external(), {}, "deny"),
        (DELEGATE, {}, "allow"),
    ],
)
def test_read_only_refuses_every_change(effect, arguments, expected):
    assert route(effect, arguments, filter_name="read_only").route == expected


def test_trusted_runs_commands_and_unknown_tools_but_judges_outside_actions():
    assert route(run("command"), {"command": "rm -rf build"}, filter_name="trusted").route == "allow"
    assert route(None, filter_name="trusted").route == "allow"
    assert route(external(), filter_name="trusted").route == "judge"


def test_an_unknown_filter_name_is_the_default():
    assert POLICY.filter("no-such-filter")[0] == "balanced"
    assert POLICY.filter(None)[0] == "balanced"


@pytest.mark.parametrize(
    "effect,arguments",
    [
        (read("filepath"), {"filepath": ".env"}),
        (read("filepath"), {"filepath": "deploy/keys/server.pem"}),
        (write("filepath"), {"filepath": "config/.env.production"}),
        (run("command"), {"command": "cat .env"}),
    ],
)
def test_secrets_wait_for_the_user_and_mark_the_run(effect, arguments):
    routing = route(effect, arguments)
    assert routing.route == "review"
    assert any(reason.startswith("secret: ") for reason in routing.reasons)
    assert (SECRET in routing.flows) == (effect.kind != "write")


def test_files_that_define_authority_wait_for_the_user_when_changed_only():
    assert route(write("filepath"), {"filepath": "policies/action-policy.yaml"}).route == "review"
    assert route(write("filepath"), {"filepath": ".github/workflows/ci.yml"}).route == "review"
    assert route(read("filepath"), {"filepath": "policies/action-policy.yaml"}).route == "allow"
    assert route(run("command"), {"command": "git diff -- policies/action-policy.yaml"}).route == "allow"


def test_a_stricter_effect_route_is_kept_on_a_protected_path():
    routing = route(write("filepath"), {"filepath": "policies/a.yaml"}, filter_name="read_only")
    assert routing.route == "deny"


def test_after_outside_content_changes_and_commands_are_judged():
    assert route(write("filepath"), {"filepath": "a.py"}, flows=[UNTRUSTED]).route == "judge"
    assert route(run("command"), {"command": "pytest -q"}, flows=[UNTRUSTED], filter_name="trusted").route == "allow"
    # Reading stays free: it changes nothing.
    assert route(read("filepath"), {"filepath": "a.py"}, flows=[UNTRUSTED]).route == "allow"


def test_after_a_secret_was_read_what_leaves_the_machine_is_judged():
    routing = route(egress(), {"url": "https://example.com"}, flows=[SECRET])
    assert routing.route == "judge"
    assert "after a secret was read" in routing.reasons
    assert route(write("filepath"), {"filepath": "a.py"}, flows=[SECRET]).route == "allow"


def test_outside_content_comes_from_untrusted_tools_and_uploads():
    assert route(egress(untrusted=True)).flows == (UNTRUSTED,)
    assert route(read("filepath"), {"filepath": "uploads/report.docx"}).flows == (UNTRUSTED,)
    assert route(read("filepath"), {"filepath": "docs/report.md"}).flows == ()


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git status --porcelain | grep -c '^??'",
        "ls web_chat 2>/dev/null; echo ---",
        "cd /workspace && git log -5 --oneline",
        "sed -n 105,130p core/system_access.py",
        "grep -rn 'AgentFactory' core | head -20",
        "find . -name '*.py' -newer setup.cfg",
        "git diff --stat 2>&1 | tail -30",
    ],
)
def test_read_only_commands(command):
    assert is_readonly(command, DEFAULT_READONLY_COMMANDS)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "echo junk > notes.txt",
        "cat a.txt >> b.txt",
        "find . -name '*.pyc' -delete",
        "find . -exec rm {} \\;",
        "sed -n -i 5p a.txt",
        "sed -ni 5p a.txt",
        "git diff --output=patch.txt",
        "ls $(rm -rf build)",
        "ls `whoami`",
        "rm -rf core",
        "git branch -D main",
        "ls 'unclosed",
        "",
    ],
)
def test_commands_that_may_change_something_are_not_read_only(command):
    assert not is_readonly(command, DEFAULT_READONLY_COMMANDS)


def test_operator_lists_shape_the_read_only_commands():
    commands = ("make lint", {"tool show": ("--write",)})
    assert is_readonly("make lint", commands)
    assert not is_readonly("make test", commands)
    assert is_readonly("tool show x", commands)
    assert not is_readonly("tool show --write=x", commands)


def test_command_words_that_may_name_files():
    assert command_paths("cat -n .env | grep KEY") == ["cat", ".env", "grep", "KEY"]


def test_path_globs_cross_directories_and_match_names_anywhere():
    assert matches("deploy/keys/server.pem", ["*.pem"])
    assert matches("policies/sub/a.yaml", ["policies/*"])
    assert matches("APP/.ENV", [".env"])
    assert not matches("environment.md", [".env"])


# -- the gate --------------------------------------------------------------
def gate_with(verdict="allow", **settings):
    validator = SimpleNamespace(evaluate=AsyncMock(return_value={"action": verdict}))
    gate = ActionGate(ActionPolicyConfig(mode="enforce", **settings), validator=validator)
    state = ActionRunState(task="Fix the failing test")
    ctx = SimpleNamespace(context=SimpleNamespace(action_state=state, factory=None))
    return gate, validator, state, ctx


async def ran(_ctx, args):
    return f"ran:{args}"


async def invoke(gate, ctx, effect, args, tool="tool", invoke=ran):
    return await gate.invoke(tool, "function", ctx, json.dumps(args), invoke, effect=effect)


async def test_a_declared_workspace_edit_runs_without_the_validator():
    gate, validator, state, ctx = gate_with("deny")
    assert (await invoke(gate, ctx, write("filepath"), {"filepath": "a.py"})).startswith("ran:")
    validator.evaluate.assert_not_awaited()
    check = next(e for e in state.events if e["rule"] == "policy_check")
    assert (check["decision"], check["source"], check["effect"]) == ("allow", "filter", "write")


async def test_the_filter_refuses_and_says_so():
    gate, validator, state, ctx = gate_with()
    state.filter = "read_only"
    blocked = json.loads(await invoke(gate, ctx, write("filepath"), {"filepath": "a.py"}))
    assert blocked["rule"] == "policy_filter"
    assert blocked["filter"] == "read_only"
    assert "Read only" in blocked["next_step"] and "switch" in blocked["next_step"]
    assert state.denials == 1
    validator.evaluate.assert_not_awaited()


async def test_switching_the_filter_applies_to_the_running_turn():
    gate, _, state, ctx = gate_with()
    child = state.delegate("orchestrate", "work")
    state.filter = "read_only"
    child_ctx = SimpleNamespace(context=SimpleNamespace(action_state=child, factory=None))
    assert json.loads(await invoke(gate, child_ctx, write("filepath"), {"filepath": "a.py"}))["rule"] == "policy_filter"
    state.filter = "balanced"
    assert (await invoke(gate, child_ctx, write("filepath"), {"filepath": "a.py"})).startswith("ran:")


async def test_operator_effects_cover_tools_that_declare_none():
    gate, validator, _, ctx = gate_with("deny", tool_effects={"codegraph_*": "read"})
    result = await gate.invoke("codegraph_explore", "mcp", ctx, "{}", ran)
    assert result.startswith("ran:")
    validator.evaluate.assert_not_awaited()


async def test_facts_are_collected_only_from_calls_that_ran():
    gate, _, state, ctx = gate_with()
    child = state.delegate("orchestrate", "research")
    child_ctx = SimpleNamespace(context=SimpleNamespace(action_state=child, factory=None))
    await invoke(gate, child_ctx, egress(untrusted=True), {"url": "https://example.com"}, tool="web_fetch")
    assert [flow["fact"] for flow in state.flows] == [UNTRUSTED]
    # The whole turn took it in: the caller's next change is judged.
    await invoke(gate, ctx, write("filepath"), {"filepath": "a.py"})
    check = [e for e in state.events if e["rule"] == "policy_check" and e["decision"] == "allow"][-1]
    assert check["source"] == "validator"
    assert "after outside content" in check["reasons"]


async def test_the_judged_packet_says_why_and_what_the_run_took_in():
    gate, validator, state, ctx = gate_with()
    state.flows.append({"fact": SECRET, "tool": "file_read", "call": 1})
    await invoke(gate, ctx, egress(), {"url": "https://example.com"})
    facts = validator.evaluate.call_args.args[0]["host_facts"]
    assert facts["effect"] == "egress"
    assert "after a secret was read" in facts["why_judged"]
    assert facts["run_took_in"] == [{"fact": SECRET, "tool": "file_read", "call": 1}]


# -- asking the user ---------------------------------------------------------
async def answered_by(gate, state, *, approve, remember=False, context_id=None):
    """Answer the first call that waits, as the chat would."""
    for _ in range(100):
        waiting = [review for review in gate.pending_reviews() if review["waiting"]]
        if waiting:
            return gate.resolve_review(
                waiting[0]["approval_id"], approve=approve, remember=remember, context_id=context_id
            )
        await asyncio.sleep(0.01)
    raise AssertionError("nothing waited for an answer")


async def test_a_held_call_waits_in_the_chat_and_runs_once_allowed():
    gate, _, state, ctx = gate_with()
    state.interactive, state.context_id = True, "ctx-1"
    events = []
    state.policy_event = events.append
    call = asyncio.create_task(invoke(gate, ctx, write("filepath"), {"filepath": ".env"}))

    assert await answered_by(gate, state, approve=True, context_id="ctx-1")
    assert (await asyncio.wait_for(call, 2)).startswith("ran:")
    waiting = next(event for event in events if event.get("awaiting"))
    assert waiting["decision"] == "review" and waiting["approval_id"]
    assert waiting["reasons"] == ["write: allow", "secret: .env"]
    answer = [event for event in events if event.get("source") == "user"]
    assert answer and answer[0]["decision"] == "allow"
    assert not gate.pending_reviews()


async def test_a_declined_call_does_not_run():
    gate, _, state, ctx = gate_with()
    state.interactive = True
    action = AsyncMock()
    call = asyncio.create_task(invoke(gate, ctx, write("filepath"), {"filepath": ".env"}, invoke=action))
    assert await answered_by(gate, state, approve=False)
    blocked = json.loads(await asyncio.wait_for(call, 2))
    assert blocked["rule"] == "declined"
    action.assert_not_awaited()


async def test_an_answer_counts_only_in_the_conversation_it_came_from():
    gate, _, state, ctx = gate_with()
    state.interactive, state.context_id = True, "ctx-1"
    call = asyncio.create_task(invoke(gate, ctx, write("filepath"), {"filepath": ".env"}))
    assert await answered_by(gate, state, approve=True, context_id="ctx-other") is False
    assert await answered_by(gate, state, approve=True, context_id="ctx-1")
    assert (await asyncio.wait_for(call, 2)).startswith("ran:")


async def test_allowed_for_the_turn_the_tool_asks_no_more():
    gate, validator, state, ctx = gate_with("review")
    state.interactive = True
    first = asyncio.create_task(invoke(gate, ctx, None, {"x": 1}, tool="deploy"))
    assert await answered_by(gate, state, approve=True, remember=True)
    assert (await asyncio.wait_for(first, 2)).startswith("ran:")
    assert (await invoke(gate, ctx, None, {"x": 2}, tool="deploy")).startswith("ran:")
    assert not gate.pending_reviews()


async def test_nobody_answering_in_time_refuses_without_spending_denials():
    gate, _, state, ctx = gate_with(review_ttl_seconds=10)
    gate.config = gate.config.model_copy(update={"review_ttl_seconds": 0.05})
    state.interactive = True
    blocked = json.loads(await invoke(gate, ctx, write("filepath"), {"filepath": ".env"}))
    assert blocked["rule"] == "review_timeout"
    assert state.denials == 0
    assert not gate.pending_reviews()


async def test_stopping_the_turn_refuses_what_waits():
    gate, _, state, ctx = gate_with()
    state.interactive = True
    call = asyncio.create_task(invoke(gate, ctx, write("filepath"), {"filepath": ".env"}))
    for _ in range(100):
        if gate.pending_reviews():
            break
        await asyncio.sleep(0.01)
    assert gate.cancel_reviews(state.run_id) == 1
    assert json.loads(await asyncio.wait_for(call, 2))["rule"] == "declined"


async def test_an_unavailable_validator_asks_the_user_instead_of_failing():
    gate, validator, state, ctx = gate_with()
    validator.evaluate = AsyncMock(side_effect=OSError("no provider"))
    state.interactive = True
    call = asyncio.create_task(invoke(gate, ctx, run("command"), {"command": "pytest -q"}))
    assert await answered_by(gate, state, approve=True)
    assert (await asyncio.wait_for(call, 2)).startswith("ran:")


async def test_without_anyone_watching_a_held_call_is_refused_with_an_approval_id():
    gate, _, state, ctx = gate_with()
    blocked = json.loads(await invoke(gate, ctx, write("filepath"), {"filepath": ".env"}))
    assert blocked["rule"] == "policy_review" and blocked["approval_id"]
    review = gate.pending_reviews()[0]
    assert review["waiting"] is False and review["reasons"] == ["write: allow", "secret: .env"]


# -- the factory and the space ---------------------------------------------
def test_a_turn_runs_under_its_conversations_filter_and_its_observer():
    from core.agent_factory import AgentFactory

    factory = object.__new__(AgentFactory)
    factory.action_gate = ActionGate(ActionPolicyConfig(mode="enforce"))
    metadata = {"ctx-1": {"policy_filter": "trusted"}}
    factory.context_manager = SimpleNamespace(get_context_metadata=lambda context_id: metadata.get(context_id, {}))
    observer = SimpleNamespace(handle_policy_event=lambda event: None, accepts_approvals=True)

    state = factory._action_state("task", context_id="ctx-1", observer=observer)

    assert (state.filter, state.context_id, state.interactive) == ("trusted", "ctx-1", True)
    plain = factory._action_state("task", context_id="ctx-2", observer=SimpleNamespace(handle_policy_event=print))
    assert (plain.filter, plain.interactive) == (None, False)


def test_switching_a_conversation_reaches_its_running_turn():
    from core.agent_factory import AgentFactory

    factory = object.__new__(AgentFactory)
    factory.action_gate = ActionGate(ActionPolicyConfig(mode="enforce"))
    stored = {}
    factory.context_manager = SimpleNamespace(update_context_metadata=lambda cid, data: stored.update({cid: data}))
    running = ActionRunState(task="task")
    factory._run_controls = {"ctx-1": SimpleNamespace(action_state=running.delegate("orchestrate", "x"))}

    assert factory.set_policy_filter("ctx-1", "read_only")
    assert running.filter == "read_only"
    assert stored == {"ctx-1": {"policy_filter": "read_only"}}
    assert not factory.set_policy_filter("ctx-1", "no-such-filter")


def test_the_shipped_policy_offers_the_switch():
    from core.config.config import Config

    policy = Config("routing.yaml").config.settings.action_policy
    assert list(policy.filters) == ["read_only", "strict", "balanced", "trusted"]
    assert policy.default_filter == "balanced" and policy.approvals == "user"
    assert matches("examples/coder/config.yaml", policy.protected_paths)
    assert policy.tool_effects["codegraph_*"] == "read"


def test_shared_and_example_tools_declare_their_effects():
    from core.config.config import Config
    from tools import tool_effect

    assert tool_effect("file_read").kind == "read"
    assert tool_effect("read_file").kind == "read"  # an alias resolves as the tool does
    assert tool_effect("beads_sync").kind == "external"
    assert tool_effect("orchestrate").kind == "delegate"
    loader = Config("examples/coder/config.yaml").project_tools_loader
    bash = tool_effect("bash_tool", loader)
    assert (bash.kind, bash.command) == ("exec", "command")
    assert tool_effect("web_fetch", loader).untrusted
