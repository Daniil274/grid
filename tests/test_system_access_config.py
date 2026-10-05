"""Key-agent configuration: default on, explicit opt-out per agent."""
from schemas import AgentConfig


def test_key_agent_is_the_default_and_per_agent():
    agent = AgentConfig(name="coordinator", model="m")
    assert agent.key_agent is True
    opt_out = AgentConfig(name="helper", model="m", key_agent=False)
    assert opt_out.key_agent is False


def test_key_agent_flag_roundtrips():
    agent = AgentConfig(name="coordinator", model="m", key_agent=False)
    assert AgentConfig.model_validate(agent.model_dump()) == agent
