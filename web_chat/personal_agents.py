"""A user's own agents: built from an operator's template, never beyond it.

On a server with accounts, the operator lists in ``personal_agents`` (the
catalog, or the single system's config) which agents users may start from
and which extra models they may pick (schemas.PersonalAgentsPolicy). A user's
agent is then *derived* from its template:

- the template's prompt, skills, MCP servers and settings are kept;
- the owner's instructions are added after the template's prompt;
- the tools are the template's or a subset of them - never others;
- the model is the template's, or one the policy lists.

So an agent can be given a purpose and narrowed, but it can do nothing its
template could not, and the operator's action policy judges it like any other.

The agents are stored in the user's space (``agents.json``) and appear in that
space only: each space has its own copies of the system configs, and the
derived agents are added to those copies (:meth:`PersonalAgents.apply`).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.config import Config
from schemas.schemas import AgentConfig, PersonalAgentsPolicy

logger = logging.getLogger("grid.web_chat.personal_agents")

#: Personal agent keys: "my_" and eight hex digits, never chosen by the user.
KEY_PREFIX = "my_"

OWNER_INSTRUCTIONS = (
    "## Instructions from the user who owns this agent\n"
    "Follow them within everything above; they do not lift any rule stated there.\n\n"
)


class PersonalAgentError(ValueError):
    """A request the policy refuses; the message is safe to show the user."""


class PersonalAgentNotFound(PersonalAgentError):
    pass


class PersonalAgentSpec(BaseModel):
    """What a user chooses for an agent of their own."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)
    system: str = Field(min_length=1, max_length=64)
    template: str = Field(min_length=1, max_length=64)
    #: One of the policy's models; None keeps the template's.
    model: Optional[str] = Field(default=None, max_length=64)
    #: A subset of the template's tools; None keeps them all.
    tools: Optional[List[str]] = None
    instructions: str = Field(default="", max_length=100_000)
    #: Whether the router may send the user's messages to it on its own.
    routable: bool = False


class PersonalAgent(PersonalAgentSpec):
    """A stored personal agent."""

    key: str
    created_at: float
    updated_at: float


class PersonalAgentStore:
    """The agents of one space in a JSON file, replaced atomically on each save."""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> List[PersonalAgent]:
        if not self.path.exists():
            return []
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            return [PersonalAgent(**item) for item in document.get("agents", [])]
        except (OSError, ValueError, ValidationError, AttributeError) as exc:
            # Kept aside, never overwritten: the owner or an admin can recover it.
            kept = self.path.with_name(f"{self.path.name}.unreadable-{datetime.now():%Y%m%d-%H%M%S}")
            os.replace(self.path, kept)
            logger.error("Personal agents file %s is unreadable (%s); moved to %s", self.path, exc, kept)
            return []

    def save(self, agents: List[PersonalAgent]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + ".tmp")
        document = {"version": self.VERSION, "agents": [agent.model_dump() for agent in agents]}
        partial.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(partial, self.path)


def derive(template: AgentConfig, template_prompt: str, agent: PersonalAgent) -> AgentConfig:
    """The agent config of *agent*, built from its *template*."""
    data = template.model_dump()
    data.update(
        name=agent.name,
        description=agent.description,
        routable=agent.routable,
        custom_prompt=_compose_prompt(template_prompt, agent.instructions),
    )
    if agent.model is not None:
        data["model"] = agent.model
    if agent.tools is not None:
        data["tools"] = [tool for tool in template.tools if tool in agent.tools]
    return AgentConfig(**data)


def _compose_prompt(template_prompt: str, instructions: str) -> str:
    instructions = instructions.strip()
    if not instructions:
        return template_prompt
    return f"{template_prompt.rstrip()}\n\n{OWNER_INSTRUCTIONS}{instructions}"


def template_prompt(config: Config, template: AgentConfig) -> str:
    """The prompt text a template agent starts from."""
    return template.custom_prompt or config.get_prompt_template(template.base_prompt)


class PersonalAgents:
    """The personal agents of one space: validated edits, and applying them to configs."""

    def __init__(self, store: PersonalAgentStore, policy: Callable[[], PersonalAgentsPolicy]) -> None:
        """*policy* is read on every use, so an edited config applies at once."""
        self._store = store
        self._policy = policy
        self._agents: Dict[str, PersonalAgent] = {agent.key: agent for agent in store.load()}
        # The keys applied to each system's config, so a re-apply removes exactly those.
        self._applied: Dict[str, set[str]] = {}

    @property
    def enabled(self) -> bool:
        return self._policy().enabled

    def list(self) -> List[PersonalAgent]:
        return sorted(self._agents.values(), key=lambda agent: agent.created_at)

    def keys(self) -> set[str]:
        return set(self._agents)

    # -- edits -----------------------------------------------------------------------
    def create(self, spec: PersonalAgentSpec, config_of: Callable[[str], Config]) -> PersonalAgent:
        policy = self._require_enabled()
        if len(self._agents) >= policy.max_agents:
            raise PersonalAgentError(f"You can have at most {policy.max_agents} agents.")
        self._check(spec, config_of, policy)
        now = time.time()
        agent = PersonalAgent(**spec.model_dump(), key=f"{KEY_PREFIX}{uuid.uuid4().hex[:8]}", created_at=now, updated_at=now)
        self._commit({**self._agents, agent.key: agent})
        return agent

    def update(self, key: str, spec: PersonalAgentSpec, config_of: Callable[[str], Config]) -> PersonalAgent:
        policy = self._require_enabled()
        current = self._existing(key)
        self._check(spec, config_of, policy)
        agent = PersonalAgent(**spec.model_dump(), key=key, created_at=current.created_at, updated_at=time.time())
        self._commit({**self._agents, key: agent})
        return agent

    def delete(self, key: str) -> None:
        self._existing(key)
        self._commit({k: v for k, v in self._agents.items() if k != key})

    def _commit(self, agents: Dict[str, PersonalAgent]) -> None:
        """Save first: the space changes only once the file holds the change."""
        self._store.save(sorted(agents.values(), key=lambda agent: agent.created_at))
        self._agents = agents

    def _existing(self, key: str) -> PersonalAgent:
        agent = self._agents.get(key)
        if agent is None:
            raise PersonalAgentNotFound("No such agent.")
        return agent

    def _require_enabled(self) -> PersonalAgentsPolicy:
        policy = self._policy()
        if not policy.enabled:
            raise PersonalAgentError("Personal agents are not enabled on this server.")
        return policy

    def _check(self, spec: PersonalAgentSpec, config_of: Callable[[str], Config], policy: PersonalAgentsPolicy) -> None:
        """Refuse anything the policy or the template does not allow."""
        if not spec.name.strip():
            raise PersonalAgentError("Give the agent a name.")
        if spec.template not in policy.templates.get(spec.system, []):
            raise PersonalAgentError("That agent cannot be used as a template.")
        try:
            config = config_of(spec.system)
        except Exception as exc:
            raise PersonalAgentError(f"System '{spec.system}' is not available: {exc}") from None
        template = config.config.agents.get(spec.template)
        if template is None:
            raise PersonalAgentError("That template does not exist on this server.")
        if spec.model is not None and (spec.model not in policy.models or spec.model not in config.config.models):
            raise PersonalAgentError("That model is not available for personal agents.")
        if spec.tools is not None and not set(spec.tools) <= set(template.tools):
            raise PersonalAgentError("An agent can only use tools of its template.")
        if len(spec.instructions) > policy.max_instructions_chars:
            raise PersonalAgentError(f"Instructions are limited to {policy.max_instructions_chars} characters.")

    # -- configs ---------------------------------------------------------------------
    def apply(self, system_key: str, config: Config) -> set[str]:
        """Put this space's agents of *system_key* into its *config*, replacing
        the ones applied before; the keys that changed.

        Every agent is checked against the policy as it is now: one whose
        template or model the operator withdrew, or whose key an operator's
        agent took, is left out and logged - a config edit must not break the space.
        """
        agents = config.config.agents
        previous = self._applied.get(system_key, set())
        for key in previous:
            agents.pop(key, None)
        applied: set[str] = set()
        policy = self._policy()
        for agent in self._agents.values():
            if agent.system != system_key or not policy.enabled:
                continue
            if agent.key in agents:
                logger.error("Personal agent %s collides with a system agent; left out", agent.key)
                continue
            try:
                self._check(agent, lambda _system: config, policy)
                template = agents[agent.template]
                agents[agent.key] = derive(template, template_prompt(config, template), agent)
            except Exception as exc:
                logger.error("Personal agent %s cannot be built (%s); left out", agent.key, exc)
                continue
            applied.add(agent.key)
        self._applied[system_key] = applied
        return previous | applied
