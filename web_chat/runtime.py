from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from core.agent_factory import AgentFactory
from core.config import Config
from core.memory_store import MemoryStore
from core.unified_memory import UnifiedMemory
from core.managers.container_manager import ContainerManager

logger = logging.getLogger("grid.web_chat.runtime")


class WebChatRuntime:
    """Single-user web runtime with the same memory/isolation model as agent_chat."""

    def __init__(
        self,
        *,
        config_path: str = "config.yaml",
        user_id: str = "web_user",
        workspace_root: str = "workspace",
        persist_root: str = "data",
    ) -> None:
        self.config_path = Path(config_path)
        self.user_id = user_id
        self.workspace_root = Path(workspace_root)
        self.persist_root = Path(persist_root)

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
        base_config = Config(config_path=str(self.config_path))

        self.workspace_path = (self.workspace_root / f"user_{self.user_id}").resolve()
        self.persist_path = (self.persist_root / f"user_{self.user_id}").resolve()
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        self.persist_path.mkdir(parents=True, exist_ok=True)

        self.container_id = None
        container_manager = ContainerManager(base_config)
        if container_manager.enabled:
            try:
                container = container_manager.get_or_create_container(self.user_id, workspace=self.workspace_path)
                if container:
                    self.container_id = container.id
                    logger.info("Web runtime container ready: %s", container.name)
            except Exception as exc:
                logger.warning("Failed to initialize web container isolation: %s", exc)

        self.config = Config(config_path=str(self.config_path), working_directory=str(self.workspace_path))
        self.unified_memory = UnifiedMemory(
            workspace=self.workspace_path,
            persist_path=self.persist_path,
            max_history=self.config.get_max_history(),
        )
        self.memory_store = MemoryStore(str(self.workspace_path / "memory.db"))
        self.factory = AgentFactory(
            config=self.config,
            working_directory=str(self.workspace_path),
            unified_memory=self.unified_memory,
            memory_store=self.memory_store,
            container_id=self.container_id,
        )
        self._prepared_agents.clear()

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
        # Validate via Config/GridConfig but persist raw structure to preserve extra sections.
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
            bucket = self.context_manager()._contexts.setdefault(context_id, {
                "conversation": [],
                "executions": [],
                "metadata": {},
                "created_at": "",
                "updated_at": "",
            })
            metadata = bucket.setdefault("metadata", {})
            metadata.update({k: v for k, v in updates.items() if v is not None})

