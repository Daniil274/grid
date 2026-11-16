"""An agent taking a conversation back is told what other agents said meanwhile."""

from core.agent_factory import AgentFactory
from core.config import Config


def _say(factory, ctx, role, agent, text):
    factory.context_manager.append_message_to(ctx, role, text, metadata={"agent": agent})


def test_missed_exchange_lists_other_agents_messages_since_own_last_turn(config_file):
    factory = AgentFactory(Config(str(config_file)))
    ctx = factory.context_manager.start_new_context()
    _say(factory, ctx, "user", "coordinator", "add loading labels")
    _say(factory, ctx, "assistant", "coordinator", "labels started")
    _say(factory, ctx, "user", "engineer", "why does the right pane flicker?")
    _say(factory, ctx, "assistant", "engineer", "clear() on every selection; keep the old card")

    missed = factory._missed_exchange(ctx, "coordinator")

    assert "User (to engineer):\nwhy does the right pane flicker?" in missed
    assert "Agent engineer:\nclear() on every selection" in missed
    assert "add loading labels" not in missed
    # Nothing was missed by the agent that spoke last, nor by one new here.
    assert factory._missed_exchange(ctx, "engineer") == ""
    assert factory._missed_exchange(ctx, "newcomer") == ""


def test_missed_exchange_strips_legacy_assistant_footer(config_file):
    """A legacy answer's service Context ID footer never reaches the model."""
    factory = AgentFactory(Config(str(config_file)))
    ctx = factory.context_manager.start_new_context()
    _say(factory, ctx, "user", "coordinator", "add loading labels")
    _say(factory, ctx, "assistant", "coordinator", "labels started")
    _say(factory, ctx, "user", "engineer", "why does the right pane flicker?")
    _say(
        factory, ctx, "assistant", "engineer",
        "clear() on every selection; keep the old card\n\nContext ID: ctx-0a1b2c3d",
    )

    missed = factory._missed_exchange(ctx, "coordinator")

    assert "Agent engineer:\nclear() on every selection; keep the old card" in missed
    assert "Context ID: ctx-0a1b2c3d" not in missed
    # The user's words are never touched.
    assert "why does the right pane flicker?" in missed


async def test_a_mid_turn_message_is_attributed_to_the_agent_it_was_said_to(config_file):
    """A message the user adds to a running turn reaches the next agent.

    It is stored as the running agent's, so ``_missed_exchange`` - which reads
    only messages tagged with an agent - does not drop it for whoever takes
    the conversation over.
    """
    from core.interruption import RunControl
    from core.steering import SteerMessage

    factory = AgentFactory(Config(str(config_file)))
    ctx = factory.context_manager.start_new_context()
    _say(factory, ctx, "user", "engineer", "why does the right pane flicker?")
    _say(factory, ctx, "assistant", "engineer", "clear() on every selection; keep the old card")
    # The coordinator's turn is the one running in this conversation now.
    factory.context_manager.set_metadata(
        "last_invocation", {"agent": "coordinator", "timestamp": 0.0}, context_id=ctx,
    )
    control = RunControl()
    factory._run_controls[ctx] = control

    assert factory.steer(ctx, SteerMessage("m1", "also check the tests")) is True
    # What the next model call does: deliver the message into the conversation.
    await control.steering.apply([], None)

    last = factory.context_manager.get_context_messages(ctx, include_full=True)["messages"][-1]
    assert last["content"] == "also check the tests"
    assert last["metadata"]["agent"] == "coordinator"
    # The engineer taking over is told what the user asked the coordinator.
    missed = factory._missed_exchange(ctx, "engineer")
    assert "User (to coordinator):\nalso check the tests" in missed
