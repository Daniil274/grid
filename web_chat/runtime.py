from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from core.agent_factory import AgentFactory
from core.config import Config
from core.memory.store import MemoryStore
from core.memory.unified import UnifiedMemory
from core.managers.container_manager import ContainerManager

logger = logging.getLogger("grid.web_chat.runtime")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolve_config_path(config_path: str) -> Path:
    path = Path(config_path).expanduser()
    if path.is_absolute():
        return path.resolve(strict=False)

    project_path = PROJECT_ROOT / path
    if project_path.exists():
        return project_path.resolve(strict=False)

    cwd_path = path.resolve(strict=False)
    if cwd_path.exists():
        return cwd_path

    return project_path.resolve(strict=False)


def _isolation_enabled(config: Config) -> bool:
    isolation_cfg = getattr(config.config, "isolation", None)
    if not isolation_cfg:
        return False
    if isinstance(isolation_cfg, dict):
        return bool(isolation_cfg.get("enabled", False))
    return bool(getattr(isolation_cfg, "enabled", False))


class WebChatRuntime:
    """Web runtime aligned with agent_chat config/path/session resolution."""

    def __init__(
        self,
        *,
        config_path: str = "config.yaml",
        working_directory: Optional[str] = None,
        user_id: str = "default_user",
    ) -> None:
        self.config_path = _resolve_config_path(config_path)
        self.user_id = user_id
        self.working_directory_override = working_directory

        self._lock = asyncio.Lock()
        self._prepared_agents: set[str] = set()

        self.config: Config
        self.container_id: Optional[str]
        self.workspace_path: Path
        self.persist_path: Path
        self.memory_store: MemoryStore
        self.unified_memory: UnifiedMemory
        self.factory: AgentFactory

        self._build_runtime()

    def _build_runtime(self) -> None:
        config = Config(str(self.config_path), self.working_directory_override)
        container_id: Optional[str] = None

        if _isolation_enabled(config):
            if self.working_directory_override is not None:
                user_workspace = Path(self.working_directory_override).expanduser().resolve()
            else:
                user_workspace = (
                    self.config_path.parent / "workspace" / f"user_{self.user_id}"
                ).resolve()

            user_workspace.mkdir(parents=True, exist_ok=True)

            container_manager = ContainerManager(config)
            if container_manager.enabled:
                try:
                    container = container_manager.get_or_create_container(
                        self.user_id,
                        workspace=user_workspace,
                    )
                    if container:
                        container_id = container.id
                        logger.info("Web runtime container ready: %s", container.name)
                except Exception as exc:
                    logger.warning("Failed to initialize web container isolation: %s", exc)

            config = Config(str(self.config_path), str(user_workspace))

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
            max_history=self.config.get_max_history(),
        )

        data_dir = Path(config.get_absolute_path("data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.memory_store = MemoryStore(
            str(data_dir / "memory.db"),
            config=self.config,
        )

        self.factory = AgentFactory(
            config=self.config,
            working_directory=str(self.workspace_path),
            unified_memory=self.unified_memory,
            memory_store=self.memory_store,
            container_id=self.container_id,
        )
        self._prepared_agents.clear()

        logger.info(
            "Web runtime ready: config=%s workdir=%s sessions=%s",
            self.config_path,
            self.workspace_path,
            logs_dir / "context.json",
        )

    async def warm_agent(self, agent_key: str) -> None:
        async with self._lock:
            if agent_key in self._prepared_agents:
                return
            await self.factory.create_agent(agent_key)
            self._prepared_agents.add(agent_key)
            logger.info("Prepared agent for web runtime: %s", agent_key)

    async def warm_default_agent(self) -> None:
        await self.warm_agent(self.config.config.settings.default_agent)

    def config_dict(self) -> Dict[str, Any]:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        return raw

    def save_structured_config(self, payload: Dict[str, Any]) -> None:
        Config(config_path=str(self.config_path))
        from schemas import GridConfig

        GridConfig(**payload)
        yaml_text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120)
        self.config_path.write_text(yaml_text, encoding="utf-8")
        self.reload()

    def save_yaml_config(self, yaml_content: str) -> None:
        parsed = yaml.safe_load(yaml_content) or {}
        from schemas import GridConfig

        GridConfig(**parsed)
        self.config_path.write_text(yaml_content, encoding="utf-8")
        self.reload()

    def reload(self) -> None:
        self._build_runtime()

    def context_manager(self):
        return self.unified_memory.context_manager

    def conversation_metadata(self, context_id: str) -> Dict[str, Any]:
        with self.context_manager()._lock:
            bucket = self.context_manager()._contexts.get(context_id)
            if not bucket:
                return {}
            return copy.deepcopy(bucket.get("metadata") or {})

    def update_conversation_metadata(self, context_id: str, **updates: Any) -> None:
        with self.context_manager()._lock:
            bucket = self.context_manager()._contexts.setdefault(
                context_id,
                {
                    "conversation": [],
                    "executions": [],
                    "metadata": {},
                    "created_at": "",
                    "updated_at": "",
                },
            )
            metadata = bucket.setdefault("metadata", {})
            metadata.update({k: v for k, v in updates.items() if v is not None})
