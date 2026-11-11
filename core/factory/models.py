"""Models, their clients and settings, as a system's config describes them.

ModelProvider resolves model keys (the allowed list, aliases), builds the
OpenAI-compatible clients and the Agents SDK models an agent runs on, and
the settings of each call; it knows each agent's context window and which
model summarizes a session. It holds the config and nothing of a run.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse
from typing import Any, Dict, FrozenSet, List, Optional

from agents import ModelSettings
from agents.model_settings import Reasoning
from openai import AsyncOpenAI

from core.bounded_model import BoundedModel
from core.compact import get_auto_compact_threshold, max_context_tokens_ceiling
from core.modalities import IMAGE, TEXT_ONLY
from core.model_access import ModelAccess
from core.model_catalog import catalog as model_catalog
from core.responses_model import StreamedOutputResponsesModel
from core.vision_model import VisionChatCompletionsModel

logger = logging.getLogger("grid.agent_factory")


# Context window assumed for an agent that is not in the config (dynamic agents).
DEFAULT_CONTEXT_WINDOW = 128_000

#: Provider auth methods that spend a plan rather than a Grid-held key.
PLAN_AUTH_METHODS = ("chatgpt", "claude")


class ModelProvider:
    """The models of one system's config, ready for agents to run on."""

    def __init__(self, config: Any, runtime_support: Any, compact_config: Any) -> None:
        self.config = config
        self._runtime_support = runtime_support
        self.compact_config = compact_config
        # Responses API warnings already logged, so each is logged once.
        self._responses_warning_keys: set[str] = set()
        # Agents whose compaction threshold was already reported as too low.
        self._low_threshold_warned: set[Optional[str]] = set()

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
          prompt_cache: true             → extra_body {"cache_control": {"type": "ephemeral"}}
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
        if self._caches_prompt(model_config):
            extra_body["cache_control"] = {"type": "ephemeral"}

        return ModelSettings(
            # A ChatGPT plan takes no output cap and never stores the response (preview limits).
            max_tokens=None if plan_usage else max_tokens,
            store=False if plan_usage else None,
            # An unstored reasoning item can be sent back only with its encrypted content.
            response_include=["reasoning.encrypted_content"] if plan_usage else None,
            reasoning=sdk_reasoning,
            extra_body=extra_body or None,
            parallel_tool_calls=parallel_tool_calls,
        )

    def _caches_prompt(self, model_config: Any) -> bool:
        """Only OpenRouter Chat supports this provider-specific wire field.

        The optional prompt_cache field is supplied by the pending schema change;
        neither a model name nor an explicit flag establishes API capability.
        Unknown/custom endpoints fail closed instead of leaking cache_control.
        """
        if self.config is None or self._uses_plan(model_config) or getattr(model_config, "use_responses_api", False):
            return False
        try:
            provider = self.config.get_provider(model_config.provider)
            endpoint = urlparse(provider.base_url)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        if endpoint.scheme != "https" or endpoint.hostname != "openrouter.ai":
            return False
        explicit = getattr(model_config, "prompt_cache", None)
        if explicit is not None:
            return bool(explicit)
        return str(getattr(model_config, "name", "")).startswith("anthropic/")

    def _uses_plan(self, model_config: Any) -> bool:
        """Whether the model is served on a plan login the CLI/provider holds, not Grid's key."""
        if self.config is None:
            return False
        try:
            return getattr(self.config.get_provider(model_config.provider), "auth", "api_key") in PLAN_AUTH_METHODS
        except Exception:  # noqa: BLE001 - an unknown provider is reported where the model is built
            return False

    def input_modalities(self, model_key: str) -> Optional[FrozenSet[str]]:
        """What the model of *model_key* accepts as input; None when that is unknown.

        Its config's ``input_modalities``, else ``vision`` over what the model
        catalog knows (core.model_catalog), else the catalog alone. The Claude
        Code CLI takes text and images (core.claude_model sends them as
        stream-json input), unless the config says ``vision: false``.
        """
        try:
            model_config = self.config.get_model(model_key)
        except Exception:  # noqa: BLE001 - a model the config does not name: nothing is known of it
            return None
        try:
            provider = self.config.get_provider(model_config.provider)
        except Exception:  # noqa: BLE001 - an unknown provider is reported where the model is built
            provider = None
        if getattr(provider, "auth", None) == "claude":
            return TEXT_ONLY if getattr(model_config, "vision", None) is False else TEXT_ONLY | {IMAGE}
        declared = getattr(model_config, "input_modalities", None)
        if declared:
            return frozenset(declared) | TEXT_ONLY
        known = None
        if provider is not None:
            known = model_catalog().lookup(
                getattr(provider, "base_url", "") or "",
                str(model_config.name),
                getattr(provider, "price_source", None),
            )
        vision = getattr(model_config, "vision", None)
        if vision is None:
            return known
        accepted = set(known or TEXT_ONLY)
        if vision:
            accepted.add(IMAGE)
        else:
            accepted.discard(IMAGE)
        return frozenset(accepted)

    def sdk_model(self, model_key: str) -> tuple[Any, Any]:
        """Create one Agents SDK model and return it with its config."""
        self.access.check_model(self.config, model_key)
        model_config = self.config.get_model(model_key)
        provider_config = self.config.get_provider(model_config.provider)
        if getattr(provider_config, "auth", "api_key") == "claude":
            # The Claude Code CLI holds the login; Grid never builds a keyed
            # client for it and never sees a Claude credential. Whether this
            # user may spend that login is still asked, as a key would be.
            self.access.check_cli_login(self.config, model_config.provider)
            return self.claude_model(model_key, model_config, provider_config), model_config
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
                model = StreamedOutputResponsesModel(
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
        # A plan request carries no output cap (settings), so max_tokens is
        # kept on this side; any other provider enforces it itself.
        timeout = getattr(model_config, "response_timeout", None)
        cap = getattr(model_config, "max_tokens", None) if self._uses_plan(model_config) else None
        if timeout or cap:
            model = BoundedModel(model, key=model_key, timeout=timeout, max_output_tokens=cap)
        return model, model_config

    def claude_model(self, model_key: str, model_config: Any, provider_config: Any) -> Any:
        """The Agents SDK model backed by the official Claude Code CLI (provider ``auth: claude``).

        Grid holds no Claude credential here: the CLI's own subscription login
        authorizes every call, and every variable that would bill an API key or
        a cloud account is removed from the child process (core.claude_cli).
        The CLI's own login is re-checked on every request, and its built-in
        tools, MCP servers and host settings are disabled from the command line.
        """
        from core.bounded_model import BoundedModel
        from core.claude_model import ClaudeCodeModel

        runner = self.claude_runner(model_config, provider_config)
        model: Any = ClaudeCodeModel(runner, model_config.name)
        # The CLI enforces no output cap of its own, and one runaway answer
        # would hold a run; bound it the way every plan-backed model is bound.
        timeout = getattr(model_config, "response_timeout", None)
        cap = getattr(model_config, "max_tokens", None)
        if timeout or cap:
            model = BoundedModel(model, key=model_key, timeout=timeout, max_output_tokens=cap)
        return model

    def claude_runner(self, model_config: Any, provider_config: Any) -> Any:
        """The locked-down Claude Code runner for *model_config* (core.claude_cli)."""
        from core.claude_cli import ClaudeCodeRunner

        try:
            working_directory = self.config.get_working_directory()
        except Exception:  # noqa: BLE001 - a config without one runs in the current directory
            working_directory = None
        provider_key = model_config.provider
        return ClaudeCodeRunner(
            binary=getattr(provider_config, "claude_binary", None),
            working_directory=working_directory,
            model=model_config.name,
            # Each run's tokens go to the user's usage, as a metered HTTP call's do.
            on_usage=lambda model, usage: self.access.record_cli_call(self.config, provider_key, model, usage),
        )

    def compact_client_and_model(
        self, key: Optional[str]
    ) -> tuple[Optional[AsyncOpenAI], Optional[str]]:
        """Resolve the client/model to use for full compact."""
        client, model, _ = self.compact_request(key)
        return client, model

    def model_request(self, model_key: str) -> tuple[Any, str, Optional[dict]]:
        """(client, model, Responses API options or None) for a one-off request to *model_key*.

        A Claude Code model has no keyed client: its requests go through the CLI
        (core.claude_model.ClaudeCodeChatClient), over Chat Completions.
        """
        model_config = self.config.get_model(model_key)
        provider_config = self.config.get_provider(model_config.provider)
        if getattr(provider_config, "auth", "api_key") == "claude":
            from core.claude_model import ClaudeCodeChatClient

            self.access.check_model(self.config, model_key)
            self.access.check_cli_login(self.config, model_config.provider)
            runner = self.claude_runner(model_config, provider_config)
            return ClaudeCodeChatClient(runner), model_config.name, None
        client, model = self.client_for(model_key)
        cfg = self.config.get_model(model_key)
        options = {"store": False, "max_output_tokens": None} if self._uses_plan(cfg) else ({} if getattr(cfg, "use_responses_api", False) else None)
        return client, model, options

    def compact_request(self, key: Optional[str]) -> tuple[Any, Optional[str], Optional[dict]]:
        """The first summary-model candidate, with its API options; None when
        none resolves (see :meth:`compact_requests`)."""
        candidates = self.compact_requests(key)
        return candidates[0] if candidates else (None, None, None)

    def compact_requests(self, key: Optional[str]) -> List[tuple[Any, Optional[str], Optional[dict]]]:
        """Every summary-model candidate in fallback order, ready to call.

        ``compact.summary_model`` comes first; the agent's own model chain
        (``model``: one key or a fallback list) follows, so a summary survives
        the failure of the model that would usually write it. Each candidate
        is ``(client, model, Responses API options)``, as
        :meth:`model_request` resolves them; a key that does not resolve is
        logged and skipped, never raised - compaction falls to the next one.
        """
        keys: List[str] = []
        summary_model_key = getattr(self.compact_config, "summary_model", None)
        if summary_model_key:
            keys.append(summary_model_key)
        if key:
            # *key* is an agent key here, not a model key: resolve_key would
            # collapse it to the primary model and lose the fallbacks. When it
            # names no agent it is a model key (an executor's own session) or
            # an alias, and resolving it is exactly what is left to do.
            chain = self._agent_model_keys(str(key))
            keys.extend(chain or [str(self.resolve_key(key))])

        candidates: List[tuple[Any, Optional[str], Optional[dict]]] = []
        seen = set()
        for model_key in keys:
            if model_key in seen:
                continue
            seen.add(model_key)
            try:
                candidates.append(self.model_request(model_key))
            except Exception:
                logger.warning(
                    "Configured compact summary model '%s' is unavailable; trying the next candidate",
                    model_key,
                )
        return candidates

    def _agent_model_keys(self, agent_key: Optional[str]) -> List[str]:
        """The model chain of *agent_key*'s agent, primary model first.

        ``model`` is one key or an ordered fallback list (AgentConfig.model_keys);
        an unknown agent (and so a model key or alias) has no chain of its
        own - an empty list, never an exception.
        """
        if not agent_key:
            return []
        try:
            agent = self.config.get_agent(agent_key)
        except Exception:
            return []
        if hasattr(agent, "model_keys"):
            return [str(key) for key in agent.model_keys()]
        return [str(agent.primary_model)]

    def context_window(self, agent_key: Optional[str]) -> int:
        """The context window *agent_key*'s requests must fit in.

        The narrowest window of the agent's model chain, not the primary
        model's alone: a fallback model with a smaller window would
        otherwise take requests the primary model's window allowed - and
        overflow (a 1M primary with a 258k fallback would compact at 860k).
        A default stands in for an unknown agent.
        """
        windows = []
        for model_key in self._agent_model_keys(agent_key):
            try:
                windows.append(int(self.config.get_model(model_key).context_window))
            except Exception:
                continue
        return min(windows) if windows else DEFAULT_CONTEXT_WINDOW

    def answer_tokens(self, agent_key: Optional[str]) -> int:
        """How much of the window *agent_key*'s model may fill with one answer.

        The largest answer cap of the agent's model chain: whichever model
        answers, the threshold leaves room for it.
        """
        caps = []
        for model_key in self._agent_model_keys(agent_key):
            try:
                cap = int(self.config.get_model(model_key).max_tokens)
            except (TypeError, ValueError, AttributeError):
                continue
            if cap > 0:
                caps.append(cap)
        return max(caps) if caps else 0

    def compact_threshold(self, agent_key: Optional[str], compact_cfg: Any) -> int:
        """Session tokens past which *agent_key*'s context is compacted.

        A threshold under a quarter of the window compacts nearly every turn:
        ``compact.auto.buffer_tokens`` is then too large for this model, and
        that is logged once per agent.
        """
        window = self.context_window(agent_key)
        threshold = get_auto_compact_threshold(window, compact_cfg, self.answer_tokens(agent_key))
        # A threshold at the configured ceiling (compact.auto.max_context_tokens)
        # is policy, however small a share of the window it is: it is not
        # warned about the way a too-large buffer_tokens is.
        if (
            threshold < window // 4
            and threshold < max_context_tokens_ceiling(compact_cfg)
            and agent_key not in self._low_threshold_warned
        ):
            self._low_threshold_warned.add(agent_key)
            logger.warning(
                "Agent %s compacts its context at %d of %d tokens: lower compact.auto.buffer_tokens "
                "(it covers instructions and tool schemas; the model's max_tokens is reserved apart)",
                agent_key,
                threshold,
                window,
            )
        return threshold
