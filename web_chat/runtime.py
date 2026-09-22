"""Web runtime: the same config, path and session resolution as ``agent_chat``.

Started without an explicit ``--config`` it loads the system catalog
(``routing.yaml``) and every message is routed across the systems in it, just
like a CLI session started without ``--agent``/``--config``. Given a config it
runs that one system, which can still route between its own agents.

Memory, workspace and conversation storage belong to the runtime, not to a
system, so routing a follow-up elsewhere keeps the conversation intact.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from core.agent_factory import AgentFactory
from core.config import Config
from core.memory.store import MemoryStore
from core.memory.unified import UnifiedMemory
from core.managers.container_manager import ContainerManager
from web_chat.systems import Resolution, SystemRegistry

logger = logging.getLogger("grid.web_chat.runtime")
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CONFIG = "config.yaml"
DEFAULT_ROUTING = "routing.yaml"


def _resolve_path(path_value: str) -> Path:
    """Resolve against the project root first, then the working directory."""
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path.resolve(strict=False)

    project_path = PROJECT_ROOT / path
    if project_path.exists():
        return project_path.resolve(strict=False)

    cwd_path = path.resolve(strict=False)
    return cwd_path if cwd_path.exists() else project_path.resolve(strict=False)


def _isolation_enabled(config: Config) -> bool:
    isolation_cfg = getattr(config.config, "isolation", None)
    if not isolation_cfg:
        return False
    if isinstance(isolation_cfg, dict):
        return bool(isolation_cfg.get("enabled", False))
    return bool(getattr(isolation_cfg, "enabled", False))


class WebChatRuntime:
    """Owns the systems, the shared memory and the workspace of a web session."""

    def __init__(
        self,
        *,
        config_path: Optional[str] = None,
        routing_path: Optional[str] = DEFAULT_ROUTING,
        working_directory: Optional[str] = None,
        user_id: str = "default_user",
        action_review_token: Optional[str] = None,
    ) -> None:
        self.requested_config_path = config_path
        self.routing_path = _resolve_path(routing_path) if routing_path else None
        self.user_id = user_id
        self.working_directory_override = working_directory
        self.action_review_token = action_review_token

        self._lock = asyncio.Lock()
        self._prepared: set[Tuple[str, str]] = set()

        self.config: Config
        self.config_path: Path
        self.container_id: Optional[str]
        self.workspace_path: Path
        self.persist_path: Path
        self.memory_store: MemoryStore
        self.unified_memory: UnifiedMemory
        self.registry: SystemRegistry

        self._build_runtime()

    # -- construction ------------------------------------------------------
    def _catalog_config(self) -> Optional[Config]:
        """The system catalog, unless a single system was requested explicitly."""
        if self.requested_config_path:
            return None
        if self.routing_path is None:
            raise ValueError("A routing catalog is required when --config is not specified")
        if not self.routing_path.is_file():
            raise FileNotFoundError(
                f"Routing catalog not found: {self.routing_path}. "
                "Pass --routing or use --config for one system."
            )
        return Config(str(self.routing_path))

    def _base_config_path(self, catalog: Optional[Config]) -> Path:
        """The system whose workspace, logs and memory the session uses."""
        if self.requested_config_path:
            return _resolve_path(self.requested_config_path)
        if catalog is not None:
            from core.routing import AutoRouter

            router = AutoRouter.from_config(catalog)
            if router is not None:
                return router.system_config_path(router.default_system())
        return _resolve_path(DEFAULT_CONFIG)

    def _build_runtime(self) -> None:
        catalog = self._catalog_config()
        # The catalog is the operator-owned policy source for every routed
        # system, matching the CLI runtime. Individual system policy remains
        # the fallback when no catalog policy is enabled.
        self._policy_config = catalog
        self.config_path = self._base_config_path(catalog)
        config = Config(str(self.config_path), self.working_directory_override)
        container_id: Optional[str] = None

        if _isolation_enabled(config):
            workspace = self._isolated_workspace()
            container_id = self._start_container(config, workspace)
            config = Config(str(self.config_path), str(workspace))

        self.config = config
        self.container_id = container_id
        self.workspace_path = Path(config.get_working_directory()).resolve()
        self.workspace_path.mkdir(parents=True, exist_ok=True)

        logs_dir = Path(config.get_absolute_path("logs"))
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.persist_path = logs_dir

        self.unified_memory = UnifiedMemory(
            workspace=self.workspace_path,
            persist_path=logs_dir,
            max_history=config.get_max_history(),
        )

        data_dir = Path(config.get_absolute_path("data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_store = MemoryStore(str(data_dir / "memory.db"), config=config)

        self.registry = SystemRegistry(
            base_config=config,
            catalog=catalog,
            build_factory=self._build_factory,
            working_directory=self.working_directory_override,
        )
        self._prepared.clear()

        logger.info(
            "Web runtime ready: catalog=%s base=%s workdir=%s systems=%s",
            self.routing_path if catalog else "none",
            self.config_path,
            self.workspace_path,
            ", ".join(self.registry.keys()),
        )

    def _isolated_workspace(self) -> Path:
        if self.working_directory_override is not None:
            workspace = Path(self.working_directory_override).expanduser().resolve()
        else:
            workspace = (self.config_path.parent / "workspace" / f"user_{self.user_id}").resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _start_container(self, config: Config, workspace: Path) -> Optional[str]:
        manager = ContainerManager(config)
        if not manager.enabled:
            return None
        try:
            container = manager.get_or_create_container(self.user_id, workspace=workspace)
        except Exception as exc:
            logger.warning("Failed to initialize web container isolation: %s", exc)
            return None
        if container:
            logger.info("Web runtime container ready: %s", container.name)
            return container.id
        return None

    def _build_factory(self, config: Config) -> AgentFactory:
        """One factory per system; memory is shared so conversations survive routing."""
        return AgentFactory(
            config=config,
            working_directory=config.get_working_directory(),
            unified_memory=self.unified_memory,
            memory_store=self.memory_store,
            container_id=self.container_id,
            policy_config=self._policy_config,
        )

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
            logger.info("Prepared agent for web runtime: %s/%s", system, agent_key)

    async def warm_default_agent(self) -> None:
        system = self.registry.default_key()
        await self.warm_agent(self.registry.config(system).get_default_agent(), system)

    def pending_action_reviews(self) -> list[dict[str, Any]]:
        """Collect pending reviews from factories that have handled a turn."""
        reviews: list[dict[str, Any]] = []
        for system_key, factory in self.registry._factories.items():
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
        for factory in self.registry._factories.values():
            gate = getattr(factory, "action_gate", None)
            if gate is not None and gate.resolve_review(
                approval_id, approve=approve
            ):
                return True
        return False

    # -- configuration -----------------------------------------------------
    def config_dict(self) -> Dict[str, Any]:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def save_structured_config(self, payload: Dict[str, Any]) -> None:
        from schemas import GridConfig

        GridConfig(**payload)
        self.config_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8"
        )
        self.reload()

    def save_yaml_config(self, yaml_content: str) -> None:
        from schemas import GridConfig

        GridConfig(**(yaml.safe_load(yaml_content) or {}))
        self.config_path.write_text(yaml_content, encoding="utf-8")
        self.reload()

    def reload(self) -> None:
        old_registry = getattr(self, "registry", None)
        if old_registry is not None:
            try:
                asyncio.get_running_loop().create_task(old_registry.close())
            except RuntimeError:
                pass  # no loop (tests, CLI): the client is garbage-collected
        self._build_runtime()

    async def close(self) -> None:
        await self.registry.close()

    # -- conversation metadata --------------------------------------------
    def context_manager(self):
        return self.unified_memory.context_manager

    def conversation_metadata(self, context_id: str) -> Dict[str, Any]:
        with self.context_manager()._lock:
            bucket = self.context_manager()._contexts.get(context_id)
            return copy.deepcopy(bucket.get("metadata") or {}) if bucket else {}

    def update_conversation_metadata(self, context_id: str, **updates: Any) -> None:
        with self.context_manager()._lock:
            bucket = self.context_manager()._contexts.setdefault(
                context_id,
                {"conversation": [], "executions": [], "metadata": {}, "created_at": "", "updated_at": ""},
            )
            bucket.setdefault("metadata", {}).update({k: v for k, v in updates.items() if v is not None})
