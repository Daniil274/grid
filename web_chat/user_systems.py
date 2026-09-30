"""A user's own systems: several of their agents that hand work to each other.

A personal agent (web_chat.personal_agents) is one agent derived from an
operator's template. A user system is a group of them, built on one *base*
system the operator lists in ``personal_agents.templates``:

- every member is derived from a template agent of the base system, by the
  same rule as a personal agent: the template's tools or fewer, the template's
  model or one the policy lists, the owner's instructions after its prompt;
- a member may hand work to the members it names in ``delegates``, through an
  agent tool ``ask_<member>`` the system is given for that;
- the ``entry`` member takes the user's messages.

So a system can do nothing its templates could not; the delegation between its
members is the only thing it adds, and the operator's action policy judges
every call as usual.

A system lives in its owner's space (``systems.json``) and runs there only:
while ``active`` the router may send the owner's messages to it, otherwise it
runs when picked by hand. To offer it to everyone the owner *submits* it: a
snapshot goes to the admins (web_chat.system_hub), who can import it as a
created system (core.system_store) and publish it.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.config import Config
from schemas.schemas import AgentConfig, PersonalAgentsPolicy, ToolConfig
from web_chat.personal_agents import derive, template_prompt

logger = logging.getLogger("grid.web_chat.user_systems")

#: User system keys: "us_" and eight hex digits, never chosen by the user.
KEY_PREFIX = "us_"
#: A member's agent key in the built config; keeps it clear of the base system's agents.
MEMBER_PREFIX = "m_"
#: The agent tool a member hands work to another member with.
ASK_PREFIX = "ask_"
MEMBER_ID = r"^[a-z][a-z0-9_]{0,23}$"


class UserSystemError(ValueError):
    """A request the policy refuses; the message is safe to show the user."""


class UserSystemNotFound(UserSystemError):
    pass


class MemberSpec(BaseModel):
    """One agent of a user system."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=MEMBER_ID)
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)
    template: str = Field(min_length=1, max_length=64)
    model: Optional[str] = Field(default=None, max_length=64)
    tools: Optional[List[str]] = None
    instructions: str = Field(default="", max_length=100_000)
    #: The members this one may hand work to.
    delegates: List[str] = Field(default_factory=list)


class UserSystemSpec(BaseModel):
    """What a user chooses for a system of their own."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    #: What the router picks the system by, when it is active.
    description: str = Field(default="", max_length=1000)
    base: str = Field(min_length=1, max_length=64)
    entry: str = Field(pattern=MEMBER_ID)
    members: List[MemberSpec] = Field(min_length=1)


class Submission(BaseModel):
    """The owner's request to offer the system to everyone, and what became of it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    at: float
    state: Literal["pending", "imported", "declined"] = "pending"
    note: str = ""
    #: The created system it became, once imported.
    system_key: Optional[str] = None


class UserSystem(UserSystemSpec):
    """A stored user system."""

    key: str
    #: The router may send the owner's messages to it.
    active: bool = False
    created_at: float
    updated_at: float
    submission: Optional[Submission] = None


class UserSystemStore:
    """The systems of one space in a JSON file, replaced atomically on each save."""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> List[UserSystem]:
        if not self.path.exists():
            return []
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            return [UserSystem(**item) for item in document.get("systems", [])]
        except (OSError, ValueError, ValidationError, AttributeError) as exc:
            kept = self.path.with_name(f"{self.path.name}.unreadable-{datetime.now():%Y%m%d-%H%M%S}")
            os.replace(self.path, kept)
            logger.error("User systems file %s is unreadable (%s); moved to %s", self.path, exc, kept)
            return []

    def save(self, systems: List[UserSystem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + ".tmp")
        document = {"version": self.VERSION, "systems": [system.model_dump() for system in systems]}
        partial.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(partial, self.path)


def member_key(member_id: str) -> str:
    return f"{MEMBER_PREFIX}{member_id}"


def ask_tool(member_id: str) -> str:
    return f"{ASK_PREFIX}{member_id}"


def check_spec(spec: UserSystemSpec, config: Config, policy: PersonalAgentsPolicy) -> None:
    """Refuse anything the policy or the templates do not allow.

    *config* is the base system's config as the operator wrote it.
    """
    if not spec.name.strip():
        raise UserSystemError("Give the system a name.")
    allowed = policy.templates.get(spec.base, [])
    if not allowed:
        raise UserSystemError(f"System '{spec.base}' cannot be used as a base.")
    if len(spec.members) > policy.max_system_members:
        raise UserSystemError(f"A system can have at most {policy.max_system_members} agents.")
    ids = [member.id for member in spec.members]
    if len(set(ids)) != len(ids):
        raise UserSystemError("Two agents of the system have the same id.")
    if spec.entry not in ids:
        raise UserSystemError("The entry agent must be one of the system's agents.")
    agents, models, tools = config.config.agents, config.config.models, config.config.tools
    for member in spec.members:
        label = f"Agent '{member.id}'"
        if not member.name.strip():
            raise UserSystemError(f"{label} needs a name.")
        if member.template not in allowed:
            raise UserSystemError(f"{label}: '{member.template}' cannot be used as a template.")
        template = agents.get(member.template)
        if template is None:
            raise UserSystemError(f"{label}: template '{member.template}' does not exist on this server.")
        if member.model is not None and (member.model not in policy.models or member.model not in models):
            raise UserSystemError(f"{label}: that model is not available.")
        if member.tools is not None and not set(member.tools) <= set(template.tools):
            raise UserSystemError(f"{label} can only use tools of its template.")
        if len(member.instructions) > policy.max_instructions_chars:
            raise UserSystemError(f"{label}: instructions are limited to {policy.max_instructions_chars} characters.")
        unknown = set(member.delegates) - set(ids)
        if unknown:
            raise UserSystemError(f"{label} hands work to agents the system does not have: {', '.join(sorted(unknown))}.")
        if member.id in member.delegates:
            raise UserSystemError(f"{label} cannot hand work to itself.")
        if member_key(member.id) in agents or ask_tool(member.id) in tools:
            raise UserSystemError(f"{label}: the id clashes with the base system; choose another.")


def build_agents(spec: UserSystemSpec, config: Config) -> tuple[Dict[str, AgentConfig], Dict[str, ToolConfig]]:
    """The members' agents and the ask tools, derived from the base *config*."""
    agents = config.config.agents
    members = {member.id: member for member in spec.members}
    built: Dict[str, AgentConfig] = {}
    for member in spec.members:
        template = agents[member.template]
        entry = member.id == spec.entry
        derived = derive(
            template,
            template_prompt(config, template),
            SimpleNamespace(
                name=member.name,
                # The entry is routable: the router reads its description, so it
                # never goes without one.
                description=member.description or (spec.description or member.name if entry else ""),
                routable=entry,
                instructions=member.instructions,
                model=member.model,
                tools=member.tools,
            ),
        )
        derived.tools = [*derived.tools, *(ask_tool(other) for other in member.delegates)]
        built[member_key(member.id)] = derived
    tools = {
        ask_tool(member_id): ToolConfig(
            type="agent",
            name=ask_tool(member_id),
            target_agent=member_key(member_id),
            description=f"Hand a task to {member.name}" + (f": {member.description}" if member.description else "."),
            context_strategy="minimal",
        )
        for member_id, member in members.items()
        if any(member_id in other.delegates for other in spec.members)
    }
    return built, tools


def materialize(spec: UserSystemSpec, config: Config) -> Config:
    """*config* - a fresh load of the base system - turned into the user system.

    The base system's own agents stay, none of them routable: its agent tools
    (a template's sub-agents) still need their targets. The entry member is the
    only routable agent and the default one.
    """
    agents, tools = build_agents(spec, config)
    for agent in config.config.agents.values():
        agent.routable = False
    config.config.agents.update(agents)
    config.config.tools.update(tools)
    config.config.settings.default_agent = member_key(spec.entry)
    return config


class UserSystems:
    """The systems of one space: validated edits and what the registry runs."""

    def __init__(
        self,
        store: UserSystemStore,
        policy: Callable[[], PersonalAgentsPolicy],
        base_config: Callable[[str], Config],
    ) -> None:
        """*base_config(key)* loads a fresh copy of a catalog system's config."""
        self._store = store
        self._policy = policy
        self._base_config = base_config
        self._systems: Dict[str, UserSystem] = {system.key: system for system in store.load()}

    @property
    def enabled(self) -> bool:
        policy = self._policy()
        return policy.enabled and policy.max_systems > 0

    def list(self) -> List[UserSystem]:
        return sorted(self._systems.values(), key=lambda system: system.created_at)

    def get(self, key: str) -> UserSystem:
        system = self._systems.get(key)
        if system is None:
            raise UserSystemNotFound("No such system.")
        return system

    # -- edits ---------------------------------------------------------------------
    def create(self, spec: UserSystemSpec) -> UserSystem:
        policy = self._require_enabled()
        if len(self._systems) >= policy.max_systems:
            raise UserSystemError(f"You can have at most {policy.max_systems} systems.")
        self._check(spec, policy)
        now = time.time()
        system = UserSystem(
            **spec.model_dump(), key=f"{KEY_PREFIX}{uuid.uuid4().hex[:8]}", created_at=now, updated_at=now
        )
        self._commit({**self._systems, system.key: system})
        return system

    def update(self, key: str, spec: UserSystemSpec) -> UserSystem:
        policy = self._require_enabled()
        current = self.get(key)
        self._check(spec, policy)
        system = current.model_copy(update={**spec.model_dump(), "updated_at": time.time()})
        self._commit({**self._systems, key: UserSystem(**system.model_dump())})
        return system

    def set_active(self, key: str, active: bool) -> UserSystem:
        self._require_enabled()
        system = self.get(key).model_copy(update={"active": active, "updated_at": time.time()})
        self._commit({**self._systems, key: system})
        return system

    def record_submission(self, key: str, submission: Optional[Submission]) -> UserSystem:
        system = self.get(key).model_copy(update={"submission": submission, "updated_at": time.time()})
        self._commit({**self._systems, key: system})
        return system

    def delete(self, key: str) -> None:
        self.get(key)
        self._commit({k: v for k, v in self._systems.items() if k != key})

    def _commit(self, systems: Dict[str, UserSystem]) -> None:
        self._store.save(sorted(systems.values(), key=lambda system: system.created_at))
        self._systems = systems

    def _require_enabled(self) -> PersonalAgentsPolicy:
        policy = self._policy()
        if not policy.enabled or policy.max_systems <= 0:
            raise UserSystemError("Systems of your own are not enabled on this server.")
        return policy

    def _check(self, spec: UserSystemSpec, policy: PersonalAgentsPolicy) -> None:
        try:
            config = self._base_config(spec.base)
        except UserSystemError:
            raise
        except Exception as exc:
            raise UserSystemError(f"System '{spec.base}' is not available: {exc}") from None
        check_spec(spec, config, policy)

    # -- running -------------------------------------------------------------------
    def build(self, key: str) -> Config:
        """The system's config, checked against the policy as it is now."""
        system = self.get(key)
        policy = self._require_enabled()
        config = self._base_config(system.base)
        check_spec(system, config, policy)
        return materialize(system, config)

    def config_path(self, key: str) -> Path:
        """Where the system's base config lives; the user system has no file of its own."""
        return Path(self._base_config(self.get(key).base).config_path)


def spec_of(system: UserSystem) -> UserSystemSpec:
    return UserSystemSpec(**{field: getattr(system, field) for field in UserSystemSpec.model_fields})


def as_dict(system: UserSystem) -> Dict[str, Any]:
    return system.model_dump()
