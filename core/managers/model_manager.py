"""
Model Manager for handling model resolution and client creation.
"""

import logging
from typing import Any, Dict, Optional, Tuple
import httpx
from openai import AsyncOpenAI

from core.config import Config
from utils.exceptions import AgentError

logger = logging.getLogger("grid.managers.model")

class ModelManager:
    """
    Manager for resolving models and creating API clients.
    Implements IModelManager protocol.
    """

    def __init__(self, config: Config):
        """
        Initialize ModelManager.
        
        Args:
            config: Configuration instance
        """
        self.config = config

    def resolve_model_key(self, key: Optional[str]) -> str:
        """
        Resolve input key into a model key using configuration.
        - If key is None: use default agent's model
        - If key is a model key: return it
        - If key is an agent key: return that agent's model
        - Otherwise: fallback to default agent's model
        """
        try:
            if not key:
                default_agent_key = self.config.get_default_agent()
                return self.config.get_agent(default_agent_key).model
            # Try as model key
            try:
                _ = self.config.get_model(key)
                return key
            except Exception:
                # Try as agent key
                try:
                    return self.config.get_agent(key).model
                except Exception:
                    # Fallback
                    default_agent_key = self.config.get_default_agent()
                    return self.config.get_agent(default_agent_key).model
        except Exception:
            # Hard fallback
            default_agent_key = self.config.get_default_agent()
            return self.config.get_agent(default_agent_key).model

    def _make_openai_client(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: int = 30,
        max_retries: int = 2,
        provider_key: Optional[str] = None,
    ) -> AsyncOpenAI:
        """Create AsyncOpenAI client; avoid proxy for local providers."""
        kwargs: Dict[str, Any] = dict(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        proxy_url = self.config.get_proxy_for_provider(provider_key)
        # We control proxy selection explicitly; disable env proxy usage in httpx.
        if proxy_url:
            kwargs["http_client"] = httpx.AsyncClient(
                proxy=proxy_url,
                timeout=float(timeout),
                trust_env=False,
            )
        else:
            kwargs["http_client"] = httpx.AsyncClient(
                timeout=float(timeout),
                trust_env=False,
            )
        return AsyncOpenAI(**kwargs)

    def get_openai_client_for_model(self, model_key: str) -> Tuple[AsyncOpenAI, str]:
        """
        Create OpenAI client and return (client, model_name) using configuration.
        """
        model_cfg = self.config.get_model(model_key)
        provider_cfg = self.config.get_provider(model_cfg.provider)
        api_key = self.config.get_api_key(model_cfg.provider)
        if not api_key:
            raise AgentError(
                f"API key not found for provider '{model_cfg.provider}'",
                details={"provider": model_cfg.provider, "env_var": provider_cfg.api_key_env},
            )
        client = self._make_openai_client(
            api_key=api_key,
            base_url=provider_cfg.base_url,
            timeout=provider_cfg.timeout,
            max_retries=provider_cfg.max_retries,
            provider_key=model_cfg.provider,
        )
        return client, model_cfg.name

    def is_reasoning_model_name(self, model_name: str) -> bool:
        """Heuristic check for reasoning-style models requiring Responses API."""
        name = (model_name or "").lower()
        reasoning_markers = [
            "o3",            # OpenAI o3 family
            "o4-mini-high",  # speculative advanced modes
            "r1",            # deepseek-r1 / other r1 models
            "reason",        # contains 'reason' or 'reasoning'
            "thinking",      # thinking-style models
        ]
        return any(marker in name for marker in reasoning_markers)




