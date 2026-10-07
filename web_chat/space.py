"""One user's space: conversations, agent sessions, workspace and running turns.

The systems and their configs belong to the server (web_chat.deployment); a
space is what one user owns on top of them:

- the conversations (a ``ContextManager`` and its file),
- the agents' SDK sessions - everything each agent did, tool calls included,
- the workspace the agents work in, and the container when isolation is on,
- a factory per system, all sharing the space's conversations, so routing a
  follow-up to another system keeps the conversation intact,
- the turns running in the space (web_chat.turns),
- with a layout of its own, the user's personal agents (web_chat.personal_agents)
  and the systems built from them (web_chat.user_systems),
- for an admin, the created systems still in draft, to test them by hand
  (core.system_store),
- on a one-user server, the working directories chosen for single chats
  (:meth:`UserSpace.choose_workspace`): each has its own configs, factories
  and container, so chats working in different directories never share one.

Where the state lives is the space's :class:`SpaceLayout`. A server for one
person keeps the layout it always had: conversations and sessions in the base
system's logs directory, the workspace from the config or ``--path``. A server
for many users gives every user a directory of their own
(:meth:`SpaceLayout.under`), outside the workspace, so the agents' tools never
reach the records of their own conversations.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from core.agent_factory import AgentFactory
from core.config import Config
from core.config.prompt_sections import PromptSection
from core.context import ContextManager
from core.factory.policy import POLICY_FILTER_KEY
from core.managers.container_manager import ContainerManager
from core.model_access import ModelAccess
from core.system_store import BuilderAccess, SystemStore
from web_chat.deployment import Deployment, isolation_enabled
from web_chat.entitlements import Entitlement, OwnCredentials, UserCredentials
from web_chat.limits import TurnCounter, TurnLimits
from web_chat.personal_agents import PersonalAgentError, PersonalAgents, PersonalAgentStore
from web_chat.spend import SpaceMeter
from web_chat.system_activity import SystemActivity
from web_chat.systems import ExtraSystem, Resolution, SystemRegistry
from web_chat.user_systems import UserSystemError, UserSystems, UserSystemStore
from web_chat.turns import TurnBoard

logger = logging.getLogger("grid.web_chat.space")

T = TypeVar("T")

#: The conversation metadata naming the working directory chosen for that chat.
WORKSPACE_KEY = "workspace"


# Taught to every agent in every system of a space: the chat renders a
# markdown link to /api/workspace/files/<path> as a download of that
# workspace file, so files an agent produces can be handed over in its
# reply the way the user expects (see web_chat/uploads.py download_url).
FILE_LINKS_SECTION = PromptSection(
    key="workspace_file_links",
    scope="static",
    content="""## Handing files to the user

When you create or update a file the user should receive, link it in your
reply so the chat shows it as a download:

[Report.pdf](/api/workspace/files/report.pdf)

The link target is `/api/workspace/files/` followed by the file's path
relative to the workspace root - the same path file tools report. Keep the
link text human-readable and link each file once. Only link files that
exist in the workspace; never use this prefix for anything else.

To show a picture in the reply rather than linking it, use markdown image
syntax with the same path and add `?inline=1`, which tells the chat to render
the file instead of downloading it:

![bench.png](/api/workspace/files/bench.png?inline=1)

Only PNG, JPEG, GIF and WebP pictures render this way; every other file is a
download link. Write the path in full - a bare file name shows as plain text.""",
)


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
    #: The session logs of the user's turns.
    logs: Path
    #: The user's own systems.
    user_systems: Path

    @classmethod
    def under(cls, root: Path) -> "SpaceLayout":
        """A space's own directory: its records beside, not inside, its workspace."""
        return cls(
            conversations=root / "conversations.json",
            agent_sessions=root / "agent_sessions.db",
            workspace=root / "workspace",
            personal_agents=root / "agents.json",
            logs=root / "logs",
            user_systems=root / "systems.json",
        )


def single_user_records(config: Config) -> tuple[Path, Path]:
    """Where a one-user server keeps its conversations and agent sessions:
    the config's ``logs/context.json`` and the logs directory's
    ``agent_sessions.db`` (AgentFactory's default)."""
    return (
        Path(config.get_absolute_path("logs")) / "context.json",
        Path(config.get_logs_directory()) / "agent_sessions.db",
    )


class IsolationUnavailable(RuntimeError):
    """A space that must isolate its agents cannot start its container."""


class WorkspaceChoiceError(ValueError):
    """A chat cannot work in the directory asked for."""


@dataclass
class ChosenWorkspace:
    """A working directory chosen for some chats, and what runs there: the
    space's configs bound to it, its own container and its own factories."""

    path: Path
    config: Config
    container_id: Optional[str]
    manager: Optional[ContainerManager]
    registry: "SystemRegistry" = field(init=False)


class UserSpace:
    """Everything one user owns on a web chat server."""

    def __init__(
        self,
        deployment: Deployment,
        *,
        user_id: str = "default_user",
        layout: Optional[SpaceLayout] = None,
        require_isolation: bool = False,
        turn_counter: Optional[TurnCounter] = None,
        activity: Optional[SystemActivity] = None,
        usage: Optional[Any] = None,
        admin: bool = False,
        on_systems_changed: Optional[Callable[[], None]] = None,
        entitlement: Optional[Callable[[], Entitlement]] = None,
        own_credentials: Optional[OwnCredentials] = None,
    ) -> None:
        """``layout`` None keeps the single-user layout (see the module docs).

        ``require_isolation``: the agents' tools must run in the space's
        container, whatever the config's ``isolation.enabled`` says (a server
        with accounts). A space whose container cannot start is then not built
        at all, rather than letting its agents work on the host.

        ``turn_counter`` counts the user's turns per day; given one, the
        deployment's ``user_limits`` apply to the space (web_chat.limits).

        ``entitlement`` returns what the user's plan allows now (web_chat.entitlements):
        the key that pays for their calls, the models they may use, their limits.
        Without it the user is the operator: the providers' own keys, every model,
        the config's ``user_limits``. ``own_credentials`` holds the keys and tokens
        the user stored for themselves.

        ``activity`` counts the turns per system (web_chat.system_activity).
        ``admin``: the space of an admin, or of the one user: it offers the
        created systems in draft, to be picked by hand, the catalog's
        ``admins_only`` systems, and access to the shared builder store.
        Other users receive access to their private builder store.
        ``on_systems_changed`` is called for shared changes so every space picks them up.
        """
        self.deployment = deployment
        self.user_id = user_id
        self.layout = layout
        self.require_isolation = require_isolation
        self.turns = TurnBoard()
        self.activity = activity
        self.usage = usage
        self.admin = admin
        self.on_systems_changed = on_systems_changed
        self.entitlement = entitlement
        policy = (lambda: entitlement().limits) if entitlement is not None else (lambda: deployment.user_limits)
        spend = usage if hasattr(usage, "spent_micro") else None
        self.limits = (
            TurnLimits(user_id, policy, turn_counter, spend=spend)
            if turn_counter is not None or spend is not None
            else None
        )
        #: Credentials in and spend out of every model call this space's agents make.
        self.model_access = self._model_access(entitlement, own_credentials)
        #: The accounts service this space counts its user's use through.
        self.turn_counter = turn_counter

        self._lock = asyncio.Lock()
        # (workspace, system, agent) of the agents built ahead of a turn.
        self._prepared: set[tuple[Path, str, str]] = set()
        # The directories chosen for single chats, built on their first turn.
        self._chosen: Dict[Path, ChosenWorkspace] = {}
        self._choosing = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        self._warmup: Optional[asyncio.Task] = None

        self.config: Config
        self.container_id: Optional[str]
        self.workspace_path: Path
        self.conversations_path: Path
        self.conversations: ContextManager
        self.personal_agents: Optional[PersonalAgents]
        self.user_systems: Optional[UserSystems]
        self.registry: SystemRegistry

        self._build()

    # -- construction ------------------------------------------------------
    def _model_access(
        self, entitlement: Optional[Callable[[], Entitlement]], own: Optional[OwnCredentials]
    ) -> ModelAccess:
        credentials = (
            UserCredentials(self.user_id, entitlement, self.deployment.plans, own) if entitlement is not None else None
        )
        return ModelAccess(
            credentials,
            on_spend=SpaceMeter(self.user_id, self.usage),
            prices=self.deployment.price_book(),
            unknown_price=self.deployment.unknown_price(),
            permits=(lambda key, name: entitlement().permits_model(key, name)) if entitlement is not None else None,
            signature=(lambda: entitlement().signature()) if entitlement is not None else None,
        )

    def _build(self) -> None:
        config = self._workspace_config()
        workspace = Path(config.get_working_directory()).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        isolated = self.require_isolation or isolation_enabled(config)
        container_id = self._start_container(config, workspace) if isolated else None
        if self.require_isolation and container_id is None:
            raise IsolationUnavailable(
                f"The workspace container for user {self.user_id} could not be started, and this "
                "server runs agents only in containers. Check isolation settings and Docker."
            )

        self.config = config
        self.container_id = container_id
        self.workspace_path = workspace

        if self.layout is not None:
            self.conversations_path = self.layout.conversations
        else:
            self.conversations_path, _ = single_user_records(config)
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
        self.user_systems = (
            UserSystems(
                UserSystemStore(self.layout.user_systems),
                lambda: self.deployment.personal_agents_policy,
                self.fresh_config,
            )
            if self.layout is not None
            else None
        )
        self.built_systems = (
            SystemStore(self.layout.user_systems.parent / "built_systems")
            if self.layout is not None and not self.admin else None
        )
        self.registry = self._registry(config, self._configs_workdir, container_id)
        self._prepared.clear()

        logger.info(
            "Space ready: user=%s catalog=%s base=%s workdir=%s systems=%s",
            self.user_id,
            self.deployment.routing_path if self.deployment.catalog else "none",
            self.deployment.config_path,
            self.workspace_path,
            ", ".join(self.registry.keys()),
        )

    def _registry(self, config: Config, workdir: Optional[str], container_id: Optional[str]) -> SystemRegistry:
        """The systems of the space, loaded with *workdir*, their tools in *container_id*."""
        build = functools.partial(self._build_factory, container_id=container_id)
        return SystemRegistry(
            base_config=config,
            catalog=self.deployment.catalog,
            build_factory=build,
            build_factory_for_key=build,
            working_directory=workdir,
            customize=self.personal_agents.apply if self.personal_agents is not None else None,
            confined=self.require_isolation,
            extras=functools.partial(self._extra_systems, workdir),
            admin=self.admin,
        )

    @property
    def _configs_workdir(self) -> Optional[str]:
        """The working directory the space's system configs are loaded with."""
        return str(self.workspace_path) if self.layout else self.deployment.working_directory

    def fresh_config(self, system_key: str) -> Config:
        """A new load of a system's config as its file says, for this space."""
        return Config(str(self.deployment.system_config_path(system_key)), self._configs_workdir)

    def _extra_systems(self, workdir: Optional[str] = None) -> List[ExtraSystem]:
        """The systems of this space beside the catalog's (SystemRegistry.refresh_extras),
        loaded with *workdir* - the space's own when None."""
        workdir = workdir if workdir is not None else self._configs_workdir
        extras: List[ExtraSystem] = []
        store = self.deployment.created_store
        if self.admin and store is not None:
            for manifest in store.list():
                if manifest.status != "draft":
                    continue
                path = store.config_path(manifest.key)
                extras.append(
                    ExtraSystem(
                        key=manifest.key,
                        name=manifest.name,
                        description=manifest.description,
                        config_path=path,
                        load=lambda path=path: Config(str(path), workdir),
                        routable=False,
                        badge="draft",
                        requires=tuple(manifest.requires),
                    )
                )
        if self.user_systems is not None and self.user_systems.enabled:
            for system in self.user_systems.list():
                try:
                    path = self.deployment.system_config_path(system.base)
                except KeyError:
                    logger.error("User system %s is built on '%s', which is gone; left out", system.key, system.base)
                    continue
                extras.append(
                    ExtraSystem(
                        key=system.key,
                        name=system.name,
                        description=system.description,
                        config_path=path,
                        load=lambda key=system.key: self.user_systems.build(key),
                        routable=system.active,
                        badge="mine",
                    )
                )
        if self.built_systems is not None:
            access = self._builder_access()
            for manifest in self.built_systems.list():
                if manifest.status == "archived":
                    continue
                extras.append(ExtraSystem(
                    key=access.system_key(manifest.key),
                    name=manifest.name,
                    description=manifest.description,
                    config_path=self.built_systems.config_path(manifest.key),
                    load=lambda key=manifest.key, access=access: access.load(key, str(self.workspace_path)),
                    routable=manifest.status == "published",
                    badge="mine",
                    requires=tuple(manifest.requires),
                ))
        return extras

    def _workspace_config(self) -> Config:
        """The base config bound to this space's workspace.

        The workspace is the space's own with a layout; on a one-user server it
        is ``--path``, else the config's - or, when the config isolates its
        agents, ``workspace/user_<id>`` beside the config, so the container
        never mounts the config's directory itself.
        """
        config_path = str(self.deployment.config_path)
        if self.layout is not None:
            return Config(config_path, str(self.layout.workspace.resolve()))
        config = Config(config_path, self.deployment.working_directory)
        if not isolation_enabled(config) or self.deployment.working_directory is not None:
            return config
        workspace = self.deployment.config_path.parent / "workspace" / f"user_{self.user_id}"
        return Config(config_path, str(workspace.resolve()))

    def _start_container(self, config: Config, workspace: Path) -> Optional[str]:
        manager = self._container_manager = self._manager(config)
        return self._container_in(manager, workspace)

    def _manager(self, config: Config) -> ContainerManager:
        return ContainerManager(config, enabled=True if self.require_isolation else None)

    def _container_in(self, manager: ContainerManager, workspace: Path) -> Optional[str]:
        """The ID of the user's container working in *workspace*; None without one."""
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

    def _build_factory(
        self, config: Config, system_key: Optional[str] = None, *, container_id: Optional[str] = None
    ) -> AgentFactory:
        """One factory per system and workspace; history is shared so
        conversations survive routing.

        Every factory carries access to the store this user may build in.
        """
        factory = AgentFactory(
            config=config,
            working_directory=config.get_working_directory(),
            context_manager=self.conversations,
            container_id=container_id,
            policy_config=self.deployment.policy_config,
            session_db_path=str(self.layout.agent_sessions) if self.layout else None,
            # Preserve the default system's historical session IDs. Other
            # systems get distinct IDs even if they reuse its agent keys.
            session_namespace=(system_key if self.layout and system_key != self.registry.default_key() else None),
            logs_directory=str(self.layout.logs) if self.layout else None,
            confine_tools=self.require_isolation,
            model_access=self.model_access,
        )
        factory.system_builder = self._builder_access()
        # Every agent in the space learns the workspace-file link convention.
        factory.instructions_builder.platform_sections = [FILE_LINKS_SECTION]
        return factory

    def _builder_access(self) -> Optional[BuilderAccess]:
        store = self.deployment.created_store if self.admin else self.built_systems
        if store is None:
            return None
        activity = self.activity

        def record(key: str, event: str, note: str) -> None:
            if activity is not None:
                activity.record_event(access.system_key(key), event, by=self.user_id, note=note)

        def changed() -> None:
            if self.admin and self.on_systems_changed is not None:
                self.on_systems_changed()
            elif not self.admin:
                self.registry.refresh_extras()

        access = BuilderAccess(
            store=store,
            catalog=self.deployment.catalog,
            base_config=self.deployment.config,
            user_id=self.user_id,
            image=self.config.config.isolation.image,
            record=record,
            changed=changed,
            shared=self.admin,
            max_systems=None if self.admin else self.deployment.personal_agents_policy.max_systems,
            count_other=lambda: len(self.user_systems.list()) if self.user_systems is not None else 0,
            model_access=self.model_access,
        )
        return access

    @property
    def agent_sessions_path(self) -> Path:
        """The SQLite file of the agents' SDK sessions (AgentFactory's session_db_path)."""
        if self.layout is not None:
            return self.layout.agent_sessions
        return single_user_records(self.config)[1]

    @property
    def workspace_label(self) -> str:
        """The workspace as shown to the user: its path on a one-user server;
        on a shared one, where it lives on the server is not the user's business."""
        return str(self.workspace_path) if self.layout is None else "your workspace"

    # -- working directories of single chats ---------------------------------
    @property
    def can_choose_workspace(self) -> bool:
        """Whether a chat may work in a directory of its own: only on a
        one-user server - its user owns the machine - whose config lets the
        working directory be overridden."""
        return self.layout is None and bool(self.config.config.settings.allow_path_override)

    def conversation_workspace(self, context_id: Optional[str]) -> Path:
        """The directory the agents of chat *context_id* work in."""
        if not context_id or not self.can_choose_workspace:
            return self.workspace_path
        chosen = self.conversations.get_context_metadata(context_id).get(WORKSPACE_KEY)
        return Path(chosen) if chosen else self.workspace_path

    def workspace_label_of(self, context_id: Optional[str]) -> str:
        if self.layout is not None:
            return self.workspace_label
        return str(self.conversation_workspace(context_id))

    def choose_workspace(self, context_id: str, path: str) -> Path:
        """Have chat *context_id* work in directory *path* from its first turn on.

        Only before the chat's first message: the agents' sessions and what
        they said refer to the files of one directory. The space's own
        workspace clears the choice. Returns the directory, resolved.
        """
        if not self.can_choose_workspace:
            raise WorkspaceChoiceError(
                "This server gives every chat the same workspace"
                + (" (the config sets allow_path_override: false)." if self.layout is None else ".")
            )
        raw = (path or "").strip()
        if not raw:
            raise WorkspaceChoiceError("Name a directory.")
        directory = Path(raw).expanduser()
        if not directory.is_absolute():
            raise WorkspaceChoiceError(f"Give the full path of the directory, not '{raw}'.")
        directory = directory.resolve()
        if not directory.is_dir():
            raise WorkspaceChoiceError(f"No such directory: {directory}")
        if directory == Path(directory.anchor):
            raise WorkspaceChoiceError("The root of the file system cannot be a workspace.")
        view = self.conversations.conversation_view(context_id)
        if view is None:
            raise KeyError(context_id)
        if view["messages"] or self.turns.is_busy(context_id):
            raise WorkspaceChoiceError("The working directory is chosen before the chat's first message.")
        chosen = None if directory == self.workspace_path else str(directory)
        self.conversations.update_context_metadata(context_id, {WORKSPACE_KEY: chosen})
        return directory

    async def registry_for(self, context_id: Optional[str]) -> SystemRegistry:
        """The systems chat *context_id* runs on: the space's own, or those of
        the directory chosen for it, built - container included - on first use."""
        directory = self.conversation_workspace(context_id)
        if directory == self.workspace_path:
            return self.registry
        async with self._choosing:
            chosen = self._chosen.get(directory)
            if chosen is None:
                # Loading configs and starting a container block: off the loop.
                chosen = await asyncio.to_thread(self._choose, directory)
                self._chosen[directory] = chosen
        return chosen.registry

    def registry_of(self, context_id: Optional[str]) -> SystemRegistry:
        """The systems chat *context_id* runs on, once :meth:`registry_for`
        has built them; the space's own before."""
        chosen = self._chosen.get(self.conversation_workspace(context_id))
        return chosen.registry if chosen is not None else self.registry

    def _choose(self, directory: Path) -> ChosenWorkspace:
        config = Config(str(self.deployment.config_path), str(directory))
        if Path(config.get_working_directory()).resolve() != directory:
            raise WorkspaceChoiceError(f"The config does not let the agents work in {directory}.")
        manager: Optional[ContainerManager] = None
        container_id: Optional[str] = None
        if self.require_isolation or isolation_enabled(config):
            manager = self._manager(config)
            container_id = self._container_in(manager, directory)
            if self.require_isolation and container_id is None:
                raise IsolationUnavailable(f"The container for {directory} could not be started.")
        chosen = ChosenWorkspace(directory, config, container_id, manager)
        chosen.registry = self._registry(config, str(directory), container_id)
        logger.info("Workspace ready: user=%s workdir=%s container=%s", self.user_id, directory, container_id)
        return chosen

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
        """Pick the system and agent for *message*; ``None`` means route it.
        The factory resolved works in the chat's directory."""
        previous = None
        if context_id:
            metadata = self.conversation_metadata(context_id)
            if metadata.get("routed_system") and metadata.get("routed_agent"):
                previous = (metadata["routed_system"], metadata["routed_agent"])
        registry = await self.registry_for(context_id)
        return await registry.resolve(
            message, system_key=system_key, agent_key=agent_key, previous=previous
        )

    async def warm_agent(
        self, agent_key: str, system_key: Optional[str] = None, *, context_id: Optional[str] = None
    ) -> None:
        """Build an agent ahead of the first message that needs it, for the
        directory chat *context_id* works in."""
        registry = await self.registry_for(context_id)
        system = system_key or registry.default_key()
        prepared = (self.conversation_workspace(context_id), system, agent_key)
        async with self._lock:
            if prepared in self._prepared:
                return
            await registry.factory(system).create_agent(agent_key)
            self._prepared.add(prepared)
            logger.info("Prepared agent for %s: %s/%s in %s", self.user_id, system, agent_key, prepared[0])

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

    def admit_turn(self) -> Optional[str]:
        """None when the user may start a turn now (it is counted), or why not."""
        if self.limits is None:
            return None
        return self.limits.admit(self.turns.claimed_count)

    def record_usage(self, tokens_in: int, tokens_out: int) -> None:
        """Add a finished turn's tokens to the user's day (web_chat.limits)."""
        if self.turn_counter is None or not (tokens_in or tokens_out):
            return
        try:
            self.turn_counter.count_tokens(self.user_id, tokens_in, tokens_out)
        except Exception:
            logger.exception("Adding the user's token usage for the day failed")

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
                self._prepared.discard((self.workspace_path, system, key))
        return result

    # -- user systems --------------------------------------------------------
    def change_user_systems(self, edit: Callable[[UserSystems], T]) -> T:
        """Apply *edit* to the user's systems; the next turn runs them as they are now."""
        if self.user_systems is None:
            raise UserSystemError("Systems of your own need a server with accounts.")
        result = edit(self.user_systems)
        self.refresh_extras()
        return result

    def refresh_extras(self) -> None:
        """The space's own systems changed: the registry asks again, built agents go."""
        before = {system.key for system in self.registry.systems() if system.badge}
        self.registry.refresh_extras()
        after = {system.key for system in self.registry.systems() if system.badge}
        self._prepared = {prepared for prepared in self._prepared if prepared[1] not in before | after}

    # -- action reviews ----------------------------------------------------
    def _built_factories(self) -> list[tuple[str, AgentFactory]]:
        """(system, factory) of every factory built so far, in every workspace."""
        registries = [self.registry, *(chosen.registry for chosen in self._chosen.values())]
        return [item for registry in registries for item in registry.built_factories().items()]

    def pending_action_reviews(self) -> list[dict[str, Any]]:
        """Pending reviews from the factories that have handled a turn."""
        reviews: list[dict[str, Any]] = []
        for system_key, factory in self._built_factories():
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
        for _, factory in self._built_factories():
            gate = getattr(factory, "action_gate", None)
            if gate is not None and gate.resolve_review(approval_id, approve=approve):
                return True
        return False

    def answer_action_review(
        self, context_id: str, approval_id: str, *, approve: bool, remember: bool = False
    ) -> bool:
        """The user's answer, from the chat of *context_id*, to a call held there.

        Only where the policy lets users answer (``approvals: user``), and only
        for a review that came from this conversation. Reaches no agent tool:
        the chat socket is the user's.
        """
        for _, factory in self._built_factories():
            gate = getattr(factory, "action_gate", None)
            if gate is None or gate.config.approvals != "user":
                continue
            if gate.resolve_review(approval_id, approve=approve, remember=remember, context_id=context_id):
                return True
        return False

    def policy_filters(self) -> Optional[dict[str, Any]]:
        """The user's policy switch - every filter and the default - or None when
        the chat runs without an action policy."""
        policy = self.deployment.action_policy
        if policy is None:
            return None
        return {
            "default": policy.default_filter,
            "approvals": policy.approvals,
            "filters": [
                {"key": key, "label": item.label, "description": item.description}
                for key, item in policy.filters.items()
            ],
        }

    def set_policy_filter(self, context_id: str, name: str) -> bool:
        """Run conversation *context_id* under filter *name* from now on, its
        running turn included. False for a filter the policy does not offer."""
        policy = self.deployment.action_policy
        if policy is None or name not in policy.filters:
            return False
        self.conversations.update_context_metadata(context_id, {POLICY_FILTER_KEY: name})
        for _, factory in self._built_factories():
            if getattr(factory, "action_gate", None) is not None:
                factory.set_policy_filter(context_id, name)
        return True

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
        for chosen in list(self._chosen.values()):
            try:
                await chosen.registry.close()
            except Exception:
                logger.exception("Closing the systems working in %s failed", chosen.path)
            finally:
                if chosen.manager is not None and chosen.container_id:
                    await asyncio.to_thread(chosen.manager.stop_container, self.user_id, chosen.container_id)
        self._chosen.clear()
        try:
            await self.registry.close()
        finally:
            manager = getattr(self, "_container_manager", None)
            if manager is not None and self.container_id:
                await asyncio.to_thread(manager.stop_container, self.user_id, self.container_id)

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
