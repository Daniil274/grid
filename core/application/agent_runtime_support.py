"""Application-layer support services for agent runtime orchestration."""

from __future__ import annotations

from typing import Callable, Optional

from agents import SQLiteSession

from core.config.config import Config
from core.context import ContextManager
from core.managers.instructions_builder import InstructionsBuilder
from core.managers.model_manager import ModelManager
from core.managers.session_manager import SessionManager


class AgentRuntimeSupport:
    """Compose runtime support services used by AgentFactory."""

    def __init__(
        self,
        *,
        config: Config,
        context_manager: ContextManager,
        container_id: Optional[str] = None,
        session_factory: Optional[Callable[[str], SQLiteSession]] = None,
    ) -> None:
        self.model_manager = ModelManager(config)
        self.instructions_builder = InstructionsBuilder(
            config=config,
            context_manager=context_manager,
            container_id=container_id,
        )
        self.session_manager = SessionManager(session_factory=session_factory)

    @property
    def agent_sessions(self) -> dict[tuple[str, str], SQLiteSession]:
        """Expose cached sessions for compatibility with existing callers/tests."""
        return self.session_manager._agent_sessions

    def resolve_model_key(self, key: Optional[str]) -> str:
        return self.model_manager.resolve_model_key(key)

    def get_openai_client_for_model(self, model_key: str):
        return self.model_manager.get_openai_client_for_model(model_key)

    def is_reasoning_model_name(self, model_name: str) -> bool:
        return self.model_manager.is_reasoning_model_name(model_name)

    def get_agent_session(self, agent_key: str, context_id: str) -> SQLiteSession:
        return self.session_manager.get_agent_session(agent_key, context_id)

    async def cleanup_sessions(self) -> None:
        await self.session_manager.cleanup_sessions()
