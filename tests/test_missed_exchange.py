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
