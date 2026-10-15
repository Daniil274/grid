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
  and container, so chats working in different directories never share one;
- with chat spaces (a server with accounts), a space of its own for every new
  chat: a directory and a container of the chat's, in which the chat's project
  space and the user's space are mounted too (:class:`ChatRoots`).

Where the state lives is the space's :class:`SpaceLayout`. A server for one
person keeps the layout it always had: conversations and sessions in the base
system's logs directory, the workspace from the config or ``--path``. A server
for many users gives every user a directory of their own
(:meth:`SpaceLayout.under`), outside the workspace, so the agents' tools never
reach the records of their own conversations.

Chat spaces. A user's work comes in three spaces, each a directory beside the
others under the user's own (never one inside another, so a container sees only
the ones it mounts):

- the chat's (``chats/<id>``): new for each chat, its agents' working
  directory - ``/workspace`` in its container;
- the project's (``projects/<id>``): shared by the chats of a project, at
  ``/project``;
- the user's - the space's workspace, where every chat worked before chat
  spaces - shared by all of the user's chats, at ``/user``.

A project keeps its tracker in its space; a chat without one plans in the
user's tracker (:meth:`UserSpace.tracker_dir`). Memory is kept by level
(core.factory.memory) in a file beside the workspace (``SpaceLayout.memory``).

When a chat is deleted its space - unless a fork or continuation still uses
it - and its memory go with it; a deleted project's too
(:meth:`UserSpace.release_chats`, :meth:`UserSpace.release_project`). A space
goes to the trash first, kept :data:`TRASH_SECONDS`. What all the user's
spaces hold together is measured in the background (:meth:`UserSpace.storage_used`)
for ``user_limits.storage_mb``.

A chat made since gets its space when it is made (:meth:`UserSpace.assign_chat_space`);
its branches and forks keep it. A chat from before has none and works in the
user's space as it always did. Each chat with a space runs in a container of
its own, built on its first turn and dropped when it has been idle a while or
too many others are loaded (:data:`MAX_CHAT_RUNTIMES`).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from core.agent_factory import AgentFactory
from core.config import Config
from core.config.prompt_sections import PromptSection
from core.context import ContextManager
from core.checkpoints import Checkpoints
from core.conversation_store import ConversationCodec, ConversationStore
from core.factory.policy import POLICY_FILTER_KEY
from core.managers.container_manager import ContainerManager
from core.model_access import ModelAccess
from core.system_store import BuilderAccess, SystemStore
from web_chat.deployment import Deployment, isolation_enabled
from web_chat.entitlements import Entitlement, OwnCredentials, UserCredentials
from web_chat.limits import TurnCounter, TurnLimits
from web_chat.personal_agents import PersonalAgentError, PersonalAgents, PersonalAgentStore
from web_chat.projects import ProjectStore
from web_chat.spend import SpaceMeter
from web_chat.system_activity import SystemActivity
from web_chat.systems import ExtraSystem, Resolution, SystemRegistry
from web_chat.user_systems import UserSystemError, UserSystems, UserSystemStore
from web_chat.turns import TurnBoard

logger = logging.getLogger("grid.web_chat.space")

T = TypeVar("T")

#: The conversation metadata naming the working directory chosen for that chat.
WORKSPACE_KEY = "workspace"
#: The conversation metadata naming the chat's own space (a directory under
#: ``SpaceLayout.chats``); branches and forks keep it (core.context BRANCH_INHERITED).
CHAT_SPACE_KEY = "chat_space"
#: What a chat space, and a project's, may be named: a context or project id.
SPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
#: Where a chat's container mounts its project's space and its user's.
PROJECT_MOUNT = "/project"
USER_MOUNT = "/user"
#: How many chats a space keeps ready to run - config, factories, container -
#: at once; the least recently used idle ones go first.
MAX_CHAT_RUNTIMES = 6
#: A chat's runtime unused for this long is dropped (its container removed);
#: its next turn builds it again.
CHAT_RUNTIME_IDLE_SECONDS = 20 * 60
#: How long the spaces of deleted chats and projects stay in the trash.
TRASH_SECONDS = 7 * 24 * 3600
#: How long a measurement of what the user's spaces hold is used.
STORAGE_STALE_SECONDS = 10 * 60
#: An empty chat older than this goes (UserSpace.prune_empty_chats): a "New
#: chat" never written in, or one an older server made at every start.
EMPTY_CHAT_SECONDS = 24 * 3600


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


def _disk_usage(root: Optional[Path]) -> int:
    """Bytes the files under *root* take on disk, without following a link."""
    if root is None or not root.is_dir():
        return 0
    total, pending = 0, [str(root)]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        else:
                            info = entry.stat(follow_symlinks=False)
                            total += getattr(info, "st_blocks", 0) * 512 or info.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def chat_spaces_section(roots: "ChatRoots", project_title: Optional[str]) -> PromptSection:
    """What the agents of a chat with a space of its own are told about the spaces they reach."""
    project = (
        f"- `/project` - the space of the project \"{project_title or roots.project_id}\", shared by every "
        "chat of the project: its code, documents and lasting results.\n"
        if roots.project is not None
        else ""
    )
    keep = "the project's space" if roots.project is not None else "the user's space"
    links = (
        "[spec.pdf](/api/workspace/files/project/docs/spec.pdf) or "
        if roots.project is not None
        else ""
    )
    return PromptSection(
        key="chat_spaces",
        scope="static",
        content=f"""## Where you work

This chat has a directory of its own, new for this chat: your working
directory - `/workspace` in your shell, where relative paths and the file tools
start. You also reach:

{project}- `/user` - the user's own space, shared by all of their chats: their files,
  uploads and the work of earlier chats.

Keep scratch work and intermediate files in your directory. Put what should
outlive this chat - a deliverable, a change to lasting files - into {keep}.
The shell and the file tools take these paths alike (`/user/notes.md`).

To hand over a file of another space, link it with that space's name first:
{links}[notes.md](/api/workspace/files/user/notes.md). The names `project` and
`user` at the top of your own directory are taken by these spaces.""",
    )


def conversation_directory(conversations: Path) -> Path:
    """Where the conversations kept in *conversations* (the old single file) live now."""
    return conversations.with_name(conversations.stem)


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
    #: The user's projects: the hierarchy over their conversations.
    projects: Path
    #: The chats' own spaces, one directory per chat (chat spaces).
    chat_spaces: Optional[Path] = None
    #: The projects' spaces, one directory per project (chat spaces).
    project_spaces: Optional[Path] = None
    #: The agents' memory, beside the workspace: no agent command reaches the file.
    memory: Optional[Path] = None
    #: The spaces of deleted chats and projects, for TRASH_SECONDS.
    trash: Optional[Path] = None

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
            projects=root / "projects.json",
            chat_spaces=root / "chats",
            project_spaces=root / "projects",
            memory=root / "memory.sqlite",
            trash=root / "trash",
        )


@dataclass(frozen=True)
class ChatRoots:
    """The spaces a chat with a space of its own works with (see the module docs)."""

    #: The chat's own directory: its agents' working directory.
    chat: Path
    #: The user's space, shared by all of their chats.
    user: Path
    #: The project's space, when the chat belongs to a project.
    project: Optional[Path] = None
    project_id: Optional[str] = None

    @property
    def key(self) -> tuple:
        """What a runtime is built for: another project means other mounts."""
        return (self.chat, self.project)

    def mounts(self) -> Dict[str, Path]:
        """The other spaces, by the path the container mounts them at."""
        mounts = {PROJECT_MOUNT: self.project} if self.project is not None else {}
        mounts[USER_MOUNT] = self.user
        return mounts

    def named(self) -> Dict[str, Path]:
        """The other spaces by the name links and the files page give them: ``project/...``."""
        return {path.strip("/"): host for path, host in self.mounts().items()}


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

    #: Whether new chats get spaces of their own; set by the constructor.
    chat_spaces = False

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
        local_cli_login: bool = False,
        chat_spaces: bool = False,
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

        ``local_cli_login``: this space is the explicit local, single-user
        server, so the machine's Claude Code CLI login is that one user's own
        credential (web_chat.entitlements). A server with accounts leaves it
        False and refuses the Claude provider.

        ``chat_spaces``: every new chat works in a space of its own, beside its
        project's and the user's (see the module docs). Needs a layout with
        ``chat_spaces``; without one it is off.
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
        self.local_cli_login = local_cli_login
        self.chat_spaces = bool(chat_spaces and layout is not None and layout.chat_spaces is not None)
        self.entitlement = entitlement
        policy = (lambda: entitlement().limits) if entitlement is not None else (lambda: deployment.user_limits)
        spend = usage if hasattr(usage, "spent_micro") else None
        self._storage_bytes: Optional[int] = None
        self._storage_at: Optional[float] = None
        self._measuring = False
        self.limits = (
            TurnLimits(user_id, policy, turn_counter, spend=spend,
                       storage=self.storage_used if layout is not None else None)
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
        # The runtimes of chats with spaces of their own (ChatRoots.key), and when each was last asked for.
        self._chat_runtimes: Dict[tuple, ChosenWorkspace] = {}
        self._runtime_used: Dict[tuple, float] = {}
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
            UserCredentials(
                self.user_id,
                entitlement,
                self.deployment.plans,
                own,
                local_cli_login=self.local_cli_login,
            )
            if entitlement is not None
            else None
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
        self._move_memory_out_of_workspace()

        if self.layout is not None:
            self.conversations_path = self.layout.conversations
        else:
            self.conversations_path, _ = single_user_records(config)
        self.conversations_path.parent.mkdir(parents=True, exist_ok=True)
        # What the workspace looked like before each turn, for Restore.
        self.checkpoints = Checkpoints(self.conversations_path.parent / "checkpoints")
        # One file per conversation, compressed and - with GRID_SECRETS_KEY -
        # encrypted, written in the background (core.conversation_store). The
        # old single file, if any, is moved into it on first load.
        self.conversations = ContextManager(
            max_history=config.get_max_history(),
            # Every chat is named by its id: starting the space must not add an empty one.
            create_initial_context=False,
            persist_path=str(self.conversations_path),
            store=ConversationStore(
                conversation_directory(self.conversations_path),
                ConversationCodec.from_secrets(os.environ.get("GRID_SECRETS_KEY")),
            ),
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

    def _move_memory_out_of_workspace(self) -> None:
        """Move the memory a space kept in its workspace (``.grid/memory.sqlite``)
        to the layout's file beside it, once: the agents' commands reached it
        there. Done while the space is built, before anything opens it."""
        target = self.layout.memory if self.layout is not None else None
        if target is None or target.exists():
            return
        old = self.workspace_path / ".grid" / "memory.sqlite"
        if not old.is_file() or old.is_symlink():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            part = old.with_name(old.name + suffix)
            if part.is_file() and not part.is_symlink():
                os.replace(part, target.with_name(target.name + suffix))
        logger.info("Moved the memory of %s out of its workspace to %s", self.user_id, target)

    def _registry(
        self,
        config: Config,
        workdir: Optional[str],
        container_id: Optional[str],
        roots: Optional[ChatRoots] = None,
    ) -> SystemRegistry:
        """The systems of the space, loaded with *workdir*, their tools in
        *container_id*; for a chat with a space of its own, reaching *roots*."""
        build = functools.partial(self._build_factory, container_id=container_id, roots=roots)
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

    def _container_in(
        self, manager: ContainerManager, workspace: Path, *, mounts: Optional[Dict[str, Path]] = None
    ) -> Optional[str]:
        """The ID of the user's container working in *workspace* - with the
        other spaces *mounts* names mounted too; None without one."""
        if not manager.enabled:
            return None
        try:
            if mounts:
                container = manager.get_or_create_container(self.user_id, workspace=workspace, mounts=mounts)
            else:
                container = manager.get_or_create_container(self.user_id, workspace=workspace)
        except Exception as exc:
            logger.warning("Failed to initialize container isolation for %s: %s", self.user_id, exc)
            return None
        if container:
            logger.info("Container ready for %s: %s", self.user_id, container.name)
            return container.id
        return None

    def _build_factory(
        self,
        config: Config,
        system_key: Optional[str] = None,
        *,
        container_id: Optional[str] = None,
        roots: Optional[ChatRoots] = None,
    ) -> AgentFactory:
        """One factory per system and workspace; history is shared so
        conversations survive routing.

        Every factory carries access to the store this user may build in. The
        factory of a chat with a space of its own reaches its other spaces
        (*roots*), and keeps its plan and memory in the user's space.
        """
        self._inherit_memory(config, system_key)
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
        # A chat remembers at its own level, its project's and the user's (core.factory.memory),
        # in a file beside the workspace that the agents' commands cannot open.
        factory.memory_scopes = self.memory_scopes
        if self.layout is not None and self.layout.memory is not None:
            factory.memory_db = str(self.layout.memory)
        # Every agent in the space learns the workspace-file link convention.
        sections = [FILE_LINKS_SECTION]
        if roots is not None:
            factory.extra_roots = {path: str(host) for path, host in roots.mounts().items()}
            # A project's chats plan in its tracker; the others in the user's.
            factory.tracker_directory = str(roots.project or roots.user)
            factory.memory_root = str(roots.user)
            project = self.project_store.get(roots.project_id) if roots.project_id else None
            sections.append(chat_spaces_section(roots, project.title if project else None))
        factory.instructions_builder.platform_sections = sections
        return factory

    def _inherit_memory(self, config: Config, system_key: Optional[str]) -> None:
        """A system whose config says nothing about memory remembers as the default one does.

        Memory is the user's, not a system's: a chat routed to a system the
        builder made (no ``memory:`` section) would otherwise neither recall nor
        store anything. Its settings come from the default system, with the
        provider its embeddings need; the system's own config file is untouched.
        A system that sets ``memory:`` - even ``enabled: false`` - keeps its own.
        """
        if "memory" in config.config.model_fields_set:
            return
        try:
            base = self.registry.config(self.registry.default_key())
        except Exception:  # noqa: BLE001 - no default to inherit from: the system's own settings
            return
        if base is config or "memory" not in base.config.model_fields_set:
            return
        settings = base.get_memory_config()
        config.config.memory = settings.model_copy(deep=True)
        provider = settings.embeddings.provider
        if provider and provider not in config.config.providers and provider in base.config.providers:
            config.config.providers[provider] = base.config.providers[provider].model_copy(deep=True)

    def _builder_access(self) -> Optional[BuilderAccess]:
        store = self.deployment.created_store if self.admin else self.built_systems
        if store is None:
            return None
        activity = self.activity

        def record(key: str, event: str, note: str) -> None:
            if activity is not None:
                activity.record_event(access.system_key(key), event, by=self.user_id, note=note)

        def changed() -> None:
            self.refresh_extras()
            if self.admin and self.on_systems_changed is not None:
                self.on_systems_changed()

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
        roots = self.chat_roots(context_id)
        if roots is not None:
            return roots.chat
        if not context_id or not self.can_choose_workspace:
            return self.workspace_path
        chosen = self.conversations.get_context_metadata(context_id).get(WORKSPACE_KEY)
        return Path(chosen) if chosen else self.workspace_path

    def workspace_label_of(self, context_id: Optional[str]) -> str:
        roots = self.chat_roots(context_id)
        if roots is not None:
            spaces = ["this chat's own space"]
            if roots.project_id is not None:
                project = self.project_store.get(roots.project_id)
                spaces.append(f"project {project.title if project else roots.project_id}")
            return " · ".join([*spaces, "your files"])
        if self.layout is not None:
            return self.workspace_label
        return str(self.conversation_workspace(context_id))

    # -- chat spaces -------------------------------------------------------
    def assign_chat_space(self, context_id: str, *, like: Optional[str] = None) -> Optional[str]:
        """Give new chat *context_id* a space of its own - or the space of chat
        *like*, which it continues. None where chats have no spaces."""
        if not self.chat_spaces:
            return None
        inherited = self.conversations.get_context_metadata(like).get(CHAT_SPACE_KEY) if like else None
        space_id = inherited if inherited and SPACE_ID.fullmatch(str(inherited)) else context_id
        if not SPACE_ID.fullmatch(space_id):
            return None
        self.conversations.update_context_metadata(context_id, {CHAT_SPACE_KEY: space_id})
        return space_id

    def chat_roots(self, context_id: Optional[str]) -> Optional[ChatRoots]:
        """The spaces chat *context_id* works with; None for a chat without a
        space of its own (made before chat spaces, or chat spaces off)."""
        if not self.chat_spaces or not context_id:
            return None
        metadata = self.conversations.get_context_metadata(context_id)
        space_id = metadata.get(CHAT_SPACE_KEY)
        if not space_id or not SPACE_ID.fullmatch(str(space_id)):
            return None
        # A branch belongs to the project of its conversation.
        root = self.conversations.branch_root(context_id)
        project_id = metadata.get("project_id")
        if root != context_id:
            project_id = self.conversations.get_context_metadata(root).get("project_id") or project_id
        project = None
        if project_id and SPACE_ID.fullmatch(str(project_id)) and self.project_exists(project_id):
            project = (self.layout.project_spaces / project_id).resolve()
        else:
            project_id = None
        return ChatRoots(
            chat=(self.layout.chat_spaces / space_id).resolve(),
            user=self.workspace_path,
            project=project,
            project_id=project_id,
        )

    def file_spaces(self, context_id: Optional[str]) -> tuple[Path, Dict[str, Path]]:
        """Where the files of chat *context_id* are: the directory paths start
        in, and the other spaces by the name a path gives them (``project/...``)."""
        roots = self.chat_roots(context_id)
        if roots is None:
            return self.conversation_workspace(context_id), {}
        # A chat's space exists from its first file on: an upload may come before its first turn.
        for directory in (roots.chat, roots.project):
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)
        return roots.chat, roots.named()

    def memory_scopes(self, context_id: Optional[str]) -> Dict[str, tuple[str, str]]:
        """The memory levels chat *context_id* has besides its user's
        (core.factory.memory): level -> (key, title). Its own - one per space,
        or per conversation for a chat without one - and its project's."""
        if not context_id or self.conversations.conversation_view(context_id) is None:
            return {}
        root = self.conversations.branch_root(context_id)
        metadata = self.conversations.get_context_metadata(context_id)
        own = metadata.get(CHAT_SPACE_KEY) or root
        scopes: Dict[str, tuple[str, str]] = {}
        if SPACE_ID.fullmatch(str(own)):
            scopes["chat"] = (str(own), str(metadata.get("title") or ""))
        project_id = self.conversations.get_context_metadata(root).get("project_id") or metadata.get("project_id")
        project = self.project_store.get(project_id) if project_id and SPACE_ID.fullmatch(str(project_id)) else None
        if project is not None:
            scopes["project"] = (project.id, project.title)
        return scopes

    # -- what goes with a chat or a project ------------------------------------
    def chat_space_ids(self, context_ids: List[str]) -> set:
        """The spaces chats *context_ids* work in."""
        found = set()
        for context_id in context_ids:
            space_id = self.conversations.get_context_metadata(context_id).get(CHAT_SPACE_KEY)
            if space_id and SPACE_ID.fullmatch(str(space_id)):
                found.add(str(space_id))
        return found

    async def release_chats(self, deleted: List[str], space_ids: set) -> List[str]:
        """After chats *deleted* went, the spaces they used - *space_ids* - that no
        other chat uses go to the trash, their runtimes close and their memory -
        and the memory of the chats themselves - is forgotten. The spaces released."""
        in_use = {
            str((view.get("metadata") or {}).get(CHAT_SPACE_KEY))
            for view in self.conversations.conversation_views()
        }
        released = [space_id for space_id in sorted(space_ids) if space_id not in in_use]
        if self.chat_spaces:
            async with self._choosing:
                for space_id in released:
                    chat = (self.layout.chat_spaces / space_id).resolve()
                    for key in [key for key in self._chat_runtimes if key[0] == chat]:
                        self._runtime_used.pop(key, None)
                        await self._close_runtime(self._chat_runtimes.pop(key), remove=True)
                    await asyncio.to_thread(self._to_trash, chat, "chats")
        forgotten = {*released, *(context_id for context_id in deleted if SPACE_ID.fullmatch(context_id))}
        forgotten -= in_use
        await asyncio.to_thread(self._forget_memory, "chat", forgotten)
        return released

    async def release_project(self, project_id: str) -> None:
        """A project was deleted: its space goes to the trash, the runtimes built
        for it close and its memory is forgotten. Its chats moved elsewhere."""
        if not SPACE_ID.fullmatch(str(project_id)):
            return
        if self.chat_spaces:
            directory = (self.layout.project_spaces / project_id).resolve()
            async with self._choosing:
                for key in [key for key in self._chat_runtimes if key[1] == directory]:
                    self._runtime_used.pop(key, None)
                    await self._close_runtime(self._chat_runtimes.pop(key), remove=True)
                await asyncio.to_thread(self._to_trash, directory, "projects")
        await asyncio.to_thread(self._forget_memory, "project", {project_id})

    def _to_trash(self, directory: Path, kind: str) -> None:
        trash = self.layout.trash if self.layout is not None else None
        if trash is None or not directory.is_dir() or directory.is_symlink():
            return
        target = trash / kind / f"{directory.name}-{int(time.time())}"
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(directory, target)
        logger.info("Moved %s of %s to the trash: %s", directory.name, self.user_id, target)

    def _forget_memory(self, level: str, keys: set) -> None:
        if not keys:
            return
        factory = self.registry.factory(self.registry.default_key())
        for key in keys:
            try:
                manager = factory.level_memory_manager(level, key, force=True)
                if manager is not None:
                    manager.forget_all()
            except Exception:  # noqa: BLE001 - memory that cannot go now stays; the chat is gone anyway
                logger.warning("Could not forget the %s memory %s of %s", level, key, self.user_id, exc_info=True)

    def empty_trash(self, older_than: float = TRASH_SECONDS) -> int:
        """Remove the spaces in the trash longer than *older_than* seconds; how many."""
        trash = self.layout.trash if self.layout is not None else None
        if trash is None or not trash.is_dir():
            return 0
        now, removed = time.time(), 0
        for kind in trash.iterdir():
            if not kind.is_dir() or kind.is_symlink():
                continue
            for entry in kind.iterdir():
                if entry.is_symlink() or now - entry.lstat().st_mtime < older_than:
                    continue
                # rmtree never follows a link out of the trash.
                shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)
                removed += 1
        return removed

    # -- disk ------------------------------------------------------------------
    def storage_used(self) -> Optional[int]:
        """The bytes all the user's spaces hold, as last measured; a stale or
        missing measurement is taken again in the background (None until then)."""
        stale = self._storage_at is None or time.monotonic() - self._storage_at > STORAGE_STALE_SECONDS
        if stale and not self._measuring and self.layout is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._measuring = True
                self._spawn(asyncio.to_thread(self._measure_storage), "measure-storage")
        return self._storage_bytes

    def _measure_storage(self) -> int:
        try:
            roots = [self.workspace_path]
            if self.layout is not None:
                roots += [path for path in (self.layout.chat_spaces, self.layout.project_spaces, self.layout.trash) if path]
            total = sum(_disk_usage(root) for root in roots)
            self._storage_bytes, self._storage_at = total, time.monotonic()
            return total
        finally:
            self._measuring = False

    async def maintain(self) -> None:
        """Periodic upkeep of a space in use: idle chat runtimes go, so do old
        empty chats, the trash is emptied of old spaces, and the disk is
        measured again when stale."""
        await self.sweep_chat_runtimes()
        await self.prune_empty_chats()
        await asyncio.to_thread(self.empty_trash)
        self.storage_used()

    async def prune_empty_chats(self, older_than: float = EMPTY_CHAT_SECONDS) -> List[str]:
        """Delete the chats nobody wrote in for *older_than* seconds - at once,
        those the server made by itself; their ids.

        A chat stays while a turn holds it, while it is the root of branches,
        and while its own space holds files (an upload before the first
        message): only what holds nothing goes.
        """
        from datetime import datetime

        busy = set(self.turns.claimed()) | {context for context, _turn, _task in self.turns.running_turns()}
        views = self.conversations.conversation_views()
        roots = {
            (view.get("metadata") or {}).get("branch_root")
            for view in views
            if (view.get("metadata") or {}).get("branch_root")
        }
        now = datetime.now()
        doomed: List[str] = []
        for view in views:
            context_id, metadata = view["id"], view.get("metadata") or {}
            if view["messages"] or context_id in busy or context_id in roots or metadata.get("active_branch"):
                continue
            try:
                age = (now - datetime.fromisoformat(str(view.get("updated_at")))).total_seconds()
            except (TypeError, ValueError):
                continue
            # One the server made by itself (an older server, at every start) was
            # never anyone's chat: it goes at once; a "New chat" waits a day.
            if age < older_than and metadata.get("created_by_web"):
                continue
            spaces = self.chat_roots(context_id)
            if spaces is not None and spaces.chat.is_dir() and any(spaces.chat.iterdir()):
                continue
            doomed.append(context_id)
        if not doomed:
            return []
        spaces = self.chat_space_ids(doomed)
        for context_id in doomed:
            self.conversations.delete_context(context_id)
        await self.release_chats(doomed, spaces)
        logger.info("Deleted %d empty chat(s) of %s", len(doomed), self.user_id)
        return doomed

    def tracker_dir(self, project_id: Optional[str] = None) -> Path:
        """Where the tracker of project *project_id* is - its space, with chat
        spaces - or the user's (the workspace) for none, or without chat spaces."""
        if (
            project_id
            and self.chat_spaces
            and SPACE_ID.fullmatch(str(project_id))
            and self.project_exists(project_id)
        ):
            directory = (self.layout.project_spaces / project_id).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        return self.workspace_path

    def project_trackers(self) -> Dict[str, Path]:
        """The projects whose space holds a tracker, by id."""
        if not self.chat_spaces or self.layout.project_spaces is None:
            return {}
        found = {}
        for project in self.project_store.list():
            directory = self.layout.project_spaces / project.id
            if (directory / ".beads").is_dir():
                found[project.id] = directory.resolve()
        return found

    def file_space_labels(self, context_id: Optional[str]) -> Dict[str, str]:
        """How the files page titles the other spaces of chat *context_id*."""
        roots = self.chat_roots(context_id)
        if roots is None:
            return {}
        labels = {"user": "Your files"}
        if roots.project_id is not None:
            project = self.project_store.get(roots.project_id)
            labels["project"] = f"Project · {project.title}" if project else "Project"
        return labels

    def checkpoint_roots(self, context_id: Optional[str]) -> Dict[str, Path]:
        """The directories a turn of chat *context_id* may change, by the name
        its changes are listed under ("" for its own)."""
        primary, others = self.file_spaces(context_id)
        return {"": primary, **others}

    def snapshot(self, context_id: Optional[str]) -> dict:
        """A checkpoint of every space a turn of the chat may change."""
        roots = self.checkpoint_roots(context_id)
        if list(roots) == [""]:
            return self.checkpoints.snapshot(roots[""])
        return {"spaces": {name: self.checkpoints.snapshot(path) for name, path in roots.items() if path.is_dir()}}

    def checkpoint_changes(self, before: dict, after: dict) -> List[dict]:
        """The files that differ between two of the chat's checkpoints, each
        named as its space's links name it."""
        if "spaces" not in before:
            return self.checkpoints.changes(before, after)
        found: List[dict] = []
        for name, old in before["spaces"].items():
            new = (after.get("spaces") or {}).get(name)
            if new is None:
                continue
            for change in self.checkpoints.changes(old, new):
                found.append({**change, "path": f"{name}/{change['path']}" if name else change["path"]})
        return found

    def restore_checkpoint(
        self, context_id: Optional[str], checkpoint: dict, turns: Optional[List[dict]] = None
    ) -> List[dict]:
        """Bring the chat's spaces back to *checkpoint*; the files that came back.

        The chat's own space comes back whole. Its project's and its user's are
        shared with other chats, so only what this chat's turns since changed
        comes back there - *turns*, each ``{"before", "after"}``, oldest first -
        and only where nobody changed the file after the chat's last turn. A
        turn without its "after" (kept before this) gives no such list: nothing
        of a shared space is put back for it.
        """
        if "spaces" not in checkpoint:
            return self.checkpoints.restore(self.conversation_workspace(context_id), checkpoint)
        restored: List[dict] = []
        roots = self.checkpoint_roots(context_id)
        for name, old in checkpoint["spaces"].items():
            path = roots.get(name)
            if path is None or str(path) != old.get("workspace"):
                # The chat left that project since: its files are not the chat's to put back.
                logger.info("Checkpoint space %r of %s is not the chat's now; left as it is", name, context_id)
                continue
            only = None if not name else self._own_changes(path, name, turns or [])
            if only is not None and not only:
                continue
            for change in self.checkpoints.restore(path, old, only=only):
                restored.append({**change, "path": f"{name}/{change['path']}" if name else change["path"]})
        return restored

    def _own_changes(self, path: Path, name: str, turns: List[dict]) -> set:
        """The files of shared space *name* this chat's *turns* changed and
        nobody changed after its last one."""
        mine: set = set()
        last_after = None
        for turn in turns:
            before = (turn.get("before") or {}).get("spaces", {}).get(name)
            after = (turn.get("after") or {}).get("spaces", {}).get(name)
            if before is None or after is None:
                continue
            mine.update(change["path"] for change in self.checkpoints.changes(before, after))
            last_after = after
        if last_after is None or not mine:
            return set()
        since = {change["path"] for change in self.checkpoints.changes(last_after, self.checkpoints.snapshot(path))}
        return mine - since

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
        """The systems chat *context_id* runs on: the space's own, those of
        the directory chosen for it, or those of its own space - built,
        container included, on first use."""
        roots = self.chat_roots(context_id)
        if roots is not None:
            return await self._chat_runtime(roots)
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
        roots = self.chat_roots(context_id)
        if roots is not None:
            runtime = self._chat_runtimes.get(roots.key)
            return runtime.registry if runtime is not None else self.registry
        chosen = self._chosen.get(self.conversation_workspace(context_id))
        return chosen.registry if chosen is not None else self.registry

    async def memory_registry(self, context_id: Optional[str]) -> SystemRegistry:
        """The systems whose memory chat *context_id* uses. A chat with a space
        of its own remembers in the user's space, as the space's own systems
        do: showing its memory never starts its container."""
        if self.chat_roots(context_id) is not None:
            return self.registry
        return await self.registry_for(context_id)

    async def _chat_runtime(self, roots: ChatRoots) -> SystemRegistry:
        async with self._choosing:
            runtime = self._chat_runtimes.get(roots.key)
            if runtime is None:
                # Making directories, loading configs and starting a container block: off the loop.
                runtime = await asyncio.to_thread(self._build_chat_runtime, roots)
                self._chat_runtimes[roots.key] = runtime
                # Closed under the lock: a chat's next runtime must not find the
                # container its last one is still removing.
                for old in self._take_idle_runtimes(keep=roots.key):
                    await self._close_runtime(old, remove=True)
            self._runtime_used[roots.key] = time.monotonic()
        return runtime.registry

    def _build_chat_runtime(self, roots: ChatRoots) -> ChosenWorkspace:
        """The config, container and systems of a chat with a space of its own."""
        for directory in (roots.chat, roots.project):
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)
        config = Config(str(self.deployment.config_path), str(roots.chat))
        manager: Optional[ContainerManager] = None
        container_id: Optional[str] = None
        if self.require_isolation or isolation_enabled(config):
            manager = self._manager(config)
            container_id = self._container_in(manager, roots.chat, mounts=roots.mounts())
            if self.require_isolation and container_id is None:
                raise IsolationUnavailable(f"The container of chat space {roots.chat.name} could not be started.")
        runtime = ChosenWorkspace(roots.chat, config, container_id, manager)
        runtime.registry = self._registry(config, str(roots.chat), container_id, roots)
        logger.info(
            "Chat space ready: user=%s chat=%s project=%s container=%s",
            self.user_id, roots.chat.name, roots.project_id, container_id,
        )
        return runtime

    def _busy_runtimes(self) -> set:
        """The runtimes a turn is claimed or running in now."""
        busy = set()
        contexts = set(self.turns.claimed()) | {context for context, _turn, _task in self.turns.running_turns()}
        for context_id in contexts:
            roots = self.chat_roots(context_id)
            if roots is not None:
                busy.add(roots.key)
        return busy

    def _take_idle_runtimes(
        self, *, keep: Optional[tuple] = None, idle_seconds: Optional[float] = None
    ) -> List[ChosenWorkspace]:
        """Take out the least recently used idle chat runtimes beyond
        :data:`MAX_CHAT_RUNTIMES`, and, given *idle_seconds*, every idle one
        unused that long; the caller closes them."""
        busy = self._busy_runtimes()
        now = time.monotonic()
        candidates = sorted(
            (key for key in self._chat_runtimes if key != keep and key not in busy),
            key=lambda key: self._runtime_used.get(key, 0.0),
        )
        excess = max(0, len(self._chat_runtimes) - MAX_CHAT_RUNTIMES)
        doomed = candidates[:excess]
        if idle_seconds is not None:
            doomed += [
                key for key in candidates[excess:] if now - self._runtime_used.get(key, 0.0) >= idle_seconds
            ]
        taken = []
        for key in doomed:
            runtime = self._chat_runtimes.pop(key)
            self._runtime_used.pop(key, None)
            self._prepared = {prepared for prepared in self._prepared if prepared[0] != runtime.path}
            taken.append(runtime)
        return taken

    async def sweep_chat_runtimes(self, idle_seconds: float = CHAT_RUNTIME_IDLE_SECONDS) -> int:
        """Drop the chat runtimes nobody used for *idle_seconds*; how many."""
        if not self._chat_runtimes:
            return 0
        async with self._choosing:
            doomed = self._take_idle_runtimes(idle_seconds=idle_seconds)
            for runtime in doomed:
                await self._close_runtime(runtime, remove=True)
        return len(doomed)

    async def _close_runtime(self, runtime: ChosenWorkspace, *, remove: bool = False) -> None:
        """Close a runtime's systems and stop - or, *remove*, remove - its container."""
        try:
            await runtime.registry.close()
        except Exception:
            logger.exception("Closing the systems working in %s failed", runtime.path)
        finally:
            if runtime.manager is not None and runtime.container_id:
                stop = runtime.manager.remove_container if remove else runtime.manager.stop_container
                await asyncio.to_thread(stop, self.user_id, runtime.container_id)

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
        registries = [
            self.registry,
            *(chosen.registry for chosen in self._chosen.values()),
            *(runtime.registry for runtime in getattr(self, "_chat_runtimes", {}).values()),
        ]
        before = {system.key for system in self.registry.systems() if system.badge}
        for registry in registries:
            registry.refresh_extras()
        after = {system.key for system in self.registry.systems() if system.badge}
        self._prepared = {prepared for prepared in self._prepared if prepared[1] not in before | after}

    # -- action reviews ----------------------------------------------------
    def _built_factories(self) -> list[tuple[str, AgentFactory]]:
        """(system, factory) of every factory built so far, in every workspace."""
        registries = [
            self.registry,
            *(chosen.registry for chosen in self._chosen.values()),
            *(runtime.registry for runtime in getattr(self, "_chat_runtimes", {}).values()),
        ]
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

    def awaiting_approval(self) -> Dict[str, list[dict[str, Any]]]:
        """Calls held for the user's answer right now, by the chat they wait in.

        Only calls that wait (a turn is running and holds them) and that the user
        may answer from the chat (``approvals: user``): what the rail marks and
        the browser notifies about (web_chat/js/ui/approval-alerts.js).
        """
        found: Dict[str, list[dict[str, Any]]] = {}
        for _, factory in self._built_factories():
            gate = getattr(factory, "action_gate", None)
            if gate is None or gate.config.approvals != "user":
                continue
            for review in gate.pending_reviews():
                if review["waiting"] and review.get("context_id"):
                    found.setdefault(review["context_id"], []).append(
                        {"approval_id": review["approval_id"], "tool": review["tool"],
                         "created_at": review["created_at"]})
        return found

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
        store = getattr(getattr(self, "conversations", None), "store", None)
        if store is not None:
            # What the turns wrote last is on disk before the space goes.
            await asyncio.to_thread(store.flush, durable=True)
        pending = list(self._background)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for chosen in list(self._chosen.values()):
            await self._close_runtime(chosen)
        self._chosen.clear()
        # A chat's container is the chat's alone: it goes with its runtime.
        for runtime in list(getattr(self, "_chat_runtimes", {}).values()):
            await self._close_runtime(runtime, remove=True)
        getattr(self, "_chat_runtimes", {}).clear()
        getattr(self, "_runtime_used", {}).clear()
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

    # -- projects ---------------------------------------------------------
    @property
    def project_store(self) -> ProjectStore:
        """The space's projects, loaded lazily so a build without projects pays nothing."""
        store = getattr(self, "_project_store", None)
        if store is None:
            path = (
                self.layout.projects
                if self.layout is not None
                else self.conversations_path.parent / "projects.json"
            )
            store = ProjectStore(path)
            self._project_store = store
        return store

    def project_exists(self, project_id: Optional[str]) -> bool:
        """Whether a project of the space has that id."""
        if not project_id:
            return False
        return self.project_store.exists(project_id)

    def conversations_by_project(self) -> Dict[Optional[str], List[str]]:
        """The ids of every conversation, grouped by its ``project_id``.

        A conversation without a project answers under the ``None`` key (the
        rail's "no project"). This scans every context's metadata, so it is
        ``O(n)`` in the number of conversations - fine at the sizes a space
        holds; a ``updated_at``-bucket cache is a later, deliberate change.
        """
        grouped: Dict[Optional[str], List[str]] = {}
        for view in self.conversations.conversation_views():
            project_id = (view.get("metadata") or {}).get("project_id") or None
            grouped.setdefault(project_id, []).append(view["id"])
        return grouped
