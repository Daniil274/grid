"""Models, their clients and settings, as a system's config describes them.

ModelProvider resolves model keys (the allowed list, aliases), builds the
OpenAI-compatible clients and the Agents SDK models an agent runs on, and
the settings of each call; it knows each agent's context window and which
model summarizes a session. It holds the config and nothing of a run.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agents import ModelSettings
from agents.model_settings import Reasoning
from openai import AsyncOpenAI

from core.model_access import ModelAccess
from core.vision_model import VisionChatCompletionsModel

logger = logging.getLogger("grid.agent_factory")


# Context window assumed for an agent that is not in the config (dynamic agents).
DEFAULT_CONTEXT_WINDOW = 128_000


class ModelProvider:
    """The models of one system's config, ready for agents to run on."""

    def __init__(self, config: Any, runtime_support: Any, compact_config: Any) -> None:
        self.config = config
        self._runtime_support = runtime_support
        self.compact_config = compact_config
        # Responses API warnings already logged, so each is logged once.
        self._responses_warning_keys: set[str] = set()

    def is_allowed(self, model_key: str) -> bool:
        """Whether settings.allowed_models permits *model_key*; no list allows every model."""
        allowed = self.config.config.settings.allowed_models
        return not allowed or model_key in allowed

    def resolve_key(self, key: Optional[str]) -> str:
        """Resolve an input key into a model key using runtime support services."""
        return self._runtime_support.resolve_model_key(key)

    @property
    def access(self) -> Any:
        """The credentials and spend reporting of this config's clients."""
        if self._runtime_support is None:
            return ModelAccess()
        return self._runtime_support.access

    def make_client(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: int = 30,
        max_retries: int = 2,
        provider_key: Optional[str] = None,
    ) -> AsyncOpenAI:
        """Create AsyncOpenAI client; avoid proxy for local providers."""
        return self.access.client(
            self.config,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            provider_key=provider_key,
        )

    def client_for(self, model_key: str) -> tuple[AsyncOpenAI, str]:
        """Create OpenAI client and return (client, model_name) using configuration."""
        return self._runtime_support.get_openai_client_for_model(model_key)

    def is_reasoning_model_name(self, model_name: str) -> bool:
        """Heuristic check for reasoning-style models requiring Responses API."""
        return self._runtime_support.is_reasoning_model_name(model_name)

    def settings(
        self, model_config: Any, parallel_tool_calls: bool = False
    ) -> ModelSettings:
        """Build ModelSettings from model config, applying reasoning overrides if configured.

        ``parallel_tool_calls`` lets the model emit several calls in one response;
        they are still executed one after another by the serial pipeline.

        Config examples:
          reasoning: {effort: "none"}    → SDK-native reasoning_effort (OpenAI)
          reasoning: {enabled: false}    → extra_body {"reasoning": {"enabled": false}} (OpenRouter etc.)
          modalities: [image, text]      → extra_body {"modalities": [...]} (image generation)
        """
        max_tokens = getattr(model_config, "max_tokens", None)
        plan_usage = self._uses_plan(model_config)
        reasoning_cfg: Optional[Dict[str, Any]] = getattr(
            model_config, "reasoning", None
        ) or {}
        sdk_reasoning: Optional[Reasoning] = None
        extra_body: Dict[str, Any] = {}

        effort = reasoning_cfg.get("effort")
        if effort is not None:
            # SDK-native: sent as reasoning_effort=<effort> in the API call
            sdk_reasoning = Reasoning(effort=effort)
        elif reasoning_cfg.get("enabled") is False:
            # Provider-specific: sent via extra_body as {"reasoning": {"enabled": false}}
            extra_body["reasoning"] = {"enabled": False}
        modalities = getattr(model_config, "modalities", None)
        if modalities:
            extra_body["modalities"] = list(modalities)

        return ModelSettings(
            # A ChatGPT plan takes no output cap and never stores the response (preview limits).
            max_tokens=None if plan_usage else max_tokens,
            store=False if plan_usage else None,
            reasoning=sdk_reasoning,
            extra_body=extra_body or None,
            parallel_tool_calls=parallel_tool_calls,
        )

    def _uses_plan(self, model_config: Any) -> bool:
        """Whether the model is served on the user's ChatGPT plan (provider ``auth: chatgpt``)."""
        if self.config is None:
            return False
        try:
            return self.config.get_provider(model_config.provider).auth == "chatgpt"
        except Exception:  # noqa: BLE001 - an unknown provider is reported where the model is built
            return False

    def sdk_model(self, model_key: str) -> tuple[Any, Any]:
        """Create one Agents SDK model and return it with its config."""
        self.access.check_model(self.config, model_key)
        model_config = self.config.get_model(model_key)
        provider_config = self.config.get_provider(model_config.provider)
        api_key = self.access.api_key(self.config, model_config.provider)

        client = self.make_client(
            api_key=api_key,
            base_url=provider_config.base_url,
            timeout=provider_config.timeout,
            max_retries=provider_config.max_retries,
            provider_key=model_config.provider,
        )

        model = None
        use_responses = False
        try:
            use_responses = bool(getattr(model_config, "use_responses_api", False))
        except Exception:
            use_responses = False
        if provider_config.auth == "chatgpt":
            use_responses = True  # a plan serves the Responses API only

        # An explicit use_responses_api is trusted for any provider: some
        # (OpenCode Go muse-spark) serve a model only over /responses.
        if use_responses:
            try:
                from agents import OpenAIResponsesModel  # type: ignore

                model = OpenAIResponsesModel(
                    model=model_config.name, openai_client=client
                )
            except Exception as e:
                warn_key = f"{model_config.provider}|{provider_config.base_url}|{model_config.name}|init_fail"
                if warn_key not in self._responses_warning_keys:
                    self._responses_warning_keys.add(warn_key)
                    logger.warning(
                        "Failed to initialize Responses model: %s",
                        e,
                        extra={
                            "provider": model_config.provider,
                            "base_url": provider_config.base_url,
                            "model": model_config.name,
                        },
                    )
                use_responses = False

        if model is None:
            model = VisionChatCompletionsModel(
                model=model_config.name,
                openai_client=client,
                preserve_reasoning_content=getattr(
                    model_config, "preserve_reasoning_content", False
                ),
            )
        return model, model_config

    def compact_client_and_model(
        self, key: Optional[str]
    ) -> tuple[Optional[AsyncOpenAI], Optional[str]]:
        """Resolve the client/model to use for full compact."""
        compact_cfg = self.compact_config
        summary_model_key = getattr(compact_cfg, "summary_model", None)
        if summary_model_key:
            try:
                return self.client_for(summary_model_key)
            except Exception:
                logger.warning(
                    "Configured compact.summary_model '%s' is unavailable; falling back to resolved model",
                    summary_model_key,
                )
        if key:
            return self.client_for(self.resolve_key(key))
        return None, None

    def context_window(self, agent_key: Optional[str]) -> int:
        """The context window of *agent_key*'s model; a default for unknown agents."""
        try:
            return self.config.get_model(self.config.get_agent(agent_key).primary_model).context_window
        except Exception:
            return DEFAULT_CONTEXT_WINDOW
