"""The policy switch and held calls, from the chat socket to the user's space."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

from core.action_policy import ActionGate, ActionRunState
from schemas.action_policy import ActionPolicyConfig
from utils.tool_effects import write
from web_chat.session import ChatSession
from web_chat.space import UserSpace


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, event):
        self.sent.append(event)


def session_with(space):
    space.context_manager = lambda: SimpleNamespace()
    socket = Socket()
    return ChatSession(space, socket, "ctx-1"), socket


async def test_the_socket_moves_the_switch_for_its_conversation():
    space = SimpleNamespace(set_policy_filter=Mock(return_value=True))
    session, socket = session_with(space)
    await session._dispatch(json.dumps({"action": "policy_filter", "filter": "trusted"}))
    space.set_policy_filter.assert_called_once_with("ctx-1", "trusted")
    assert socket.sent == [{"type": "policy_filter", "filter": "trusted", "ok": True}]


async def test_the_socket_answers_a_held_call_of_its_conversation():
    space = SimpleNamespace(answer_action_review=Mock(return_value=False))
    session, socket = session_with(space)
    await session._dispatch(
        json.dumps({"action": "policy_review", "approval_id": "a1", "approve": True, "remember": True})
    )
    space.answer_action_review.assert_called_once_with("ctx-1", "a1", approve=True, remember=True)
    assert socket.sent == [{"type": "policy_review", "approval_id": "a1", "ok": False}]


def space_with(gate, policy):
    space = object.__new__(UserSpace)
    factory = SimpleNamespace(action_gate=gate, set_policy_filter=Mock())
    space.registry = SimpleNamespace(built_factories=lambda: {"coder": factory})
    space.deployment = SimpleNamespace(action_policy=policy)
    stored = {}
    space.conversations = SimpleNamespace(update_context_metadata=lambda cid, data: stored.update({cid: data}))
    return space, factory, stored


def test_the_space_offers_the_operators_filters_and_keeps_the_choice():
    policy = ActionPolicyConfig(mode="enforce")
    space, factory, stored = space_with(ActionGate(policy), policy)

    switch = space.policy_filters()
    assert switch["default"] == "balanced" and switch["approvals"] == "user"
    assert [item["key"] for item in switch["filters"]] == ["read_only", "strict", "balanced", "trusted"]

    assert space.set_policy_filter("ctx-1", "read_only")
    assert stored == {"ctx-1": {"policy_filter": "read_only"}}
    factory.set_policy_filter.assert_called_once_with("ctx-1", "read_only")
    assert not space.set_policy_filter("ctx-1", "no-such-filter")

    off, _, _ = space_with(None, None)
    assert off.policy_filters() is None and not off.set_policy_filter("ctx-1", "trusted")


async def held_call(gate, context_id):
    state = ActionRunState(task="task", interactive=True, context_id=context_id)
    ctx = SimpleNamespace(context=SimpleNamespace(action_state=state, factory=None))

    async def ran(_ctx, _args):
        return "ran"

    call = asyncio.create_task(
        gate.invoke("file_write", "function", ctx, json.dumps({"filepath": ".env"}), ran, effect=write("filepath"))
    )
    for _ in range(100):
        if gate.pending_reviews():
            return call, gate.pending_reviews()[0]["approval_id"]
        await asyncio.sleep(0.01)
    raise AssertionError("nothing was held")


async def test_a_user_answers_only_where_the_policy_lets_users_answer():
    policy = ActionPolicyConfig(mode="enforce")
    gate = ActionGate(policy)
    space, _, _ = space_with(gate, policy)
    call, approval_id = await held_call(gate, "ctx-1")
    assert not space.answer_action_review("ctx-other", approval_id, approve=True)
    assert space.answer_action_review("ctx-1", approval_id, approve=True)
    assert await asyncio.wait_for(call, 2) == "ran"

    operator = ActionPolicyConfig(mode="enforce", approvals="operator", review_ttl_seconds=10)
    gate = ActionGate(operator)
    space, _, _ = space_with(gate, operator)
    call, approval_id = await held_call(gate, "ctx-1")
    assert not space.answer_action_review("ctx-1", approval_id, approve=True)
    # The operator's review API still answers it.
    assert space.resolve_action_review(approval_id, approve=False)
    assert json.loads(await asyncio.wait_for(call, 2))["rule"] == "declined"
