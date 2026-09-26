"""One user's space: conversations, agent sessions, workspace and running turns.

The systems and their configs belong to the server (web_chat.deployment); a
space is what one user owns on top of them:

- the conversations (a ``ContextManager`` and its file),
- the agents' SDK sessions - everything each agent did, tool calls included,
- the workspace the agents work in, and the container when isolation is on,
- a factory per system, all sharing the space's conversations, so routing a
  follow-up to another system keeps the conversation intact,
- the turns running in the space (web_chat.turns),
- with a layout of its own, the user's personal agents (web_chat.personal_agents).

Where the state lives is the space's :class:`SpaceLayout`. A server for one
person keeps the layout it always had: conversations and sessions in the base
system's logs directory, the workspace from the config or ``--path``. A server
for many users gives every user a directory of their own
(:meth:`SpaceLayout.under`), outside the workspace, so the agents' tools never
reach the records of their own conversations.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

from core.agent_factory import AgentFactory
from core.config import Config
from core.context import ContextManager
from core.managers.container_manager import ContainerManager
from web_chat.deployment import Deployment, isolation_enabled
from web_chat.personal_agents import PersonalAgentError, PersonalAgents, PersonalAgentStore
from web_chat.systems import Resolution, SystemRegistry
from web_chat.turns import TurnBoard

logger = logging.getLogger("grid.web_chat.space")

T = TypeVar("T")


@dataclass(frozen=True)
class SpaceLayout:
    """Where a space keeps its state."""

    #: The ContextManager file of the space's conversations.
    conversations: Path
    #: The SQLite file of the agents' SDK sessions.
    agent_sessions: Path
    #: The directory the agents work in.
    workspace: Path
    #: The user's personal agents.
    personal_agents: Path

    @classmethod
    def under(cls, root: Path) -> "SpaceLayout":
        """A space's own directory: its records beside, not inside, its workspace."""
        return cls(
            conversations=root / "conversations.json",
            agent_sessions=root / "agent_sessions.db",
            workspace=root / "workspace",
            personal_agents=root / "agents.json",
        )


class IsolationUnavailable(RuntimeError):
    """A space that must isolate its agents cannot start its container."""


class UserSpace:
    """Everything one user owns on a web chat server."""

    def __init__(
        self,
        deployment: Deployment,
        *,
        user_id: str = "default_user",
        layout: Optional[SpaceLayout] = None,
        require_isolation: bool = False,
    ) -> None:
        """``layout`` None keeps the single-user layout (see the module docs).

        ``require_isolation``: the agents' tools must run in the space's
        container. A space whose container cannot start is then not built at
        all, rather than letting its agents work on the host.
        """
        self.deployment = deployment
        self.user_id = user_id
        self.layout = layout
        self.require_isolation = require_isolation
        self.turns = TurnBoard()

        self._lock = asyncio.Lock()
        self._prepared: set[tuple[str, str]] = set()
        self._background: set[asyncio.Task] = set()
        self._warmup: Optional[asyncio.Task] = None

        self.config: Config
        self.container_id: Optional[str]
        self.workspace_path: Path
        self.conversations_path: Path
        self.conversations: ContextManager
        self.personal_agents: Optional[PersonalAgents]
        self.registry: SystemRegistry

        self._build()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        config, workspace = self._workspace_config()
        container_id = self._start_container(config, workspace) if isolation_enabled(config) else None
        if self.require_isolation and container_id is None:
            raise IsolationUnavailable(
                f"The workspace container for user {self.user_id} could not be started, and this "
                "server runs agents only in containers. Check isolation settings and Docker."
            )

        self.config = config
        self.container_id = container_id
        self.workspace_path = Path(config.get_working_directory()).resolve()
        self.workspace_path.mkdir(parents=True, exist_ok=True)

        if self.layout is not None:
            self.conversations_path = self.layout.conversations
        else:
            logs_dir = Path(config.get_absolute_path("logs"))
            logs_dir.mkdir(parents=True, exist_ok=True)
            self.conversations_path = logs_dir / "context.json"
        self.conversations_path.parent.mkdir(parents=True, exist_ok=True)
        self.conversations = ContextManager(
            max_history=config.get_max_history(),
            persist_path=str(self.conversations_path),
        )

        self.personal_agents = (
            PersonalAgents(
                PersonalAgentStore(self.layout.personal_agents),
                lambda: self.deployment.personal_agents_policy,
            )
            if self.layout is not None
            else None
        )
        self.registry = SystemRegistry(
            base_config=config,
            catalog=self.deployment.catalog,
            build_factory=self._build_factory,
            working_directory=str(self.workspace_path) if self.layout else self.deployment.working_directory,
            customize=self.personal_agents.apply if self.personal_agents is not None else None,
        )
        self._prepared.clear()

        logger.info(
            "Space ready: user=%s catalog=%s base=%s workdir=%s systems=%s",
            self.user_id,
            self.deployment.routing_path if self.deployment.catalog else "none",
            self.deployment.config_path,
            self.workspace_path,
            ", ".join(self.registry.keys()),
        )

    def _workspace_config(self) -> tuple[Config, Optional[Path]]:
        """The base config bound to this space's workspace, and that workspace.

        The workspace is None when the config decides it (single user without
        isolation or ``--path``).
        """
        config_path = str(self.deployment.config_path)
        if self.layout is not None:
            workspace = self.layout.workspace.resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            return Config(config_path, str(workspace)), workspace
        config = Config(config_path, self.deployment.working_directory)
        if not isolation_enabled(config):
            return config, None
        workspace = self._single_user_isolated_workspace()
        return Config(config_path, str(workspace)), workspace

    def _single_user_isolated_workspace(self) -> Path:
        if self.deployment.working_directory is not None:
            workspace = Path(self.deployment.working_directory).expanduser().resolve()
        else:
            workspace = (self.deployment.config_path.parent / "workspace" / f"user_{self.user_id}").resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _start_container(self, config: Config, workspace: Optional[Path]) -> Optional[str]:
        manager = ContainerManager(config)
        if not manager.enabled:
            return None
        try:
            container = manager.get_or_create_container(self.user_id, workspace=workspace)
        except Exception as exc:
            logger.warning("Failed to initialize container isolation for %s: %s", self.user_id, exc)
            return None
        if container:
            logger.info("Container ready for %s: %s", self.user_id, container.name)
            return container.id
        return None

    def _build_factory(self, config: Config) -> AgentFactory:
        """One factory per system; history is shared so conversations survive routing."""
        return AgentFactory(
            config=config,
            working_directory=config.get_working_directory(),
            context_manager=self.conversations,
            container_id=self.container_id,
            policy_config=self.deployment.policy_config,
            session_db_path=str(self.layout.agent_sessions) if self.layout else None,
        )

    @property
    def workspace_label(self) -> str:
        """The workspace as shown to the user: its path on a one-user server;
        on a shared one, where it lives on the server is not the user's business."""
        return str(self.workspace_path) if self.layout is None else "your workspace"

    # -- systems -----------------------------------------------------------
    @property
    def factory(self) -> AgentFactory:
        """Factory of the default system - the entry point for warm-up and voice."""
        return self.registry.factory(self.registry.default_key())

    async def resolve_turn(
        self,
        message: str,
        *,
        system_key: Optional[str] = None,
        agent_key: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> Resolution:
        """Pick the system and agent for *message*; ``None`` means route it."""
        previous = None
        if context_id:
            metadata = self.conversation_metadata(context_id)
            if metadata.get("routed_system") and metadata.get("routed_agent"):
                previous = (metadata["routed_system"], metadata["routed_agent"])
        return await self.registry.resolve(
            message, system_key=system_key, agent_key=agent_key, previous=previous
        )

    async def warm_agent(self, agent_key: str, system_key: Optional[str] = None) -> None:
        """Build an agent ahead of the first message that needs it."""
        system = system_key or self.registry.default_key()
        async with self._lock:
            if (system, agent_key) in self._prepared:
                return
            await self.registry.factory(system).create_agent(agent_key)
            self._prepared.add((system, agent_key))
            logger.info("Prepared agent for %s: %s/%s", self.user_id, system, agent_key)

    async def warm_default_agent(self) -> None:
        system = self.registry.default_key()
        await self.warm_agent(self.registry.config(system).get_default_agent(), system)

    def schedule_warmup(self) -> None:
        """Warm the default agent in the background; a newer request replaces an older one."""
        if self._warmup is not None:
            self._warmup.cancel()
        self._warmup = self._spawn(self.warm_default_agent(), "warm-default-agent")

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        """Run *coro* in the background, owned by the space until it finishes."""
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "Background task %s failed", task.get_name(), exc_info=task.exception()
            )

    # -- personal agents -----------------------------------------------------
    def change_personal_agents(self, edit: Callable[[PersonalAgents], T]) -> T:
        """Apply *edit* to the personal agents, then bring configs and factories
        up to date: the next turn uses the agents as they are now."""
        if self.personal_agents is None:
            raise PersonalAgentError("Personal agents need a server with accounts.")
        result = edit(self.personal_agents)
        factories = self.registry.built_factories()
        for system, keys in self.registry.recustomize().items():
            for key in keys:
                if system in factories:
                    factories[system].forget_agent(key)
                self._prepared.discard((system, key))
        return result

    # -- action reviews ----------------------------------------------------
    def pending_action_reviews(self) -> list[dict[str, Any]]:
        """Pending reviews from the factories that have handled a turn."""
        reviews: list[dict[str, Any]] = []
        for system_key, factory in self.registry.built_factories().items():
            gate = getattr(factory, "action_gate", None)
            if gate is None:
                continue
            reviews.extend(
                {**review, "system_key": system_key}
                for review in gate.pending_reviews()
            )
        return sorted(reviews, key=lambda item: item["created_at"])

    def resolve_action_review(self, approval_id: str, *, approve: bool) -> bool:
        """Resolve an action review without exposing this capability to agents."""
        for factory in self.registry.built_factories().values():
            gate = getattr(factory, "action_gate", None)
            if gate is not None and gate.resolve_review(approval_id, approve=approve):
                return True
        return False

    # -- lifecycle -----------------------------------------------------------
    @property
    def idle(self) -> bool:
        """No turn claimed or running and no background work of any kind."""
        return self.turns.idle and not self._background

    async def close(self) -> None:
        """Stop everything the space runs. A space is never rebuilt in place:
        its pool replaces it with a new one (web_chat.spaces)."""
        await self.turns.close()
        pending = list(self._background)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.registry.close()

    # -- conversation metadata --------------------------------------------
    def context_manager(self) -> ContextManager:
        return self.conversations

    def conversation_metadata(self, context_id: str) -> Dict[str, Any]:
        return self.conversations.get_context_metadata(context_id)

    def update_conversation_metadata(self, context_id: str, **updates: Any) -> None:
        if self.conversations.get_context_metadata(context_id).get("title_locked"):
            updates.pop("title", None)  # the user renamed it; keep their title
        self.conversations.update_context_metadata(
            context_id, {k: v for k, v in updates.items() if v is not None}
        )
