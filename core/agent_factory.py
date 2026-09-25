"""
Agent Factory with caching, tracing, and error handling.
"""

import asyncio
import time
import logging
import threading
import sys
import json
from typing import Callable, List, Dict, Any, Optional, Tuple, Union
from dotenv import load_dotenv
import httpx
from openai import AsyncOpenAI
from openai import (
    APIConnectionError,
    APITimeoutError,
    APIStatusError,
    InternalServerError,
    RateLimitError,
)

# OpenAI Agents SDK imports
import agents
from agents import (
    Agent,
    ModelSettings,
    function_tool,
    RunContextWrapper,
    SQLiteSession,
    RunItemStreamEvent,
)
from agents.model_settings import Reasoning
from agents.exceptions import (
    ModelBehaviorError,
    MaxTurnsExceeded,
    UserError as AgentsUserError,
)
from core.vision_model import VisionChatCompletionsModel
from core.managers.mcp_manager import ResilientMCPServerStdio

from core.config.config import Config
from core.action_policy import (
    ActionGate,
    ActionRunState,
    ActionValidator,
    delegated_state,
    is_policy_block,
)
from .context import ContextManager
from schemas import AgentConfig, AgentExecution
from tools import get_tools_by_names
from utils.exceptions import AgentError, ConfigError, ContextError
from utils.logger import Logger
from utils.path_utils import set_current_factory, reset_current_factory
from core.tracing.config import get_tracing_config, ImmediateTraceProcessor
from core.application.agent_runtime_support import AgentRuntimeSupport
from core.fallback_model import AllModelsFailedError, FallbackModel, ModelCandidate
from core import sdk_patches
from core.sdk_patches import tool_error_output
from core.run_stream import (
    StreamObserver,
    append_action_reasoning,
    run_output_text,
    interrupted_run_report,
    tool_event_info,
    ConsoleStreamObserver,
)
from core.agent_input import AgentInput, context_content, parse_agent_input

# Compact system integration
from core.compact import (
    calculate_token_warning_state,
    auto_compact_if_needed,
    AutoCompactTrackingState,
    reactive_compact_on_prompt_too_long,
    is_prompt_too_long_error,
    CompactMessage,
    estimate_messages_tokens,
    to_compact_messages,
    to_context_messages,
)


import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sdk_patches.install()
load_dotenv()
tracing_config = get_tracing_config()

CONTEXT_ID_REGEX = re.compile(r"ctx-[0-9a-fA-F]{8,}")

# Sent after an answer that wrote tool calls as text instead of calling tools.
TOOL_CALL_CORRECTION = """Your last answer wrote tool calls as text, for example:
<tool_call><function=function_name><parameter=parameter_name>value</parameter></function></tool_call>

Text like that runs nothing. Call the tools themselves, then answer.
Repeat the last step that way."""


@dataclass
class _RunProgress:
    """Where a run stands, for the pending-run record kept in the context."""

    input_preview: str
    attempt: int = 0
    recorded_failure: bool = False

logger = logging.getLogger("grid.agent_factory")
verbose_logger = logging.getLogger("grid.verbose")
_TRACING_CONFIGURED = False
_TRACING_CONFIG_LOCK = threading.Lock()


def __getattr__(name: str) -> Any:
    if name == "Runner":
        return getattr(agents, "Runner")
    raise AttributeError(name)


def _get_runner() -> Any:
    """Resolve the current Runner implementation, honoring runtime patches."""
    return getattr(sys.modules[__name__], "Runner")


# Helper class to mock the SDK's ToolContext for auto-run tools
class AutoRunToolContext:
    """Mock context that mimics SDK's ToolContext for direct tool invocation."""

    def __init__(
        self, context: Any, tool_name: str = "", operator_configured: bool = False
    ):
        self.context = context
        self.tool_name = tool_name
        # True only for calls taken verbatim from host configuration
        # (auto_run_tools); the action policy then runs them unjudged.
        self.operator_configured = operator_configured


@dataclass
class GridRunContext:
    """
    Runtime context object passed into Agents SDK Runner.

    It enables `function_tool` implementations to access the live AgentFactory instance
    via `context.context.factory`.

    Also provides access to the current agent's session for local context injection.

    user_id provides workspace isolation for tools that need per-user storage.
    """

    factory: "AgentFactory"
    context_id: Optional[str] = None
    session: Optional[Any] = None  # SQLiteSession for local agent history
    user_id: Optional[str] = None  # User identifier for workspace isolation
    agent_id: Optional[str] = None  # Agent identifier for isolation
    metadata: Optional[dict] = None  # Additional metadata from context manager
    container_id: Optional[str] = None  # Docker container ID for isolation
    pipeline_id: Optional[str] = (
        None  # Shared serial pipeline for nested agent/tool trees
    )
    step_id: Optional[str] = None  # Current serialized execution step
    parent_step_id: Optional[str] = None  # Parent serialized execution step
    execution_mode: Optional[str] = None  # Runtime execution mode (e.g. serial_subtree)
    action_state: Optional[Any] = (
        None  # Trusted task and call chain for the policy gate
    )
    action_depth: int = (
        0  # Context-local delegation depth (safe across parallel branches)
    )
    stream_observer: Optional[Any] = (
        None  # The run's own observer, so sub-agents report into the same view
    )


class AgentFactory:
    """
    Enterprise Agent Factory with advanced features:
    - Configuration validation
    - Agent caching and reuse
    - Context management
    - MCP integration
    - Comprehensive logging and tracing
    - Session-based memory for agents
    """

    MALFORMED_TOOL_CALL_RETRIES = 2

    def __init__(
        self,
        config: Optional[Config] = None,
        working_directory: Optional[str] = None,
        *,
        tracing_level: Optional[str] = "INFO",
        stream_observer: Optional[StreamObserver] = None,
        context_manager: Optional[ContextManager] = None,
        container_id: Optional[str] = None,
        policy_config: Optional[Config] = None,
    ):
        """
        Initialize Agent Factory.

        Args:
            config: Configuration instance (creates default if None)
            working_directory: Working directory override
            tracing_level: Tracing level for debugging
            stream_observer: Observer for agent stream events
            context_manager: Conversation history to share with other factories
                (the web runtime keeps one across systems); a new one otherwise
            container_id: Docker container ID for isolation
            policy_config: Config whose action policy and model registry win over
                this factory's own config (the root routing config when the CLI
                routes a message between systems)
        """
        if tracing_level is not None:
            self._configure_tracing_once(tracing_level)

        # Set up minimal logging for agents SDK to avoid spam
        agents_logger = logging.getLogger("openai.agents")
        agents_logger.setLevel(logging.WARNING)

        self.config = config or Config()
        self.container_id = container_id
        if working_directory:
            self.config.set_working_directory(working_directory)

        # Initialize image processing config
        from utils.image_utils import ImageUtils

        if hasattr(self.config.config, "settings"):
            ImageUtils.set_config(self.config.config.settings.image_processing)

        self.context_manager = context_manager or ContextManager(
            max_history=self.config.get_max_history(),
            persist_path=str(Path(self.config.get_logs_directory()) / "context.json"),
        )

        self._agent_session_db_path = self._build_agent_session_db_path()

        self._runtime_support = AgentRuntimeSupport(
            config=self.config,
            context_manager=self.context_manager,
            container_id=self.container_id,
            session_factory=self._create_persistent_sqlite_session,
        )
        self.instructions_builder = self._runtime_support.instructions_builder

        # Initialize ContainerManager
        from core.managers.container_manager import ContainerManager, CONTAINER_WORKDIR

        self.container_manager = ContainerManager(self.config)
        self._container_workdir = CONTAINER_WORKDIR
        if self.container_id:
            logger.info(
                "AgentFactory initialized with container isolation: %s",
                self.container_id,
            )

        # Caches
        self._agent_cache: Dict[str, Agent] = {}
        self._tool_cache: Dict[str, List[Any]] = {}
        self._mcp_servers: Dict[str, Any] = {}

        # Session management for agent memory (per agent/context pair)
        self._agent_sessions = self._runtime_support.agent_sessions
        # Track emitted warnings to avoid log spam (e.g., Responses API fallbacks)
        self._responses_warning_keys: set[str] = set()
        self._stream_observer: StreamObserver = (
            stream_observer or ConsoleStreamObserver()
        )

        # One policy gate per factory, snapshotted from operator configuration.
        self.action_gate = self._build_action_gate(policy_config)

        # Track logged agents to log prompt only once
        self._logged_agents: set[str] = set()

        # Track initialized agents (auto_run_tools executed) per user
        self._initialized_agents: set[str] = set()

        # Per-session compact tracking state (circuit breaker lives here)
        self._compact_tracking = AutoCompactTrackingState()

        # Initialize pipeline registry for emergency shutdown
        from core.tracing.pipeline_registry import PipelineRegistry

        self._pipeline_registry = PipelineRegistry()
        logger.info("PipelineRegistry initialized")

    def _build_action_gate(
        self, policy_config: Optional[Config]
    ) -> Optional[ActionGate]:
        """Snapshot the policy for this factory. Operator configuration only.

        The routing config wins when it enables a policy, so a routed system is
        mediated by the host's policy and validator model without repeating them
        in every system config. Unknown validator model keys fail here.
        """
        for candidate in (policy_config, self.config):
            if candidate is None:
                continue
            policy = getattr(candidate.config.settings, "action_policy", None)
            if policy is None or policy.mode == "off":
                continue
            gate = ActionGate(policy, validator=ActionValidator.from_config(candidate))
            logger.info(
                "Action policy enabled: mode=%s version=%s kinds=%s chain=%s",
                policy.mode,
                policy.version,
                ",".join(policy.kinds),
                policy.check_chain,
            )
            return gate
        return None

    def _action_state(self, task: str, parent: Any = None) -> Optional[ActionRunState]:
        """Trusted task for a run: one chain and one budget per user task."""
        if self.action_gate is None:
            return None
        if isinstance(parent, ActionRunState):
            return parent
        return ActionRunState(task=task if isinstance(task, str) else str(task))

    @staticmethod
    def _policy_message_text(content: Any) -> str:
        """Extract user-authored text without carrying image payloads."""
        if not isinstance(content, str):
            return str(content)
        stripped = content.strip()
        if not stripped.startswith(("{", "[")):
            return content
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return content
        messages = payload if isinstance(payload, list) else [payload]
        parts: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            body = item.get("content")
            if isinstance(body, str):
                parts.append(body)
            elif isinstance(body, list):
                for part in body:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in {"text", "input_text"}:
                        text = part.get("text")
                        if isinstance(text, str):
                            parts.append(text)
        return "\n".join(parts) or "[multimodal user request]"

    def _policy_task(self, message: Any, context_id: Optional[str]) -> str:
        """Build trusted task context from user messages, newest first on trim."""
        if self.action_gate is None:
            return str(message)
        policy = self.action_gate.config
        prior: list[str] = []
        if context_id:
            snapshot = self.context_manager.get_context_messages(
                context_id,
                limit=policy.max_task_context_messages * 2,
                preview_limit=policy.max_task_context_bytes,
                include_full=True,
            )
            for item in (snapshot or {}).get("messages", []):
                if item.get("role") != "user":
                    continue
                content = item.get("content", item.get("preview", ""))
                text = self._policy_message_text(content).strip()
                if text:
                    prior.append(text)
        current = self._policy_message_text(message).strip()
        if not prior or prior[-1] != current:
            prior.append(current)
        prior = prior[-policy.max_task_context_messages :]
        while prior:
            task = "\n\n".join(
                f"User instruction {index + 1}:\n{text}"
                for index, text in enumerate(prior)
            )
            if len(task.encode("utf-8")) <= policy.max_task_context_bytes:
                return task
            prior.pop(0)
        encoded = current.encode("utf-8")[-policy.max_task_context_bytes :]
        return encoded.decode("utf-8", "ignore")

    def _wrap_tool_with_policy(self, tool: Any, tool_name: str, kind: str) -> Any:
        """Route a call through the policy gate of the factory that runs it.

        Tool objects are shared between agents and factories, so the gate is
        resolved from the run context and the wrapper is applied once.
        """
        if not hasattr(tool, "on_invoke_tool") or getattr(
            tool, "_grid_policy_gated", False
        ):
            return tool
        inner = tool.on_invoke_tool
        descriptor = ActionGate.describe_tool(tool, tool_name, kind)

        async def gated_invoke(ctx, args):
            raw_ctx = getattr(ctx, "context", None)
            factory = getattr(raw_ctx, "factory", None) or self
            gate = getattr(factory, "action_gate", None)
            try:
                if gate is None:
                    return await inner(ctx, args)
                return await gate.invoke(
                    tool_name, kind, ctx, args, inner, descriptor=descriptor
                )
            except Exception as exc:
                # Outermost wrapper: anything raised past the tool's own error
                # handler would make the SDK abort the whole run.
                return tool_error_output(tool_name, exc)

        tool.on_invoke_tool = gated_invoke
        tool._grid_policy_gated = True
        return tool

    def _build_agent_session_db_path(self) -> str:
        """Return durable SQLite path for agent sessions."""
        base_dir = Path(self.config.get_logs_directory())
        base_dir.mkdir(parents=True, exist_ok=True)
        return str(base_dir / "agent_sessions.db")

    def _create_persistent_sqlite_session(self, session_id: str) -> SQLiteSession:
        """Create a file-backed SDK session so history survives process restarts."""
        return SQLiteSession(session_id=session_id, db_path=self._agent_session_db_path)

    @staticmethod
    def _safe_preview(value: Any, max_length: Optional[int] = 500) -> str:
        """Convert arbitrary runtime data to a compact preview string."""
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                text = str(value)
        text = text.strip()
        if max_length is not None and len(text) > max_length:
            return text[:max_length] + "..."
        return text

    def _runtime_event_preview_limit(self) -> Optional[int]:
        """Max length for tool_events in context metadata; None = keep full payloads."""
        settings = getattr(self.config.config.settings, "agent_logging", None)
        if not settings or not getattr(settings, "enabled", True):
            return 700
        level = (getattr(settings, "level", "full") or "full").lower()
        if level in ("full", "detailed"):
            return None
        return 700

    def _logs_directory_path(self) -> Path:
        return Path(self.config.get_logs_directory())

    def _update_pending_agent_run(
        self,
        *,
        agent_key: str,
        active_context_id: Optional[str],
        input_preview: str,
        status: str,
        retry_count: int = 0,
        last_error: Optional[str] = None,
        clear_tool_events: bool = False,
    ) -> Dict[str, Any]:
        """Persist durable state for the currently running agent attempt."""
        existing = self.context_manager.get_metadata("pending_agent_run")
        payload: Dict[str, Any] = existing.copy() if isinstance(existing, dict) else {}
        if clear_tool_events or not isinstance(payload.get("tool_events"), list):
            payload["tool_events"] = []

        payload.update(
            {
                "agent": agent_key,
                "context_id": active_context_id,
                "status": status,
                "retry_count": retry_count,
                "updated_at": datetime.now().isoformat(),
            }
        )
        if input_preview:
            payload["input_preview"] = input_preview
        if last_error is not None:
            payload["last_error"] = last_error
        elif status in {"completed", "running"}:
            payload.pop("last_error", None)

        self.context_manager.set_metadata("pending_agent_run", payload)
        return payload

    def _record_runtime_event(
        self,
        *,
        event_type: str,
        tool_name: Optional[str] = None,
        arguments: Any = None,
        output: Any = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a compact runtime event to durable context metadata."""
        event: Dict[str, Any] = {
            "event_type": event_type,
            "timestamp": datetime.now().isoformat(),
        }
        if tool_name:
            event["tool_name"] = tool_name
        preview_limit = self._runtime_event_preview_limit()
        if arguments is not None:
            event["arguments"] = self._safe_preview(arguments, max_length=preview_limit)
        if output is not None:
            event["output"] = self._safe_preview(output, max_length=preview_limit)
        if extra:
            for key, value in extra.items():
                if value is not None:
                    event[key] = self._safe_preview(
                        value, max_length=preview_limit or 300
                    )
        pending = self.context_manager.get_metadata("pending_agent_run")
        if not isinstance(pending, dict):
            pending = {}
        tool_events = pending.get("tool_events")
        if not isinstance(tool_events, list):
            tool_events = []
        tool_events = [item for item in tool_events if isinstance(item, dict)]
        tool_events.append(event)
        pending["tool_events"] = tool_events[-100:]
        pending["updated_at"] = datetime.now().isoformat()
        # Saved with the next status change, not once per tool call.
        self.context_manager.set_metadata("pending_agent_run", pending, persist=False)

    @staticmethod
    def _message_looks_transient_provider_error(message: str) -> bool:
        text = (message or "").lower()
        transient_markers = (
            "connection error",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "remote protocol error",
            "server disconnected",
            "connection reset",
            "network",
            "rate limit",
            "overloaded",
            "stream closed",
            "incomplete chunked read",
            "all providers exhausted",
            "upstream_unavailable",
            "upstream unavailable",
            "server_error",
            "server error",
            "provider",
            "retry",
            "unavailable",
        )
        return any(marker in text for marker in transient_markers)

    def _is_retriable_agent_exception(self, exc: Exception) -> bool:
        """Decide whether agent execution should be retried indefinitely."""
        if isinstance(exc, AllModelsFailedError):
            return False
        if isinstance(
            exc,
            (
                asyncio.TimeoutError,
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RemoteProtocolError,
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
                RateLimitError,
            ),
        ):
            return True
        if isinstance(exc, APIStatusError):
            status_code = getattr(exc, "status_code", None)
            return status_code in {408, 409, 425, 429, 500, 502, 503, 504}
        if isinstance(exc, AgentError):
            return self._message_looks_transient_provider_error(str(exc))
        if isinstance(exc, Exception):
            return self._message_looks_transient_provider_error(str(exc))
        return False

    @staticmethod
    def _retry_backoff_seconds(retry_count: int) -> float:
        """Backoff that rises quickly but stays bounded for endless retries."""
        schedule = [1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 30.0, 45.0, 60.0]
        if retry_count <= 0:
            return schedule[0]
        return schedule[min(retry_count, len(schedule) - 1)]

    @staticmethod
    def _configure_tracing_once(level: str) -> None:
        global _TRACING_CONFIGURED
        if _TRACING_CONFIGURED:
            return
        with _TRACING_CONFIG_LOCK:
            if _TRACING_CONFIGURED:
                return
            tracing_config.configure_console_tracing(level)
            # Timeline tracer: same DB path as serve_timeline / configure_tracing_from_env
            if os.getenv("GRID_TIMELINE_ENABLED", "true").lower() not in (
                "0",
                "false",
                "no",
            ):
                try:
                    from core.tracing.tracer import get_tracer

                    timeline_exporter = get_tracer()
                    timeline_processor = ImmediateTraceProcessor(
                        timeline_exporter, export_span_start=True
                    )
                    tracing_config._processors.append(timeline_processor)
                except Exception as e:
                    logging.getLogger("grid.tracing").warning(
                        f"Timeline tracer init failed: {e}"
                    )
            tracing_config.apply()
            _TRACING_CONFIGURED = True

    async def initialize(self) -> None:
        """Async init hook for compatibility with API lifespan."""
        return None

    # ---------------------------------------------------------------------
    # Lightweight model resolution helpers for API (e.g., Cline endpoint)
    # ---------------------------------------------------------------------
    def _is_model_allowed(self, model_key: str) -> bool:
        """Whether settings.allowed_models permits *model_key*; no list allows every model."""
        allowed = self.config.config.settings.allowed_models
        return not allowed or model_key in allowed

    def resolve_model_key(self, key: Optional[str]) -> str:
        """Resolve an input key into a model key using runtime support services."""
        return self._runtime_support.resolve_model_key(key)

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
        if provider_key:
            default_headers = self.config.get_provider(provider_key).default_headers
            if default_headers:
                kwargs["default_headers"] = dict(default_headers)
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

    def get_openai_client_for_model(self, model_key: str) -> tuple[AsyncOpenAI, str]:
        """Create OpenAI client and return (client, model_name) using configuration."""
        return self._runtime_support.get_openai_client_for_model(model_key)

    def _get_agent_session(self, agent_key: str, context_id: str) -> SQLiteSession:
        """Get or create a session scoped to an agent/context pair."""
        return self._runtime_support.get_agent_session(agent_key, context_id)

    def _is_reasoning_model_name(self, model_name: str) -> bool:
        """Heuristic check for reasoning-style models requiring Responses API."""
        return self._runtime_support.is_reasoning_model_name(model_name)

    @staticmethod
    def _model_requires_manual_history(model_config: Any) -> bool:
        """Return True when SDK session history is unsafe for the model.

        Some thinking-enabled providers require `reasoning_content` from prior
        assistant tool-call turns to be replayed verbatim on subsequent requests.
        Our manual ContextManager history stores user/assistant-visible messages
        only, which is sufficient for continuity, while SDK SQLiteSession may
        replay internal tool-call turns without the provider-specific reasoning
        payload and trigger 400 errors.
        """
        return bool(getattr(model_config, "preserve_reasoning_content", False))

    def _build_model_settings(
        self, model_config: Any, parallel_tool_calls: bool = False
    ) -> ModelSettings:
        """Build ModelSettings from model config, applying reasoning overrides if configured.

        ``parallel_tool_calls`` lets the model emit several calls in one response;
        they are still executed one after another by the serial pipeline.

        Config examples:
          reasoning: {effort: "none"}    → SDK-native reasoning_effort (OpenAI)
          reasoning: {enabled: false}    → extra_body {"reasoning": {"enabled": false}} (OpenRouter etc.)
        """
        max_tokens = getattr(model_config, "max_tokens", None)
        reasoning_cfg: Optional[Dict[str, Any]] = getattr(
            model_config, "reasoning", None
        )
        if not reasoning_cfg:
            return ModelSettings(
                max_tokens=max_tokens,
                parallel_tool_calls=parallel_tool_calls,
            )

        sdk_reasoning: Optional[Reasoning] = None
        extra_body: Optional[Dict[str, Any]] = None

        effort = reasoning_cfg.get("effort")
        enabled = reasoning_cfg.get("enabled")

        if effort is not None:
            # SDK-native: sent as reasoning_effort=<effort> in the API call
            sdk_reasoning = Reasoning(effort=effort)
        elif enabled is False:
            # Provider-specific: sent via extra_body as {"reasoning": {"enabled": false}}
            extra_body = {"reasoning": {"enabled": False}}

        return ModelSettings(
            max_tokens=max_tokens,
            reasoning=sdk_reasoning,
            extra_body=extra_body,
            parallel_tool_calls=parallel_tool_calls,
        )

    def _create_sdk_model(self, model_key: str) -> tuple[Any, Any]:
        """Create one Agents SDK model and return it with its config."""
        model_config = self.config.get_model(model_key)
        provider_config = self.config.get_provider(model_config.provider)
        api_key = self.config.get_api_key(model_config.provider)
        if not api_key:
            raise AgentError(
                f"API key not found for provider '{model_config.provider}'",
                details={
                    "provider": model_config.provider,
                    "env_var": provider_config.api_key_env,
                },
            )

        client = self._make_openai_client(
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

        base_url_lower = (provider_config.base_url or "").lower()
        provider_supports_responses = "api.openai.com" in base_url_lower
        if use_responses and not provider_supports_responses:
            warn_key = f"{model_config.provider}|{provider_config.base_url}|{model_config.name}|no_support"
            if warn_key not in self._responses_warning_keys:
                self._responses_warning_keys.add(warn_key)
                logger.warning(
                    "Responses API requested for provider without support",
                    extra={
                        "provider": model_config.provider,
                        "base_url": provider_config.base_url,
                        "model": model_config.name,
                    },
                )
            use_responses = False

        if use_responses and provider_supports_responses:
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

    async def create_agent(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        force_reload: bool = False,
    ) -> Agent:
        """
        Create or retrieve cached agent.

        Args:
            agent_key: Agent configuration key
            context_path: Optional context path for agent
            force_reload: Force recreation even if cached

        Returns:
            Configured Agent instance

        Raises:
            AgentError: If agent creation fails
            ConfigError: If configuration is invalid
        """
        # Use agent_key only for caching to ensure consistent sessions
        cache_key = agent_key

        if not force_reload and cache_key in self._agent_cache:
            return self._agent_cache[cache_key]

        try:
            # Get configurations
            agent_config = self.config.get_agent(agent_key)
            model_keys = agent_config.model_keys()
            candidates: list[ModelCandidate] = []
            for model_key in model_keys:
                sdk_model, candidate_config = self._create_sdk_model(model_key)
                candidates.append(
                    ModelCandidate(
                        key=model_key,
                        model=sdk_model,
                        settings=self._build_model_settings(
                            candidate_config, agent_config.parallel_tool_calls
                        ),
                    )
                )
            model_config = self.config.get_model(agent_config.primary_model)
            model = (
                candidates[0].model
                if len(candidates) == 1
                else FallbackModel(candidates)
            )

            # Build instructions with context (include conversation context for agents)
            instructions = self._build_agent_instructions(
                agent_key,
                context_path,
                include_conversation_context=False,
            )

            # Get tools (function and agent tools only; MCP tools handled via mcp_servers)
            tools = await self._get_agent_tools(agent_config, agent_key=agent_key)

            # Prepare MCP servers for this agent (if enabled)
            mcp_server_names: list[str] = []
            for tool_key in agent_config.tools:
                try:
                    tool_cfg = self.config.get_tool(tool_key)
                    if tool_cfg.type == "mcp":
                        mcp_server_names.append(tool_key)
                except ConfigError:
                    continue

            mcp_servers_list: list[Any] = []
            if mcp_server_names and (
                agent_config.mcp_enabled or self.config.is_mcp_enabled()
            ):
                mcp_servers_list = await self._create_mcp_servers(mcp_server_names)

            # Create agent
            agent = Agent(
                name=agent_config.name,
                instructions=instructions,
                model=model,
                model_settings=self._build_model_settings(
                    model_config, agent_config.parallel_tool_calls
                ),
                tools=tools,
                mcp_servers=mcp_servers_list,
            )
            setattr(agent, "_grid_agent_key", agent_key)
            setattr(agent, "_grid_model_key", agent_config.primary_model)
            setattr(agent, "_grid_model_keys", model_keys)
            # Auto-run tools (beads_init, beads_ready, etc.) run only once per user/agent
            # in run_agent() when handling the first request — see _initialized_agents.

            self._agent_cache[cache_key] = agent

            return agent

        except Exception as e:
            error_msg = f"Failed to create agent '{agent_key}': {e}"
            raise AgentError(error_msg, details={"agent_key": agent_key}) from e

    def _substitute_tool_params(
        self, tool_params: dict, working_dir: str, user_message: str = ""
    ) -> dict:
        """Substitute template variables in tool parameters."""
        result = {}
        for k, v in tool_params.items():
            if v == "${working_directory}":
                result[k] = working_dir
            elif v == "${user_message}":
                result[k] = user_message
            else:
                result[k] = v
        return result

    async def _execute_auto_run_tools(
        self,
        agent_key: str,
        agent_config: AgentConfig,
        tools: list,
        working_dir: str,
        run_context: GridRunContext,
        every_run: bool = False,
        user_message: str = "",
    ) -> Tuple[str, bool]:
        """Run auto_run_tools with given working_dir.

        Args:
            every_run: If True, only run tools marked every_run=True.
                       If False, only run one-time tools (every_run not set or False).

        Returns:
            The combined result string to inject, and whether every tool ran:
            a call the action policy blocked is neither injected nor counted
            as done, so one-time tools are tried again on the next run.
        """
        result_parts: List[str] = []
        complete = True
        for auto_tool in agent_config.auto_run_tools or []:
            tool_every_run = bool(auto_tool.get("every_run", False))
            if tool_every_run != every_run:
                continue
            tool_name = auto_tool.get("name")
            tool_params = self._substitute_tool_params(
                dict(auto_tool.get("parameters", {})), working_dir, user_message
            )
            target_tool = next(
                (t for t in tools if getattr(t, "name", "") == tool_name), None
            )
            if target_tool and hasattr(target_tool, "on_invoke_tool"):
                logger.info(
                    f"Auto-running tool '{tool_name}' for agent '{agent_key}' (cwd={working_dir}, every_run={every_run})"
                )
                try:
                    if hasattr(self._stream_observer, "tool_call_started"):
                        self._stream_observer.tool_call_started(
                            agent_key, tool_name, tool_params
                        )
                    tool_ctx_wrapper = AutoRunToolContext(
                        run_context, tool_name=tool_name, operator_configured=True
                    )
                    tool_result = await target_tool.on_invoke_tool(
                        tool_ctx_wrapper, json.dumps(tool_params)
                    )
                    if hasattr(self._stream_observer, "tool_call_finished"):
                        self._stream_observer.tool_call_finished(
                            agent_key, tool_name, tool_result
                        )
                    # Log the full result of the auto-run tool call in verbose mode
                    Logger("agent_factory").log_verbose(
                        f"AUTO-RUN TOOL RESULT: {tool_name}",
                        (
                            tool_result
                            if isinstance(tool_result, str)
                            else str(tool_result)
                        ),
                    )
                    if is_policy_block(tool_result):
                        complete = False
                        logger.warning(
                            f"⚠️ Auto-run tool '{tool_name}' blocked by action policy"
                        )
                        continue
                    result_parts.append(
                        f"\n\n=== AUTO-RUN TOOL RESULT '{tool_name}' ===\n{tool_result}\n"
                    )
                    logger.debug(
                        f"Injected auto-run result of '{tool_name}' into instructions"
                    )
                except Exception as tool_err:
                    logger.warning(
                        f"⚠️ Error in auto-run tool '{tool_name}': {tool_err}"
                    )
        return "".join(result_parts), complete

    async def _execute_init_tools(
        self,
        init_tools: List[Dict[str, Any]],
        tools: list,
        run_context: "GridRunContext",
    ) -> str:
        """
        Run a list of raw tool-spec dicts and return concatenated results.

        Mirrors _execute_auto_run_tools() but accepts plain dicts instead of
        an AgentConfig, making it suitable for dynamic (orchestrated) agents.

        Args:
            init_tools: List of {"name": "tool_name", "parameters": {...}}.
            tools:      Resolved tool objects the agent has access to.
            run_context: GridRunContext used for tool invocation.

        Returns:
            Combined result string ready to be prepended to agent instructions.
        """
        result_parts: List[str] = []
        working_dir = (
            "/"
            if getattr(self, "container_id", None)
            else self.config.get_working_directory()
        )
        for spec in init_tools:
            tool_name = spec.get("name")
            tool_params = self._substitute_tool_params(
                dict(spec.get("parameters", {})), working_dir, ""
            )
            target_tool = next(
                (t for t in tools if getattr(t, "name", "") == tool_name), None
            )
            if target_tool and hasattr(target_tool, "on_invoke_tool"):
                logger.info(f"init_tool: running '{tool_name}' for dynamic agent")
                try:
                    agent_key = (
                        getattr(run_context, "agent_key", None)
                        or getattr(run_context, "agent_name", None)
                        or getattr(run_context, "agent_id", None)
                    )
                    if not agent_key:
                        agent_key = getattr(
                            getattr(run_context, "context", None), "agent_key", None
                        )
                    if not agent_key:
                        agent_key = "dynamic-agent"
                    if hasattr(self._stream_observer, "tool_call_started"):
                        self._stream_observer.tool_call_started(
                            agent_key, tool_name, tool_params
                        )
                    wrapper = AutoRunToolContext(run_context, tool_name=tool_name)
                    result = await target_tool.on_invoke_tool(
                        wrapper, json.dumps(tool_params)
                    )
                    if hasattr(self._stream_observer, "tool_call_finished"):
                        self._stream_observer.tool_call_finished(
                            agent_key, tool_name, result
                        )
                    # Log the full result of the init tool call in verbose mode
                    Logger("agent_factory").log_verbose(
                        f"INIT TOOL RESULT: {tool_name}",
                        result if isinstance(result, str) else str(result),
                    )
                    if is_policy_block(result):
                        # A refusal is not context: injected into the
                        # instructions it only misleads the agent.
                        logger.warning(
                            f"⚠️ init_tool '{tool_name}' blocked by action policy"
                        )
                        continue
                    result_parts.append(
                        f"\n\n=== CONTEXT [{tool_name}] ===\n{result}\n"
                    )
                except Exception as e:
                    logger.warning(f"⚠️ init_tool '{tool_name}' error: {e}")
            else:
                logger.warning(
                    f"⚠️ init_tool '{tool_name}' not found in agent's tool list"
                )
        return "".join(result_parts)

    # ---------------------------------------------------------------------
    # Dynamic agents (not declared in config.yaml)
    # ---------------------------------------------------------------------
    async def create_dynamic_agent(
        self,
        *,
        name: str,
        instructions: str,
        model_key: Optional[str] = None,
        tool_names: Optional[List[str]] = None,
        mcp_tool_names: Optional[List[str]] = None,
        init_tools: Optional[List[Dict[str, Any]]] = None,
        system_skills: Optional[List[str]] = None,
        action_state: Optional[Any] = None,
    ) -> Agent:
        """
        Create an ad-hoc Agent instance not backed by config.yaml.

        This is the core building block for orchestration/meta-agent patterns.
        ``action_state`` is the policy state the agent will run under; its
        ``init_tools`` are judged under it too, since an agent wrote them.
        """
        resolved_model_key = self.resolve_model_key(model_key)

        allowed_models = self.config.config.settings.allowed_models or []

        # Log every dynamic agent creation attempt
        logger.info(
            f"Creating dynamic agent: name={name}, model_key_requested={model_key}, "
            f"model_key_resolved={resolved_model_key}, allowed_models={allowed_models}"
        )

        # CRITICAL: Validate that the model is in the allowed models whitelist
        if not self._is_model_allowed(resolved_model_key):
            error_msg = (
                f"❌ MODEL VALIDATION FAILED ❌\n"
                f"Model '{resolved_model_key}' is not in the allowed models whitelist.\n"
                f"Allowed models: {allowed_models}\n"
                f"Agent requested: {name}\n"
                f"Instructions preview: {instructions[:100]}..."
            )
            logger.error(
                f"❌ BLOCKED: Agent '{name}' tried to use non-whitelisted model '{resolved_model_key}'",
                extra={
                    "model_key_requested": model_key,
                    "model_key_resolved": resolved_model_key,
                    "agent_name": name,
                    "allowed_models": allowed_models,
                    "validation_status": "FAILED",
                },
            )
            raise AgentError(
                error_msg,
                details={
                    "model_key": resolved_model_key,
                    "agent_name": name,
                    "allowed_models": allowed_models,
                },
            )

        # Log successful validation
        logger.info(
            f"✅ MODEL VALIDATION PASSED: Agent '{name}' using model '{resolved_model_key}'"
        )

        client, model_name = self.get_openai_client_for_model(resolved_model_key)
        model_cfg = self.config.get_model(resolved_model_key)
        provider_cfg = self.config.get_provider(model_cfg.provider)

        model = None
        use_responses = bool(getattr(model_cfg, "use_responses_api", False))
        base_url_lower = (provider_cfg.base_url or "").lower()
        provider_supports_responses = "api.openai.com" in base_url_lower
        if use_responses and not provider_supports_responses:
            use_responses = False

        if use_responses and provider_supports_responses:
            try:
                from agents import OpenAIResponsesModel  # type: ignore

                model = OpenAIResponsesModel(model=model_name, openai_client=client)
            except Exception:
                model = None

        if model is None:
            model = VisionChatCompletionsModel(
                model=model_name,
                openai_client=client,
                preserve_reasoning_content=getattr(
                    model_cfg, "preserve_reasoning_content", False
                ),
            )

        tools: List[Any] = []
        mcp_servers_list: List[Any] = []
        effective_tool_names = tool_names or []

        # Gather all tool names (including MCP)
        all_tool_names = list(effective_tool_names)
        if mcp_tool_names:
            all_tool_names.extend(mcp_tool_names)

        if effective_tool_names:
            tools, inferred_mcp = await self._resolve_tools_for_names(
                effective_tool_names
            )
            if inferred_mcp:
                mcp_tool_names = list(
                    dict.fromkeys([*(mcp_tool_names or []), *inferred_mcp])
                )
                # Add inferred_mcp to all_tool_names, removing duplicates
                for mcp_name in inferred_mcp:
                    if mcp_name not in all_tool_names:
                        all_tool_names.append(mcp_name)

        if mcp_tool_names:
            # Only if enabled (globally or per caller)
            if self.config.is_mcp_enabled():
                mcp_servers_list = await self._create_mcp_servers(mcp_tool_names)

        # Add prompt_addition from tool configuration to instructions
        enhanced_instructions = self._build_dynamic_agent_instructions(
            instructions, all_tool_names
        )

        # Load system_skills and prepend to instructions
        if system_skills:
            for skill_name in system_skills:
                skill_content = self._load_system_skill(skill_name)
                if skill_content:
                    enhanced_instructions = f"## System Skill: {skill_name}\n\n{skill_content}\n\n{enhanced_instructions}"

        # Run init_tools and prepend results to instructions
        if init_tools:
            try:
                temp_ctx = GridRunContext(
                    factory=self,
                    context_id=self.get_active_context_id() or "init",
                    user_id=None,
                    agent_id=name,
                    container_id=getattr(self, "container_id", None),
                    action_state=action_state,
                )
                init_info = await self._execute_init_tools(init_tools, tools, temp_ctx)
                if init_info:
                    enhanced_instructions = init_info + "\n\n" + enhanced_instructions
                    logger.info(
                        f"init_tools: injected {len(init_tools)} context result(s) into '{name}'"
                    )
            except Exception as e:
                logger.warning(f"⚠️ init_tools failed for agent '{name}': {e}")

        # Log final tool configuration
        function_tool_names = [getattr(t, "__name__", str(t)) for t in tools]
        logger.info(
            f"Dynamic agent '{name}' created with {len(tools)} function tools and {len(mcp_servers_list)} MCP servers"
        )
        verbose_logger.debug(
            f"\n{'='*80}\nDYNAMIC AGENT CREATED\n{'='*80}\n"
            f"Agent name: {name}\n"
            f"Model: {resolved_model_key}\n"
            f"Requested tool names: {effective_tool_names}\n"
            f"Resolved function tools: {function_tool_names}\n"
            f"MCP tool names: {mcp_tool_names}\n"
            f"MCP servers created: {len(mcp_servers_list)}\n"
            f"{'='*80}\n"
        )

        agent = Agent(
            name=name,
            instructions=enhanced_instructions,
            model=model,
            model_settings=self._build_model_settings(model_cfg),
            tools=tools,
            mcp_servers=mcp_servers_list,
        )
        setattr(agent, "_grid_model_key", resolved_model_key)
        return agent

    # ---------------------------------------------------------------------
    def _load_system_skill(self, skill_name: str) -> Optional[str]:
        """Load a system skill file, delegating to config."""
        return self.config._load_skill_file(skill_name)

    # ---------------------------------------------------------------------
    async def _resolve_tools_for_names(
        self, tool_names: List[str]
    ) -> tuple[List[Any], List[str]]:
        """
        Resolve a mixed list of tool keys (function/agent/mcp from config) into:
        - tools: SDK tool instances (function tools + agent tools)
        - mcp_server_names: MCP tool keys (servers) to attach to agent
        """
        function_tools: List[str] = []
        agent_tools: List[str] = []
        mcp_tools: List[str] = []

        for tool_key in tool_names:
            try:
                tool_cfg = self.config.get_tool(tool_key)
                if tool_cfg.type == "function":
                    function_tools.append(tool_key)
                elif tool_cfg.type == "agent":
                    agent_tools.append(tool_key)
                elif tool_cfg.type == "mcp":
                    mcp_tools.append(tool_key)
            except ConfigError:
                # Tool lists of dynamic agents are written by models: an unknown
                # name is dropped, not fatal, but it must be visible.
                logger.warning("Unknown tool '%s' requested for a dynamic agent; skipped", tool_key)
                continue

        resolved: List[Any] = []
        if function_tools:
            try:
                resolved_ft = get_tools_by_names(function_tools)
                resolved_ft = [
                    self._wrap_tool_with_output_limit(tool, tool_key)
                    for tool, tool_key in zip(resolved_ft, function_tools)
                ]
                resolved.extend(resolved_ft)
            except Exception as exc:
                logger.debug("Failed to resolve function tools: %s", exc, exc_info=exc)

        if agent_tools:
            try:
                resolved.extend(
                    self._wrap_tool_with_policy(t, getattr(t, "name", "agent"), "agent")
                    for t in await self._create_agent_tools(agent_tools)
                )
            except Exception as exc:
                logger.debug("Failed to resolve agent tools: %s", exc, exc_info=exc)

        return resolved, mcp_tools

    def _build_dynamic_agent_instructions(
        self, base_instructions: str, tool_names: List[str]
    ) -> str:
        """
        Build complete instructions for dynamic agent including tool prompt_additions.

        This mirrors the logic from Config.build_agent_prompt but for dynamic agents.
        """
        # Combine parts
        parts = [base_instructions]

        if not tool_names:
            return "\n\n".join(parts)

        # Collect prompt_addition from tool configuration
        tool_descriptions = []
        for tool_name in tool_names:
            try:
                tool_config = self.config.get_tool(tool_name)
                if tool_config.prompt_addition:
                    tool_descriptions.append(tool_config.prompt_addition)
            except Exception:
                # Ignore unknown tools (for compatibility)
                logger.debug(
                    f"Tool '{tool_name}' not found in config, skipping prompt_addition"
                )
                continue

        # If no tool descriptions, return base instructions + memory
        if not tool_descriptions:
            return "\n\n".join(parts)

        # General rules for tools (if set)
        common_rules = getattr(self.config.config.settings, "tools_common_rules", None)
        if common_rules:
            parts.append("\nRules for using tools (general):")
            parts.append(str(common_rules))

        # Add tool descriptions
        parts.append("\nAvailable tools:")
        parts.extend(tool_descriptions)

        return "\n\n".join(parts)

    def _replace_context_with_compact_messages(
        self, messages: List[CompactMessage]
    ) -> None:
        """Persist compacted/truncated history into the active context."""
        self.context_manager.replace_conversation_history(
            to_context_messages(messages)
        )

    def _get_compact_client_and_model(
        self, key: Optional[str]
    ) -> tuple[Optional[AsyncOpenAI], Optional[str]]:
        """Resolve the client/model to use for full compact."""
        compact_cfg = self.config.config.compact
        summary_model_key = getattr(compact_cfg, "summary_model", None)
        if summary_model_key:
            try:
                return self.get_openai_client_for_model(summary_model_key)
            except Exception:
                logger.warning(
                    "Configured compact.summary_model '%s' is unavailable; falling back to resolved model",
                    summary_model_key,
                )
        if key:
            return self.get_openai_client_for_model(self.resolve_model_key(key))
        return None, None

    # ------------------------------------------------------------------
    # Running agents
    # ------------------------------------------------------------------

    async def _consume_stream(
        self,
        result: Any,
        *,
        observer: Any,
        agent_key: str,
        action_state: Any,
        on_event: Optional[Callable[[Any], None]] = None,
    ) -> List[str]:
        """Feed a streamed run to its observer; return the text fragments it rendered.

        Rendering and bookkeeping never stop a run: their failures are logged.
        """
        fragments: List[str] = []
        async for event in result.stream_events():
            append_action_reasoning(action_state, event)
            try:
                if on_event is not None:
                    on_event(event)
                fragment = observer.handle_event(event, agent_key=agent_key)
            except Exception:
                logger.exception("Stream observer failed for %s", agent_key)
                continue
            if fragment:
                fragments.append(fragment)
        return fragments

    @staticmethod
    def _final_text(result: Any, fragments: List[str]) -> str:
        """The answer of a finished run: its final output, else what it streamed."""
        final = getattr(result, "final_output", None)
        if final is not None and str(final).strip():
            return str(final)
        streamed = "".join(fragments).strip()
        return streamed or str(run_output_text(result))

    async def run_agent_object_simple(
        self,
        agent: Any,
        input_message: str,
        context_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        stream_observer: Optional[Any] = None,
        action_state: Optional[Any] = None,
        action_depth: int = 0,
    ) -> str:
        """Run an Agent instance in its own session and return its answer text.

        Used for dynamic and background agents. The run has its own SDK session
        (agent name + context id) and never reads or changes the factory's
        conversation history.

        ``stream_observer`` is the view the run reports into - the caller's, so a
        dynamic agent shows up in the trace of the turn that launched it rather
        than on the server console. Defaults to the factory's own observer.

        ``action_state`` is the policy state of a run started by another agent
        (see ``core.action_policy.delegated_state``). Without it the run is its own
        task: the input message becomes the trusted instruction, which is right
        only for callers that speak for the user (background workers, the CLI).
        """
        observer = stream_observer or self._stream_observer
        agent_label = getattr(agent, "name", None) or "dynamic-agent"
        context_id = context_id or self.context_manager.get_current_context_id()
        session = self._get_agent_session(agent_label, context_id)
        if action_state is None:
            action_state = self._action_state(self._policy_task(input_message, context_id))
            if action_state is not None and hasattr(observer, "handle_policy_event"):
                action_state.policy_event = observer.handle_policy_event
        run_ctx = GridRunContext(
            factory=self,
            context_id=context_id,
            session=session,
            pipeline_id=pipeline_id,
            action_state=action_state,
            action_depth=action_depth,
            # Agents this one delegates to report into the same view.
            stream_observer=stream_observer,
        )

        attempt = 0
        set_current_factory(self)
        try:
            while True:
                result = _get_runner().run_streamed(
                    starting_agent=agent,
                    input=input_message,
                    context=run_ctx,
                    session=session,
                    max_turns=self.config.get_max_turns(),
                )
                try:
                    fragments = await self._consume_stream(
                        result,
                        observer=observer,
                        agent_key=agent_label,
                        action_state=action_state,
                    )
                except (MaxTurnsExceeded, ModelBehaviorError, AgentsUserError) as exc:
                    # Not transient: retrying would repeat the work. The caller
                    # gets what the agent did so far.
                    logger.warning("Agent %s stopped: %s: %s", agent_label, type(exc).__name__, exc)
                    return interrupted_run_report(result, f"Agent {agent_label}", exc)
                except Exception as exc:
                    if not self._is_retriable_agent_exception(exc):
                        raise
                    attempt += 1
                    delay = self._retry_backoff_seconds(attempt)
                    logger.warning(
                        "Retriable failure of agent %s (attempt %d, retry in %.1fs): %s",
                        agent_label,
                        attempt,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                return self._final_text(result, fragments)
        finally:
            reset_current_factory()

    def _open_context(self, context_id: Optional[str], use_active_context: bool) -> str:
        """The conversation this request belongs to: named, active, or new."""
        try:
            if context_id:
                return self.context_manager.activate_context(context_id)
            if use_active_context and self.context_manager.get_current_context_id():
                return self.context_manager.get_current_context_id()
            return self.context_manager.start_new_context()
        except ContextError as exc:
            raise AgentError("Failed to prepare conversation context") from exc

    def _record_invocation(
        self, agent_key: str, context_id: str, user_id: Optional[str], message: str
    ) -> None:
        self.context_manager.set_metadata("context_id", context_id)
        self.context_manager.set_metadata(
            "last_invocation", {"agent": agent_key, "timestamp": time.time()}
        )
        if user_id:
            self.context_manager.set_metadata("user_id", user_id)
        agent_logging = self.config.config.settings.agent_logging
        if agent_logging is None or not agent_logging.enabled:
            return
        Logger.configure_agent_logging(
            enabled=True,
            level=agent_logging.level,
            log_dir=str(self._logs_directory_path()),
        )
        Logger.activate_session_log(context_id)
        if agent_logging.save_conversations and message:
            Logger("agent_factory").log_verbose(f"USER INPUT: {agent_key}", message)

    async def _auto_run_preamble(
        self,
        agent_key: str,
        agent_config: AgentConfig,
        agent: Agent,
        run_ctx: "GridRunContext",
        user_message: str,
    ) -> str:
        """Run the agent's auto_run_tools and return their output for its input.

        One-time tools run once per user and agent; per-message tools on every
        request. A failing tool is logged and left out; the request still runs.
        """
        if not agent_config.auto_run_tools:
            return ""
        working_dir = "/" if self.container_id else self.config.get_working_directory()
        init_key = f"{agent_key}:{run_ctx.user_id or 'default'}"
        parts: List[str] = []
        phases = [(False, init_key not in self._initialized_agents), (True, True)]
        for every_run, due in phases:
            if not due:
                continue
            try:
                info, complete = await self._execute_auto_run_tools(
                    agent_key,
                    agent_config,
                    list(agent.tools or []),
                    working_dir,
                    run_ctx,
                    every_run=every_run,
                    user_message=user_message,
                )
            except Exception:
                logger.warning(
                    "auto_run_tools of %s failed (every_run=%s)", agent_key, every_run, exc_info=True
                )
                continue
            if info:
                parts.append(info)
            if not every_run and complete:
                self._initialized_agents.add(init_key)
        # Per-message output first: it is the most recent state.
        return "\n\n".join(reversed(parts))

    def _prepare_instructions(
        self,
        agent_key: str,
        context_path: Optional[str],
        include_transcript: bool,
        context_id: str,
    ) -> str:
        """Build the agent's instructions and record how they were assembled."""
        instructions = self._build_agent_instructions(
            agent_key, context_path, include_conversation_context=include_transcript
        )
        try:
            assembly = self.instructions_builder.assemble_model_context(
                agent_key,
                context_path,
                include_conversation_context=include_transcript,
                include_path_context=True,
            ).to_debug_payload()
        except Exception:
            logger.debug("Context assembly report failed for %s", agent_key, exc_info=True)
            assembly = {
                "context_id": context_id,
                "history_strategy": "prompt" if include_transcript else "none",
                "instruction_length": len(instructions),
                "sections": [],
            }
        self.context_manager.set_metadata("last_context_assembly", assembly)
        if not self.context_manager.get_metadata("agent_instructions"):
            # Shown by the context inspector.
            self.context_manager.set_metadata("agent_instructions", instructions)
        if agent_key not in self._logged_agents:
            Logger("agent_factory").log_verbose(f"FULL PROMPT STARTUP: {agent_key}", instructions)
            self._logged_agents.add(agent_key)
        return instructions

    def _add_user_message(
        self, message: str, agent_input: AgentInput, agent_key: str, context_id: str
    ) -> None:
        content = (
            message if agent_input.is_text else context_content(agent_input.items[0])
        )
        self.context_manager.add_message(
            "user",
            content,
            metadata={"context_id": context_id, "agent": agent_key, "type": "user_input"},
        )

    async def _compact_conversation_if_needed(
        self, agent_key: str, model_config: Any, session: Optional[SQLiteSession]
    ) -> None:
        """Summarize the conversation before it outgrows the model's context window.

        The summary replaces the stored history and, in session mode, the agent's
        session too, so the model continues from the summary instead of the
        full transcript. A failed compaction is logged; the run goes on.
        """
        messages = to_compact_messages(self.context_manager.conversation_snapshot())
        if not messages:
            return
        context_window = model_config.context_window
        compact_cfg = self.config.config.compact
        tokens = estimate_messages_tokens(messages)
        if not calculate_token_warning_state(
            tokens, context_window, compact_cfg
        ).is_above_auto_compact_threshold:
            return
        logger.info("Auto-compact of %s: %d/%d tokens", agent_key, tokens, context_window)
        try:
            client, model = self._get_compact_client_and_model(agent_key)
            outcome = await auto_compact_if_needed(
                messages=messages,
                context_window=context_window,
                llm_client=client,
                model=model,
                tracking=self._compact_tracking,
                compact_cfg=compact_cfg,
            )
        except Exception:
            logger.warning("Auto-compact of %s failed", agent_key, exc_info=True)
            return
        self._compact_tracking.consecutive_failures = outcome.get(
            "consecutive_failures", self._compact_tracking.consecutive_failures
        )
        result = outcome.get("compaction_result")
        if outcome.get("was_compacted") and result and result.compacted_messages:
            await self._replace_history(result.compacted_messages, session)
            logger.info(
                "Auto-compact of %s: %d -> %d tokens",
                agent_key,
                result.tokens_before,
                result.tokens_after,
            )

    async def _replace_history(
        self, messages: List[CompactMessage], session: Optional[SQLiteSession]
    ) -> None:
        """Make *messages* the conversation the model continues from."""
        self._replace_context_with_compact_messages(messages)
        if session is not None:
            await session.clear_session()
            await session.add_items(self.context_manager.get_conversation_history_as_sdk_messages())

    async def _run_attempt(
        self,
        agent: Agent,
        agent_key: str,
        run_input: Union[str, List[Any]],
        run_ctx: "GridRunContext",
        session: Optional[SQLiteSession],
        *,
        stream: bool,
        observer: Any,
        attempt: int,
    ) -> Tuple[str, Any]:
        """One run of the agent. Returns its answer text and the SDK result.

        A run that exceeds settings.agent_timeout, max_turns, or stops on a model
        or tool error ends with a report of what it did, not a retry: repeating
        it would repeat the work done so far.
        """
        timeout = self.config.get_agent_timeout()
        max_turns = self.config.get_max_turns()
        fragments: List[str] = []
        result: Any = None

        def record_tool_event(event: Any) -> None:
            if not isinstance(event, RunItemStreamEvent):
                return
            if event.name not in ("tool_called", "tool_output"):
                return
            info = tool_event_info(event.item)
            self._record_runtime_event(
                event_type=event.name,
                tool_name=info.get("tool_name") or "tool",
                arguments=info.get("arguments") if event.name == "tool_called" else None,
                output=info.get("output") if event.name == "tool_output" else None,
                extra={"retry_count": attempt},
            )

        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                if stream:
                    if hasattr(observer, "reasoning_text"):
                        observer.reasoning_text = ""
                        observer._reasoning_buf = []
                    result = _get_runner().run_streamed(
                        agent, run_input, context=run_ctx, max_turns=max_turns, session=session
                    )
                    fragments = await self._consume_stream(
                        result,
                        observer=observer,
                        agent_key=agent_key,
                        action_state=run_ctx.action_state,
                        on_event=record_tool_event,
                    )
                else:
                    result = await _get_runner().run(
                        agent, run_input, context=run_ctx, max_turns=max_turns, session=session
                    )
        except TimeoutError:
            if not deadline.expired():
                raise
            logger.warning("Agent %s stopped after %s s", agent_key, timeout)
            stopped = TimeoutError(f"no answer within settings.agent_timeout ({timeout} s)")
            return interrupted_run_report(result, f"Agent {agent_key}", stopped), result
        except (MaxTurnsExceeded, ModelBehaviorError, AgentsUserError) as exc:
            logger.warning("Agent %s stopped: %s: %s", agent_key, type(exc).__name__, exc)
            return interrupted_run_report(result, f"Agent {agent_key}", exc), result
        return self._final_text(result, fragments), result

    async def _run_with_retries(
        self,
        agent: Agent,
        agent_key: str,
        run_input: Union[str, List[Any]],
        run_ctx: "GridRunContext",
        session: Optional[SQLiteSession],
        *,
        stream: bool,
        observer: Any,
        progress: "_RunProgress",
    ) -> Tuple[str, Any]:
        """Run until an answer: transient provider errors are retried with backoff.

        A context overflow is answered once by trimming the oldest history.
        """
        trimmed = False
        set_current_factory(self)
        try:
            while True:
                self._update_pending_agent_run(
                    agent_key=agent_key,
                    active_context_id=run_ctx.context_id,
                    input_preview=progress.input_preview,
                    status="running",
                    retry_count=progress.attempt,
                )
                self._record_runtime_event(
                    event_type="attempt_started", extra={"retry_count": progress.attempt}
                )
                try:
                    return await self._run_attempt(
                        agent,
                        agent_key,
                        run_input,
                        run_ctx,
                        session,
                        stream=stream,
                        observer=observer,
                        attempt=progress.attempt,
                    )
                except Exception as exc:
                    if not trimmed and is_prompt_too_long_error(exc):
                        trimmed = True
                        if await self._trim_history_after_overflow(exc, session):
                            if session is None and isinstance(run_input, list):
                                run_input = self._manual_run_input(run_input[-1:])
                            continue
                    retriable = self._is_retriable_agent_exception(exc)
                    error_text = self._safe_preview(str(exc), max_length=700)
                    self._update_pending_agent_run(
                        agent_key=agent_key,
                        active_context_id=run_ctx.context_id,
                        input_preview=progress.input_preview,
                        status="retrying" if retriable else "failed",
                        retry_count=progress.attempt,
                        last_error=error_text,
                    )
                    self._record_runtime_event(
                        event_type="attempt_error",
                        output=error_text,
                        extra={
                            "retry_count": progress.attempt,
                            "retriable": retriable,
                            "exception_type": type(exc).__name__,
                        },
                    )
                    if not retriable:
                        progress.recorded_failure = True
                        raise AgentError(f"Agent execution failed: {exc}") from exc
                    progress.attempt += 1
                    delay = self._retry_backoff_seconds(progress.attempt)
                    logger.warning(
                        "Retriable failure of agent %s (attempt %d, retry in %.1fs): %s",
                        agent_key,
                        progress.attempt,
                        delay,
                        exc,
                        exc_info=exc,
                    )
                    await asyncio.sleep(delay)
        finally:
            reset_current_factory()

    async def _trim_history_after_overflow(
        self, error: Exception, session: Optional[SQLiteSession]
    ) -> bool:
        """Drop the oldest history after a context overflow; True if anything was dropped."""
        messages = to_compact_messages(self.context_manager.conversation_snapshot())
        try:
            trimmed = await reactive_compact_on_prompt_too_long(messages=messages, error=error)
        except Exception:
            logger.warning("Trimming history after a context overflow failed", exc_info=True)
            return False
        if trimmed.status.value not in ("trimmed", "success"):
            return False
        await self._replace_history(trimmed.messages, session)
        logger.info(
            "Context overflow: history trimmed to %d messages (%d -> %d tokens), retrying",
            len(trimmed.messages),
            trimmed.tokens_before,
            trimmed.tokens_after,
        )
        return True

    def _manual_run_input(self, current: List[Any]) -> List[Any]:
        """Stored history followed by the current input: the manual-history run input."""
        history = self.context_manager.get_conversation_history_as_sdk_messages()
        # The current message is the last stored one; send it once.
        return history[:-1] + current if history else current

    async def run_agent(
        self,
        agent_key: str,
        message: str,
        context_path: Optional[str] = None,
        context_id: Optional[str] = None,
        *,
        stream: bool = False,
        use_active_context: bool = False,
        user_id: Optional[str] = None,
        stream_observer: Optional[Any] = None,
    ) -> str:
        """Run an agent on a message within a conversation and return its answer.

        Args:
            agent_key: Agent to run
            message: The user's message: text, or a JSON SDK message with images
            context_path: Optional context path
            context_id: Conversation to continue; a new one when omitted
            stream: Stream events to the observer while the agent runs
            use_active_context: Continue the active conversation when no id is given
            user_id: User the run acts for (workspace isolation)
            stream_observer: View the run reports into; the factory's by default

        The answer ends with the line ``Context ID: <id>``. An answer written as
        text tool calls (``<tool_call><function=...>``) is retried with a
        correction, up to MALFORMED_TOOL_CALL_RETRIES times.
        """
        output, context_id = await self._run_turn(
            agent_key,
            message,
            context_path,
            context_id,
            stream=stream,
            use_active_context=use_active_context,
            user_id=user_id,
            stream_observer=stream_observer,
        )
        for retry in range(1, self.MALFORMED_TOOL_CALL_RETRIES + 1):
            if not self._detect_malformed_tool_calls(output):
                break
            logger.warning(
                "Agent %s wrote tool calls as text; retrying with a correction (%d/%d)",
                agent_key,
                retry,
                self.MALFORMED_TOOL_CALL_RETRIES,
            )
            output, context_id = await self._run_turn(
                agent_key,
                TOOL_CALL_CORRECTION,
                context_path,
                context_id,
                stream=stream,
                user_id=user_id,
                stream_observer=stream_observer,
            )
        return output

    async def _run_turn(
        self,
        agent_key: str,
        message: str,
        context_path: Optional[str],
        context_id: Optional[str],
        *,
        stream: bool,
        user_id: Optional[str],
        stream_observer: Optional[Any],
        use_active_context: bool = False,
    ) -> Tuple[str, str]:
        """One request and answer of the conversation. Returns (answer, context id)."""
        observer = stream_observer or self._stream_observer
        execution = AgentExecution(
            agent_name=agent_key, start_time=time.time(), input_message=message
        )
        progress = _RunProgress(input_preview=self._safe_preview(message, max_length=700))
        active_context_id: Optional[str] = None
        try:
            active_context_id = self._open_context(context_id, use_active_context)
            execution.context_id = active_context_id
            continuing = context_id is not None or use_active_context
            # The trusted task includes the bounded user-authored conversation,
            # not only a context-free follow-up such as "commit" or "continue".
            action_state = self._action_state(self._policy_task(message, active_context_id))
            if action_state is not None and hasattr(observer, "handle_policy_event"):
                action_state.policy_event = observer.handle_policy_event
            self._record_invocation(agent_key, active_context_id, user_id, message)
            user_id = user_id or self.context_manager.get_metadata("user_id")

            agent = await self.create_agent(agent_key, context_path)
            agent_config = self.config.get_agent(agent_key)
            model_config = self.config.get_model(agent_config.primary_model)
            agent_input = parse_agent_input(message)
            run_input: Union[str, List[Any]] = agent_input.items

            run_ctx = GridRunContext(
                factory=self,
                context_id=active_context_id,
                user_id=user_id,
                agent_id=agent_key,
                container_id=self.container_id,
                action_state=action_state,
                stream_observer=observer,
            )
            preamble = await self._auto_run_preamble(
                agent_key,
                agent_config,
                agent,
                run_ctx,
                user_message=message if agent_input.is_text else "",
            )
            if preamble and agent_input.is_text:
                run_input = f"{preamble}\n\n[Current user request]\n\n{run_input}"

            # History reaches the model through one channel: the agent's SDK
            # session, or - for images and models that need their reasoning
            # replayed - the stored history sent with the input.
            manual_history = (
                not agent_input.is_text
                or self.context_manager.history_has_images()
                or self._model_requires_manual_history(model_config)
            )
            session = None if manual_history else self._get_agent_session(agent_key, active_context_id)
            if continuing:
                await self._compact_conversation_if_needed(agent_key, model_config, session)
            # A transcript in the prompt only for an agent that joins a
            # conversation its session has not seen (e.g. after routing).
            include_transcript = (
                continuing and session is not None and not await session.get_items(limit=1)
            )
            agent.instructions = self._prepare_instructions(
                agent_key, context_path, include_transcript, active_context_id
            )
            self._add_user_message(message, agent_input, agent_key, active_context_id)
            if manual_history:
                current = [{"role": "user", "content": run_input}] if isinstance(run_input, str) else run_input
                run_input = self._manual_run_input(current)

            run_ctx.session = session
            run_ctx.metadata = self.context_manager.get_all_metadata()
            progress.input_preview = self._safe_preview(run_input, max_length=700)
            self._update_pending_agent_run(
                agent_key=agent_key,
                active_context_id=active_context_id,
                input_preview=progress.input_preview,
                status="running",
                clear_tool_events=True,
            )
            output, result = await self._run_with_retries(
                agent,
                agent_key,
                run_input,
                run_ctx,
                session,
                stream=stream,
                observer=observer,
                progress=progress,
            )

            marker = f"Context ID: {active_context_id}"
            if marker not in output:
                output = f"{output.rstrip()}\n\n{marker}"
            self.context_manager.add_message(
                "assistant",
                output,
                metadata={"context_id": active_context_id, "agent": agent_key, "type": "agent_response"},
            )
            execution.end_time = time.time()
            execution.output = output
            execution.tools_used = [
                name
                for item in getattr(result, "new_items", None) or []
                if getattr(item, "type", "") == "tool_call_item"
                and (name := tool_event_info(item).get("tool_name"))
            ]
            self.context_manager.add_execution(execution)
            self._record_runtime_event(
                event_type="attempt_completed",
                output=output,
                extra={"retry_count": progress.attempt},
            )
            self._update_pending_agent_run(
                agent_key=agent_key,
                active_context_id=active_context_id,
                input_preview=progress.input_preview,
                status="completed",
                retry_count=progress.attempt,
            )
            Logger("agent_factory").log_verbose(f"FULL RESPONSE: {agent_key}", output)
            return output, active_context_id
        except Exception as exc:
            execution.end_time = time.time()
            execution.error = str(exc)
            if not progress.recorded_failure:
                try:
                    self._update_pending_agent_run(
                        agent_key=agent_key,
                        active_context_id=active_context_id,
                        input_preview=progress.input_preview,
                        status="failed",
                        retry_count=progress.attempt,
                        last_error=self._safe_preview(str(exc), max_length=700),
                    )
                    self._record_runtime_event(
                        event_type="execution_failed",
                        output=str(exc),
                        extra={"exception_type": type(exc).__name__},
                    )
                except Exception:
                    logger.debug("Failed to persist pending run failure state", exc_info=True)
            self.context_manager.add_execution(execution)
            raise
        finally:
            Logger.deactivate_session_log()

    def _build_agent_instructions(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        include_conversation_context: bool = True,
    ) -> str:
        """Build complete agent instructions with context."""
        base_instructions = self.config.build_agent_prompt(agent_key)

        # Add path context
        path_context = self._build_path_context(context_path)

        # Combine all parts
        parts = [base_instructions]

        if path_context:
            parts.append(path_context)
        # Add current session context only if explicitly requested
        if include_conversation_context:
            conversation_context = self.context_manager.get_conversation_context()
            if conversation_context:
                parts.append(conversation_context)

        return "\n\n".join(parts)

    def _build_path_context(self, context_path: Optional[str] = None) -> str:
        """Build path context information."""
        # In container mode the agent sees "/" as its root.
        working_dir = "/" if self.container_id else self.config.get_working_directory()
        config_dir = self.config.get_config_directory()

        context_parts = [
            "Path information:",
            f"Working directory: {working_dir}",
        ]

        # Only add config dir if not in container (or if we decide to map it later)
        if not self.container_id:
            context_parts.append(f"Configuration directory: {config_dir}")

        if context_path:
            # If in container, we try to make the path relative to the workspace
            if self.container_id:
                # If it's already relative, keep it. If absolute host path, try to convert.
                if os.path.isabs(context_path):
                    host_wd = self.config.get_working_directory()
                    if context_path.startswith(host_wd):
                        rel_path = os.path.relpath(context_path, host_wd)
                        absolute_path = (Path("/") / rel_path).as_posix()
                        context_path = rel_path
                    else:
                        # Outside host workspace — use relative form to avoid leaking container paths
                        absolute_path = context_path.lstrip("/") or "/"
                        context_path = absolute_path
                else:
                    absolute_path = (Path("/") / context_path).as_posix()
            else:
                absolute_path = self.config.get_absolute_path(context_path)

            context_parts.extend(
                [
                    f"Context path: {context_path}",
                    f"Absolute context path: {absolute_path}",
                ]
            )

        context_parts.extend(
            ["", "Use these paths for working with files and directories."]
        )

        return "\n".join(context_parts)

    def _legacy_build_agent_instructions(
        self,
        agent_key: str,
        context_path: Optional[str] = None,
        include_conversation_context: bool = True,
    ) -> str:
        """Build complete agent instructions with context."""
        return self.instructions_builder.build_agent_instructions(
            agent_key,
            context_path,
            include_conversation_context=include_conversation_context,
        )

    def _legacy_build_path_context(self, context_path: Optional[str] = None) -> str:
        """Build path context information."""
        return self.instructions_builder.build_path_context(context_path)

    # ------------------------------------------------------------------
    # SDK exception helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Tool output truncation
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimates the number of tokens in a string (~4 chars = 1 token)."""
        return max(1, len(text) // 4)

    def _truncate_tool_output(self, output: Any, tool_name: str = "") -> Any:
        """Check tool output for limit violations.

        2. max_tool_output — truncates the string with a warning when exceeded.
        """
        settings = self.config.config.settings
        output_str = str(output) if not isinstance(output, str) else output

        # Token check — return error without truncation
        max_tokens = getattr(settings, "max_tool_output_tokens", None)
        if max_tokens is not None and isinstance(output, str):
            estimated = self._estimate_tokens(output_str)
            if estimated > max_tokens:
                logger.warning(
                    "Tool output rejected (token limit): %s (~%d tokens > %d limit)",
                    tool_name,
                    estimated,
                    max_tokens,
                )
                return (
                    f"ERROR: Tool output is too large (~{estimated} tokens, limit {max_tokens} tokens). "
                    f"The result of '{tool_name}' was not passed to avoid context overflow. "
                    f"Use more specific parameters or split the request into smaller parts."
                )

        # Character check — truncate with warning
        max_chars = getattr(settings, "max_tool_output", None)
        if (
            max_chars is not None
            and isinstance(output, str)
            and len(output) > max_chars
        ):
            original_length = len(output)
            truncated = output[:max_chars]
            truncated += (
                f"\n\n⚠️ Tool output truncated "
                f"(showing {max_chars} of {original_length} characters)"
            )
            logger.info(
                "Tool output truncated: %s (%d -> %d chars)",
                tool_name,
                original_length,
                max_chars,
            )
            return truncated

        return output

    async def _ensure_pipeline_for_run_context(
        self,
        run_context_obj: Any,
        orchestrator_name: str,
    ) -> tuple[Any, str, str]:
        """Attach the current execution tree to a shared serial pipeline."""
        from core.tracing.pipeline_registry import PipelineRegistry

        raw_ctx = getattr(run_context_obj, "context", run_context_obj)
        if raw_ctx is None:
            raise ValueError("Run context is missing")

        active_context_id = (
            getattr(raw_ctx, "context_id", None)
            or self.get_active_context_id()
            or self.context_manager.start_new_context()
        )
        user_id = getattr(raw_ctx, "user_id", None)
        pipeline_id = getattr(raw_ctx, "pipeline_id", None)

        registry = PipelineRegistry()
        if not pipeline_id:
            pipeline_id = await registry.get_or_create_pipeline(
                orchestrator_name=orchestrator_name,
                context_id=active_context_id,
                user_id=user_id,
            )

        if (
            hasattr(raw_ctx, "context_id")
            and getattr(raw_ctx, "context_id", None) is None
        ):
            raw_ctx.context_id = active_context_id
        if hasattr(raw_ctx, "pipeline_id"):
            raw_ctx.pipeline_id = pipeline_id
        if hasattr(raw_ctx, "step_id"):
            raw_ctx.step_id = registry.get_current_step_id(pipeline_id)
        if (
            hasattr(raw_ctx, "execution_mode")
            and getattr(raw_ctx, "execution_mode", None) is None
        ):
            raw_ctx.execution_mode = "serial_subtree"

        return registry, pipeline_id, active_context_id

    def _wrap_tool_with_output_limit(
        self, tool: Any, tool_key: Optional[str] = None
    ) -> Any:
        """Wraps FunctionTool with pipeline serialization and output limits."""
        settings = self.config.config.settings
        max_tokens = getattr(settings, "max_tool_output_tokens", None)
        max_chars = getattr(settings, "max_tool_output", None)
        if not hasattr(tool, "on_invoke_tool"):
            return tool

        original_invoke = tool.on_invoke_tool
        factory_ref = self
        tool_name = (
            tool_key or getattr(tool, "name", "") or getattr(tool, "__name__", "tool")
        )

        async def invoke_original(ctx, args):
            result = await original_invoke(ctx, args)
            if max_tokens is None and max_chars is None:
                return result
            return factory_ref._truncate_tool_output(
                result, getattr(tool, "name", "") or tool_name
            )

        async def limited_invoke(ctx, args):
            registry, pipeline_id, active_context_id = (
                await factory_ref._ensure_pipeline_for_run_context(
                    ctx,
                    f"tool:{tool_name}",
                )
            )
            raw_ctx = getattr(ctx, "context", None)
            if raw_ctx is not None:
                raw_ctx.context_id = active_context_id
                raw_ctx.pipeline_id = pipeline_id
                raw_ctx.execution_mode = "serial_subtree"

            async def execute_step():
                return await invoke_original(ctx, args)

            return await registry.run_serialized_step(
                pipeline_id=pipeline_id,
                agent_name=f"tool:{tool_name}",
                step_coro_factory=execute_step,
                metadata={
                    "kind": "function_tool",
                    "tool_name": tool_name,
                },
            )

        tool.on_invoke_tool = limited_invoke
        return self._wrap_tool_with_policy(tool, tool_name, "function")

    async def _get_agent_tools(
        self, agent_config: AgentConfig, agent_key: Optional[str] = None
    ) -> List[Any]:
        """Get all tools for agent with caching."""
        cache_key = f"{agent_config.name}:{hash(tuple(agent_config.tools))}"

        if cache_key in self._tool_cache:
            return self._tool_cache[cache_key]

        tools = []

        # Categorize tools
        function_tools = []
        mcp_tools = []
        agent_tools = []

        for tool_name in agent_config.tools:
            try:
                tool_config = self.config.get_tool(tool_name)

                if tool_config.type == "function":
                    function_tools.append(tool_name)
                elif tool_config.type == "mcp":
                    mcp_tools.append(tool_name)
                elif tool_config.type == "agent":
                    agent_tools.append(tool_name)

            except ConfigError as exc:
                logger.warning(
                    "Tool configuration missing for agent",
                    extra={"agent": agent_config.name, "tool": tool_name},
                    exc_info=exc,
                )

        # Add function tools
        if function_tools:
            try:
                func_tools = get_tools_by_names(function_tools)
                func_tools = [
                    self._wrap_tool_with_output_limit(tool, tool_key)
                    for tool, tool_key in zip(func_tools, function_tools)
                ]
                tools.extend(func_tools)

            except Exception as e:
                logger.error(
                    "Failed to load function tools %s: %s",
                    function_tools,
                    e,
                    exc_info=e,
                )

        # Add agent tools
        if agent_tools:
            try:
                agent_tool_instances = await self._create_agent_tools(
                    agent_tools, current_agent_key=agent_key
                )
                tools.extend(
                    self._wrap_tool_with_policy(t, getattr(t, "name", "agent"), "agent")
                    for t in agent_tool_instances
                )

            except Exception as e:
                logger.error(
                    "Failed to create agent tools %s: %s", agent_tools, e, exc_info=e
                )

        # Cache tools
        self._tool_cache[cache_key] = tools

        return tools

    async def _create_agent_tools(
        self, agent_keys: List[str], current_agent_key: Optional[str] = None
    ) -> List[Any]:
        """Create agent tools with proper logging and context sharing."""
        tools = []

        for agent_tool_key in agent_keys:
            try:
                # Get tool configuration
                tool_config = self.config.get_tool(agent_tool_key)
                target_agent_key = (
                    getattr(tool_config, "target_agent", None) or agent_tool_key
                )
                target_agent_config = self.config.get_agent(target_agent_key)
                tool_name = tool_config.name or f"call_{target_agent_key}"

                # Self-referential coordinator tools must be lazy. Creating the
                # target eagerly while the parent agent is still being assembled
                # recurses through _get_agent_tools indefinitely.
                sub_agent = (
                    None
                    if target_agent_key == current_agent_key
                    else await self.create_agent(target_agent_key)
                )
                target_agent_name = (
                    getattr(sub_agent, "name", None) or target_agent_config.name
                )
                tool_description = (
                    tool_config.description or f"Calls {target_agent_name}"
                )

                # Get context sharing parameters from tool config
                context_strategy = getattr(
                    tool_config, "context_strategy", "conversation"
                )
                context_depth = getattr(tool_config, "context_depth", 5)
                include_tool_history = getattr(
                    tool_config, "include_tool_history", True
                )

                # Create context-aware tool (primary name)
                main_tool = self._create_context_aware_agent_tool(
                    agent_key=target_agent_key,
                    sub_agent=sub_agent,
                    tool_name=tool_name,
                    tool_description=tool_description,
                    context_strategy=context_strategy,
                    context_depth=context_depth,
                    include_tool_history=include_tool_history,
                )

                # Wrap for logging
                # Channel suffixes the model glues onto the name
                # ("<name>_commentary") are resolved by sdk_patches.resolve_model_tool_name.
                wrapped_main = self._wrap_agent_tool(main_tool, target_agent_name)
                tools.append(wrapped_main)

            except Exception as e:
                logger.error(
                    "Failed to configure agent tool",
                    extra={"agent_tool": agent_tool_key},
                    exc_info=e,
                )

        return tools

    def _wrap_agent_tool(self, agent_tool: Any, agent_name: str) -> Any:
        """Wrap agent tool for proper logging and execution tracking."""
        if not hasattr(agent_tool, "on_invoke_tool"):
            return agent_tool

        original_invoke = agent_tool.on_invoke_tool

        async def wrapped_invoke_tool(tool_context, tool_call_arguments):
            start_time = time.time()
            # Normalize and log tool arguments
            # The SDK passes the model's arguments as a JSON string; map every
            # allowed alias onto the single required 'input' field.
            arguments = tool_call_arguments
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments) if arguments.strip() else {}
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    arguments = parsed
            preferred_text: Optional[str] = None
            if isinstance(arguments, dict):
                # Prioritize text aliases over input to avoid losing the task
                for alias in ("task", "message", "prompt", "input"):
                    value = arguments.get(alias)
                    if isinstance(value, str) and value.strip():
                        preferred_text = value.strip()
                        break
                # If null/None or empty strings — replace with empty string
                if not isinstance(preferred_text, str):
                    preferred_text = ""
            else:
                # Not a JSON object (plain text or malformed JSON): the text is the task
                preferred_text = str(arguments) if arguments is not None else ""
            normalized_args = {"input": preferred_text}

            # Safely convert arguments to string for logging
            requested_context_id = self._extract_context_id_from_text(preferred_text)
            sub_context_id = requested_context_id or f"ctx-{uuid.uuid4().hex[:8]}"

            # Safely convert arguments to string for logging
            input_data = str(normalized_args)

            execution = AgentExecution(
                agent_name=agent_name, start_time=start_time, input_message=input_data
            )
            execution.context_id = sub_context_id

            try:
                # Log tool call with a nice name
                tool_display_name = getattr(agent_tool, "name", agent_name)
                # Add prefix for agent-tools
                formatted_tool_name = f"Agent-Tool: {tool_display_name}"

                Logger("agent_factory").log_verbose(
                    f"TOOL CALL: {tool_display_name}", normalized_args
                )

                registry, pipeline_id, active_context_id = (
                    await self._ensure_pipeline_for_run_context(
                        tool_context,
                        formatted_tool_name,
                    )
                )
                raw_ctx = getattr(tool_context, "context", None)
                if raw_ctx is not None:
                    raw_ctx.context_id = active_context_id
                    raw_ctx.pipeline_id = pipeline_id
                    raw_ctx.execution_mode = "serial_subtree"

                async def execute_original():
                    result = original_invoke(
                        tool_context, json.dumps(normalized_args, ensure_ascii=False)
                    )
                    if hasattr(result, "__await__"):
                        return await result
                    return result

                result = await registry.run_serialized_step(
                    pipeline_id=pipeline_id,
                    agent_name=formatted_tool_name,
                    step_coro_factory=execute_original,
                    metadata={
                        "kind": "agent_tool",
                        "tool_name": tool_display_name,
                        "target_agent": agent_name,
                    },
                )

                # ✅ CONTEXT ISOLATION: DO NOT inject multimodal output into the global context!
                # Each agent works with its own isolated context.
                # The parent agent should only see the text result (final_output).
                # Multimodal data (images) stays inside the sub-agent and does NOT leak upward.

                execution.end_time = time.time()

                # Prepare text representation for logs/history
                if isinstance(result, str):
                    result_text = result
                elif isinstance(result, list) and result:
                    # For object list (multimodal output), create a summary
                    result_text = f"[Multimodal Tool Output: {len(result)} items]"
                    # If all elements have a text representation, use it
                    if all(isinstance(x, str) for x in result):
                        result_text = str(result)
                else:
                    result_text = str(result)

                # The sub-agent reports the session it actually ran in.
                reported = re.findall(r"Context ID: (ctx-[0-9a-fA-F]{8,})", result_text)
                if reported:
                    execution.context_id = reported[-1]
                    execution.output = result_text
                else:
                    execution.output = (
                        result_text.rstrip() + "\n\nContext ID: " + sub_context_id
                    )

                self.context_manager.add_execution(execution)

                # Log the full tool call result in verbose mode
                Logger("agent_factory").log_verbose(
                    f"TOOL RESULT: {tool_display_name}",
                    result if isinstance(result, str) else str(result),
                )

                result = self._truncate_tool_output(result, tool_display_name)
                return result

            except Exception as e:
                execution.end_time = time.time()
                execution.error = str(e)

                self.context_manager.add_execution(execution)

                raise

        agent_tool.on_invoke_tool = wrapped_invoke_tool
        return agent_tool

    def _create_context_aware_agent_tool(
        self,
        agent_key: str,
        sub_agent: Optional[Agent],
        tool_name: str,
        tool_description: str,
        context_strategy: str = "minimal",
        context_depth: int = 5,
        include_tool_history: bool = False,
    ) -> Any:
        """Create an agent tool that can share context with the sub-agent."""

        # Enhance tool description, but move common rules to the shared prompt (see settings.tools_common_rules)
        effective_description = tool_description or ""
        # Key local rules kept brief (one line), the rest in the shared block
        local_rule = "Call: pass a single field input (string). Allowed aliases: task, message, prompt."
        if effective_description:
            effective_description = effective_description + "\n" + local_rule
        else:
            effective_description = local_rule

        @function_tool(
            name_override=tool_name,
            description_override=effective_description,
        )
        async def run_agent_with_context(
            context: RunContextWrapper,
            input: str,
        ) -> str:
            local_sub_agent = sub_agent
            if local_sub_agent is None:
                local_sub_agent = await self.create_agent(agent_key)

            # Prepare human-readable context for the sub-agent
            # At this level, input must be a string, as normalization happened in `wrapped_invoke_tool`
            if not isinstance(input, str) or not input.strip():
                return f"❌ Empty input for tool '{tool_name}'. Please provide a non-empty 'input' (string)."

            raw_input = input.strip()

            # Context is passed ONLY if a context_id is explicitly specified in
            # the request; it names the sub-agent session to continue.
            requested_context_id = self._extract_context_id_from_text(raw_input)
            should_include_context = requested_context_id is not None

            if should_include_context:
                enhanced_input = self.context_manager.get_context_for_agent_tool(
                    strategy=context_strategy,
                    depth=context_depth,
                    include_tools=include_tool_history,
                    task_input=raw_input,
                )
            else:
                # For new sessions, pass only the original request without context
                enhanced_input = raw_input

            # Inherit user_id from parent context
            parent_user_id = (
                context.context.user_id
                if hasattr(context, "context") and hasattr(context.context, "user_id")
                else None
            )

            # The sub-agent session is the requested context, or a new one. Its id
            # is reported back with the result, so the caller can continue it.
            # The id comes from model-written text, so the session is also keyed
            # by user: a named id never opens another user's session.
            sub_context_id = requested_context_id or f"ctx-{uuid.uuid4().hex[:8]}"
            session = self._get_agent_session(
                agent_key,
                f"{sub_context_id}@{parent_user_id}" if parent_user_id else sub_context_id,
            )

            # Create GridRunContext for sub-agent with LOCAL session access
            parent_pipeline_id = (
                context.context.pipeline_id
                if hasattr(context, "context")
                and hasattr(context.context, "pipeline_id")
                else None
            )
            parent_step_id = (
                context.context.step_id
                if hasattr(context, "context") and hasattr(context.context, "step_id")
                else None
            )
            sub_run_ctx = GridRunContext(
                factory=self,
                context_id=sub_context_id,
                session=session,
                action_state=delegated_state(
                    getattr(getattr(context, "context", None), "action_state", None),
                    tool_name,
                    raw_input,
                ),
                action_depth=(
                    getattr(getattr(context, "context", None), "action_depth", 0) or 0
                )
                + 1,
                user_id=parent_user_id,
                container_id=self.container_id,
                pipeline_id=parent_pipeline_id,
                parent_step_id=parent_step_id,
                execution_mode="serial_subtree" if parent_pipeline_id else None,
            )
            # A sub-agent reports into its caller's view (the web trace, not the
            # console), through a child that keeps its prose out of the answer
            # and nests its steps under the call that started it.
            sub_observer = (
                getattr(getattr(context, "context", None), "stream_observer", None)
                or self._stream_observer
            )
            nested_observer = hasattr(sub_observer, "nested")
            if nested_observer:
                sub_observer = sub_observer.nested(
                    agent_key, call_id=getattr(context, "tool_call_id", None)
                )
            sub_run_ctx.stream_observer = sub_observer

            preamble = await self._auto_run_preamble(
                agent_key,
                self.config.get_agent(agent_key),
                local_sub_agent,
                sub_run_ctx,
                user_message=enhanced_input,
            )
            if preamble:
                enhanced_input = f"{preamble}\n\n[Current user request]\n\n{enhanced_input}"

            set_current_factory(self)
            run_error: Optional[str] = None
            result = None
            try:
                result = _get_runner().run_streamed(
                    starting_agent=local_sub_agent,
                    input=enhanced_input,
                    context=sub_run_ctx,
                    session=session,
                    max_turns=self.config.get_max_turns(),
                )
                await self._consume_stream(
                    result,
                    observer=sub_observer,
                    agent_key=agent_key,
                    action_state=sub_run_ctx.action_state,
                )
                output = run_output_text(result, f"Agent {agent_key}")
            except (MaxTurnsExceeded, ModelBehaviorError, AgentsUserError) as exc:
                # The sub-agent stopped, but its caller goes on: it gets what the
                # sub-agent did so far instead of a bare error, and can decide
                # whether to retry without repeating work already done.
                run_error = str(exc) or type(exc).__name__
                logger.warning("Sub-agent %s stopped: %s: %s", agent_key, type(exc).__name__, run_error)
                output = interrupted_run_report(result, f"Agent {agent_key}", exc)
            except BaseException as exc:
                run_error = str(exc) or type(exc).__name__
                raise
            finally:
                reset_current_factory()
                if nested_observer and hasattr(sub_observer, "finish"):
                    try:
                        sub_observer.finish(error=run_error)
                    except Exception:
                        logger.debug("Failed to settle sub-agent trace", exc_info=True)

            if isinstance(output, str):
                output = f"{output.rstrip()}\n\nContext ID: {sub_context_id}"

            # Record the result as an assistant message so the main agent can discuss and provide corrections
            try:
                self.context_manager.add_tool_result_as_message(tool_name, output)
            except Exception as exc:
                logger.debug(
                    "Failed to record tool result in context: %s", exc, exc_info=exc
                )

            return output

        return run_agent_with_context

    async def _create_mcp_servers(self, mcp_tool_names: List[str]) -> List[Any]:
        """Create and connect MCP servers using the Agents SDK."""
        logger.info(f"Creating MCP servers for tools: {mcp_tool_names}")
        servers: list[Any] = []
        unavailable: list[str] = []
        for name in mcp_tool_names:
            try:
                logger.debug(f"Attempting to create MCP server: {name}")
                server = await self._get_mcp_server(name)
                if server is not None:
                    servers.append(server)
                    logger.info(f"✅ MCP server created successfully: {name}")
                else:
                    unavailable.append(name)
                    logger.warning(f"❌ MCP server creation returned None: {name}")
            except Exception as e:
                unavailable.append(name)
                logger.error(
                    f"❌ MCP server creation failed: {name} - {e}", exc_info=True
                )

        if unavailable:
            logger.warning(f"Unavailable MCP servers: {unavailable}")
            try:
                self.context_manager.set_metadata("mcp_unavailable", unavailable)
            except Exception as exc:
                logger.warning(
                    "Failed to store MCP availability metadata: %s", exc, exc_info=exc
                )

        logger.info(
            f"Created {len(servers)} MCP servers out of {len(mcp_tool_names)} requested"
        )
        return servers

    async def _get_mcp_server(self, tool_name: str) -> Optional[Any]:
        """Get or create an SDK-based MCP server (MCPServerStdio)."""
        tool_config = self.config.get_tool(tool_name)
        if tool_config.type != "mcp":
            logger.warning(
                f"Tool '{tool_name}' is not of type 'mcp' (type={tool_config.type})"
            )
            return None

        cwd = self.config.get_working_directory()

        # IMPORTANT:
        # MCP tools like terminal/filesystem are started with cwd and/or a cwd argument.
        # If we cache only by tool_name, then per-user TG runs can reuse a server created
        # for a different cwd, breaking the "cwd is always user workspace" invariant.
        # Also include container_id in cache key
        cache_key = (
            f"{tool_name}::{cwd}"
            if getattr(tool_config, "add_working_directory", False)
            else tool_name
        )
        if self.container_id:
            cache_key += f"::{self.container_id}"

        if cache_key in self._mcp_servers:
            logger.debug("Reusing cached MCP server: %s", cache_key)
            return self._mcp_servers[cache_key]

        server_command = tool_config.server_command or []
        if not server_command:
            logger.error(f"MCP tool '{tool_name}' has no server_command configured")
            return None

        command = server_command[0]
        args = list(server_command[1:])
        # Make npx non-interactive
        if command.lower() in ("npx", "npx.cmd") and "-y" not in args:
            args.insert(0, "-y")

        env = dict(tool_config.env_vars or {})

        # Add working directory to args if configured (CRITICAL FIX for filesystem MCP)
        if getattr(tool_config, "add_working_directory", False):
            # Agent sees root as "/". In container pass "/" as the allowed root so MCP
            # filesystem accepts any absolute agent path (e.g. /docs, /sub/file).
            # The docker exec still runs with -w /workspace so relative ops work correctly.
            target_cwd = "/" if self.container_id else cwd
            args.append(target_cwd)
            logger.debug(f"Added working directory to MCP server args: {target_cwd}")

            # CRITICAL SAFETY (TG invariant):
            # Prevent `git` from walking up from the per-user workspace into the main repo.
            # project root when executed from host workspace.
            target_ceiling = self._container_workdir if self.container_id else cwd
            env.setdefault("GIT_CEILING_DIRECTORIES", target_ceiling)
            env.setdefault("GIT_DISCOVERY_ACROSS_FILESYSTEM", "0")
            # For beads, ensure it doesn't try to use a host daemon
            env.setdefault("BEADS_DAEMON", "0")

        # Wrap command for Docker execution if container_id is provided
        if self.container_id:
            # Construct docker exec command
            # docker exec -i -w <container workdir> [ENV] <container_id> <command> <args>

            # Forward proxy env vars from host so npm/npx can download packages inside container
            for _proxy_var in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "all_proxy",
            ):
                _proxy_val = os.environ.get(_proxy_var)
                if _proxy_val:
                    env.setdefault(_proxy_var, _proxy_val)

            # Save original command/args for logging/debug
            orig_cmd = command
            orig_args = args

            # Build docker exec args
            args = ["exec", "-i", "-w", self._container_workdir]

            # Pass environment variables
            for k, v in env.items():
                args.extend(["-e", f"{k}={v}"])

            args.extend([self.container_id, orig_cmd])
            args.extend(orig_args)

            command = "docker"

            logger.info(
                f"Wrapping MCP server in container {self.container_id}",
                extra={
                    "tool_name": tool_name,
                    "mcp_command": command,
                    "mcp_args": args,
                },
            )

        logger.info(
            f"Creating MCP server: {tool_name} | command={command} | args={args} | cwd={cwd}"
        )

        max_output_tokens = getattr(
            self.config.config.settings, "max_tool_output_tokens", None
        )
        server = ResilientMCPServerStdio(
            params={
                "command": command,
                "args": args,
                "env": env,
                "cwd": cwd,
            },
            cache_tools_list=True,
            name=tool_name,
            client_session_timeout_seconds=300,  # 5 minutes timeout (increased from default 5s)
            max_output_tokens=max_output_tokens,
        )

        await server.connect()
        logger.info(f"MCP server connected successfully: {tool_name}")
        self._mcp_servers[cache_key] = server
        return server

    def _extract_context_id_from_text(self, text: Optional[str]) -> Optional[str]:
        """Extract context identifier (ctx-XXXXXXXX) from arbitrary text."""
        if not text:
            return None
        match = CONTEXT_ID_REGEX.search(text)
        return match.group(0).lower() if match else None

    # Context management methods
    def add_to_context(
        self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add message to conversation context."""
        if metadata is None:
            metadata = {"context_id": self.context_manager.get_current_context_id()}
        self.context_manager.add_message(role, content, metadata=metadata)

    def clear_context(self) -> str:
        """Clear conversation context and start a new session."""
        return self.context_manager.clear_history()

    def get_active_context_id(self) -> Optional[str]:
        """Return the current active context identifier."""
        return self.context_manager.get_current_context_id()

    def activate_context(self, context_id: str) -> str:
        """Activate a specific context session by identifier."""
        return self.context_manager.activate_context(context_id)

    def list_context_ids(self) -> List[str]:
        """Return the list of known context identifiers."""
        return self.context_manager.list_context_ids()

    def get_context_info(self) -> Dict[str, Any]:
        """Get context information."""
        return self.context_manager.get_context_stats()

    def get_recent_executions(self, limit: int = 3) -> List[Any]:
        """Get recent executions from context manager."""
        return self.context_manager.get_recent_executions(limit=limit)

    # Cache management
    def clear_cache(self) -> None:
        """Clear all caches."""
        self._agent_cache.clear()
        self._tool_cache.clear()

    async def cleanup(self) -> None:
        """Cleanup resources."""
        gate = getattr(self, "action_gate", None)
        if gate is not None:
            await gate.aclose()
        # Disconnect MCP clients
        for mcp_client in self._mcp_servers.values():
            try:
                # SDK MCP servers expose cleanup()
                cleanup_method = getattr(mcp_client, "cleanup", None)
                if cleanup_method is not None:
                    await cleanup_method()
                else:
                    # Back-compat for any legacy clients
                    await mcp_client.disconnect()
            except asyncio.CancelledError:
                logger.debug("MCP cleanup cancelled", exc_info=True)
            except Exception as e:
                logger.warning(
                    "Failed to cleanup MCP server %s: %s",
                    getattr(mcp_client, "name", "unknown"),
                    e,
                    exc_info=e,
                )

        # Clear agent sessions
        await self._runtime_support.cleanup_sessions()

        # Kill stale dolt server started by beads inside the container (network_mode=host
        # makes container ports visible on the host, so orphaned dolt processes persist).
        if self.container_id:
            try:
                import subprocess

                subprocess.run(
                    ["docker", "exec", self.container_id, "pkill", "-f", "dolt"],
                    capture_output=True,
                    timeout=5,
                )
                logger.debug("Sent pkill dolt to container %s", self.container_id[:12])
            except Exception as e:
                logger.debug("Could not pkill dolt in container: %s", e)

        # Clear caches
        self.clear_cache()
        self._mcp_servers.clear()
        self._agent_sessions.clear()

    # Fallback: stub for manual tool call parsing from response text
    def _detect_malformed_tool_calls(self, output: str) -> bool:
        """
        Detect malformed tool call formats in agent output.

        Returns True if malformed tool calls are detected.
        Examples of malformed formats:
        - <tool_call><function=get_screen><parameter=save_path>...</parameter></function></tool_call>
        """
        # Pattern for malformed tool_call format
        malformed_patterns = [
            r"<tool_call>\s*<function=",  # <tool_call><function=...>
            r"<function=[^>]+>\s*<parameter=",  # <function=name><parameter=...>
        ]

        for pattern in malformed_patterns:
            if re.search(pattern, output):
                logger.warning(
                    f"Detected malformed tool call format in output: {pattern}"
                )
                return True

        return False
