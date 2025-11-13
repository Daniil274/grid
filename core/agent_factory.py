"""
Agent Factory with caching, tracing, and error handling.
"""

import asyncio
import time
import logging
import threading
import sys
from typing import List, Dict, Any, Optional, Protocol
from dotenv import load_dotenv
from openai import AsyncOpenAI

# OpenAI Agents SDK imports
import agents
from agents import (
    Agent,
    OpenAIChatCompletionsModel,
    set_tracing_disabled,
    function_tool,
    RunContextWrapper,
    SQLiteSession,
    RunItemStreamEvent,
    RawResponsesStreamEvent,
)
from agents.items import ItemHelpers
from agents.mcp import MCPServerStdio

from .config import Config
from .context import ContextManager, safe_lock
from schemas import AgentConfig, AgentExecution
from tools import get_tools_by_names
from utils.exceptions import AgentError, ConfigError, ContextError
from core.tracing_config import get_tracing_config

import json
import re
import uuid

load_dotenv()
tracing_config = get_tracing_config()

CONTEXT_ID_REGEX = re.compile(r"ctx-[0-9a-fA-F]{8,}")
logger = logging.getLogger("grid.agent_factory")
_TRACING_CONFIGURED = False
_TRACING_CONFIG_LOCK = threading.Lock()


def __getattr__(name: str) -> Any:
    if name == "Runner":
        return getattr(agents, "Runner")
    raise AttributeError(name)


def _get_runner() -> Any:
    """Resolve the current Runner implementation, honoring runtime patches."""
    return getattr(sys.modules[__name__], "Runner")


class StreamObserver(Protocol):
    """Protocol for components that render streaming events."""

    def handle_event(self, event: Any, *, agent_key: Optional[str] = None) -> Optional[str]:
        """Render a streaming event. Return text fragments to append to buffers if any."""


class ConsoleStreamObserver:
    """Default stream observer that mirrors legacy console output."""

    def __init__(self, output_writer=None) -> None:
        self._write = output_writer or print
        self._logger = logging.getLogger("grid.agent_factory.stream")

    def _emit(self, message: str, *, end: str = "\n", flush: bool = False) -> None:
        self._write(message, end=end, flush=flush)

    def handle_event(self, event: Any, *, agent_key: Optional[str] = None) -> Optional[str]:
        try:
            if isinstance(event, RunItemStreamEvent):
                name = getattr(event, "name", "")
                item = getattr(event, "item", None)
                if name == "tool_called" and item is not None:
                    raw_item = getattr(item, "raw_item", None)
                    tool_name = getattr(raw_item, "name", None) or getattr(raw_item, "type", None) or "tool"
                    arguments = getattr(raw_item, "arguments", None)
                    args_str = ""
                    if isinstance(arguments, str):
                        args_str = arguments
                    elif isinstance(arguments, dict):
                        parts = []
                        for key, value in arguments.items():
                            if isinstance(value, str) and len(value) > 60:
                                parts.append(f"{key}=...({len(value)} символов)")
                            elif isinstance(value, (dict, list)):
                                parts.append(f"{key}={type(value).__name__}({len(value)})")
                            else:
                                parts.append(f"{key}={value}")
                        args_str = " | ".join(parts)
                    elif arguments is not None:
                        args_str = str(arguments)

                    server_label = getattr(raw_item, "server_label", None)
                    tool_display_name = f"{server_label}.{tool_name}" if server_label else tool_name
                    if args_str:
                        self._emit(f"\n🔧 {tool_display_name} · {args_str}")
                    else:
                        self._emit(f"\n🔧 {tool_display_name}")
                elif name == "tool_output" and item is not None:
                    raw_item = getattr(item, "raw_item", None)
                    tool_name = getattr(raw_item, "name", None) or getattr(raw_item, "type", None) or "tool"
                    output_val = getattr(item, "output", "")
                    server_label = getattr(raw_item, "server_label", None)
                    tool_display_name = f"{server_label}.{tool_name}" if server_label else tool_name
                    output_str = str(output_val)
                    if len(output_str) > 200:
                        output_str = output_str[:200] + "…"
                    self._emit(f"✅ {tool_display_name} → {output_str}")
                elif name == "handoff_requested" and item is not None:
                    src = getattr(item, "agent", None)
                    src_name = getattr(src, "name", None) or agent_key or "agent"
                    raw_item = getattr(item, "raw_item", None)
                    target = getattr(raw_item, "name", None) or "agent"
                    self._emit(f"\n🔀 {src_name} → {target}")
                elif name == "handoff_occured" and item is not None:
                    src_agent = getattr(item, "source_agent", None)
                    dst_agent = getattr(item, "target_agent", None)
                    src_name = getattr(src_agent, "name", None) or "agent"
                    dst_name = getattr(dst_agent, "name", None) or "agent"
                    self._emit(f"🔁 {src_name} ⇒ {dst_name}")
                elif name == "mcp_list_tools" and item is not None:
                    raw_item = getattr(item, "raw_item", None)
                    server_label = getattr(raw_item, "server_label", None) or "mcp"
                    tools = getattr(raw_item, "tools", None)
                    count = len(tools) if tools is not None else "?"
                    self._emit(f"🧩 MCP {server_label}: {count} tool(s)")
            elif isinstance(event, RawResponsesStreamEvent):
                content: Optional[str] = None
                if hasattr(event, "content") and event.content:
                    content = event.content
                elif hasattr(event, "delta") and event.delta:
                    content = event.delta
                elif hasattr(event, "text") and event.text:
                    content = event.text
                elif hasattr(event, "data") and event.data:
                    data = event.data
                    if hasattr(data, "delta") and data.delta:
                        content = data.delta
                    elif hasattr(data, "content") and data.content:
                        content = data.content
                    elif hasattr(data, "text") and data.text:
                        content = data.text
                    elif isinstance(data, dict):
                        content = data.get("content") or data.get("delta") or data.get("text")

                if content and isinstance(content, str) and content.strip():
                    self._emit(content, end="", flush=True)
                    return content
        except Exception:
            self._logger.exception("Failed to render streaming event")
        return None


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
    
    def __init__(
        self,
        config: Optional[Config] = None,
        working_directory: Optional[str] = None,
        *,
        tracing_level: Optional[str] = "INFO",
        stream_observer: Optional[StreamObserver] = None,
    ):
        """
        Initialize Agent Factory.
        
        Args:
            config: Configuration instance (creates default if None)
            working_directory: Working directory override
        """
        if tracing_level is not None:
            self._configure_tracing_once(tracing_level)
        
        # Set up minimal logging for agents SDK to avoid spam
        agents_logger = logging.getLogger("openai.agents")
        agents_logger.setLevel(logging.WARNING)
        
        self.config = config or Config()
        if working_directory:
            self.config.set_working_directory(working_directory)

        # Initialize image processing config
        from utils.image_utils import ImageUtils
        if hasattr(self.config.config, 'settings'):
            ImageUtils.set_config(self.config.config.settings.image_processing)

        # Initialize managers
        self.context_manager = ContextManager(
            max_history=self.config.get_max_history(),
            persist_path=None  # Контекст сохраняется только в памяти, не в файле
        )
        
        # Caches
        self._agent_cache: Dict[str, Agent] = {}
        self._tool_cache: Dict[str, List[Any]] = {}
        # Deprecated: _mcp_clients kept for backward compatibility (no longer used)
        self._mcp_clients: Dict[str, Any] = {}
        # New MCP servers cache (SDK-based)
        self._mcp_servers: Dict[str, Any] = {}
        
        # Session management for agent memory (per agent/context pair)
        self._agent_sessions: Dict[tuple[str, str], SQLiteSession] = {}
        # Track emitted warnings to avoid log spam (e.g., Responses API fallbacks)
        self._responses_warning_keys: set[str] = set()
        self._stream_observer: StreamObserver = stream_observer or ConsoleStreamObserver()

    @staticmethod
    def _configure_tracing_once(level: str) -> None:
        global _TRACING_CONFIGURED
        if _TRACING_CONFIGURED:
            return
        with _TRACING_CONFIG_LOCK:
            if _TRACING_CONFIGURED:
                return
            tracing_config.configure_console_tracing(level)
            tracing_config.apply()
            _TRACING_CONFIGURED = True
        

    
    async def initialize(self) -> None:
        """Async init hook for compatibility with API lifespan."""
        return None
    
    # ---------------------------------------------------------------------
    # Lightweight model resolution helpers for API (e.g., Cline endpoint)
    # ---------------------------------------------------------------------
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

    def get_openai_client_for_model(self, model_key: str) -> tuple[AsyncOpenAI, str]:
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
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=provider_cfg.base_url,
            timeout=provider_cfg.timeout,
            max_retries=provider_cfg.max_retries,
        )
        return client, model_cfg.name
    
    def _get_agent_session(self, agent_key: str, context_id: str) -> SQLiteSession:
        """Get or create a session scoped to an agent/context pair."""
        session_key = (agent_key, context_id)
        if session_key not in self._agent_sessions:
            session_id = f"agent_{agent_key}_{context_id}"
            self._agent_sessions[session_key] = SQLiteSession(session_id)

        return self._agent_sessions[session_key]
    
    def _is_reasoning_model_name(self, model_name: str) -> bool:
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
    
    async def create_agent(
        self, 
        agent_key: str, 
        context_path: Optional[str] = None,
        force_reload: bool = False
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
            model_config = self.config.get_model(agent_config.model)
            provider_config = self.config.get_provider(model_config.provider)
            
            # Validate API key
            api_key = self.config.get_api_key(model_config.provider)
            if not api_key:
                raise AgentError(
                    f"API key not found for provider '{model_config.provider}'",
                    details={
                        "provider": model_config.provider,
                        "env_var": provider_config.api_key_env
                    }
                )
            
            # Create OpenAI client
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=provider_config.base_url,
                timeout=provider_config.timeout,
                max_retries=provider_config.max_retries
            )
            
            # Create model (auto-switch to Responses API for reasoning models if available)
            model = None
            use_responses = False
            try:
                use_responses = bool(getattr(model_config, "use_responses_api", False))
            except Exception:
                use_responses = False
            
            # Разрешаем Responses API только для провайдера OpenAI
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
                    # Lazy import to not require newer SDK if not installed
                    from agents import OpenAIResponsesModel  # type: ignore
                    model = OpenAIResponsesModel(
                        model=model_config.name,
                        openai_client=client
                    )

                except Exception as e:
                    warn_key = f"{model_config.provider}|{provider_config.base_url}|{model_config.name}|init_fail"
                    if warn_key not in self._responses_warning_keys:
                        self._responses_warning_keys.add(warn_key)
                        logger.warning(
                            "Failed to initialize Responses model: %s", e,
                            extra={
                                "provider": model_config.provider,
                                "base_url": provider_config.base_url,
                                "model": model_config.name,
                            },
                        )
                    use_responses = False
            
            if model is None:
                model = OpenAIChatCompletionsModel(
                    model=model_config.name,
                    openai_client=client
                )
            
            # Build instructions with context (включаем контекст диалога для агентов)
            instructions = self._build_agent_instructions(agent_key, context_path, include_conversation_context=True)
            
            # Get tools (function and agent tools only; MCP tools handled via mcp_servers)
            tools = await self._get_agent_tools(agent_config)

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
            if mcp_server_names and (agent_config.mcp_enabled or self.config.is_mcp_enabled()):
                mcp_servers_list = await self._create_mcp_servers(mcp_server_names)

            # Create agent
            agent = Agent(
                name=agent_config.name,
                instructions=instructions,
                model=model,
                tools=tools,
                mcp_servers=mcp_servers_list,
            )
            
            self._agent_cache[cache_key] = agent
            
            return agent
            
        except Exception as e:
            error_msg = f"Failed to create agent '{agent_key}': {e}"
            raise AgentError(error_msg, details={"agent_key": agent_key}) from e
    
    async def run_agent(
        self,
        agent_key: str,
        message: str,
        context_path: Optional[str] = None,
        context_id: Optional[str] = None,
        *,
        stream: bool = False,
        streaming: Optional[bool] = None,
        use_active_context: bool = False,
    ) -> str:
        """
        Run agent with message and context management.
        
        Args:
            agent_key: Agent to run
            message: Input message
            context_path: Optional context path
            context_id: Optional identifier of a saved conversation context
            stream: Whether to stream response (alias: streaming)
            use_active_context: If True, use the currently active context instead of creating new one
            
        Returns:
            Agent response
        """
        start_time = time.time()
        execution = AgentExecution(
            agent_name=agent_key,
            start_time=str(start_time),
            input_message=message
        )
        active_context_id: Optional[str] = None
        context_marker_line: Optional[str] = None

        if streaming is not None:
            stream = streaming
        
        try:

            try:
                if context_id:
                    active_context_id = self.context_manager.activate_context(context_id)
                elif use_active_context:
                    # Используем активный контекст, если он есть, иначе создаем новый
                    current_id = self.context_manager.get_current_context_id()
                    if current_id:
                        active_context_id = current_id
                    else:
                        active_context_id = self.context_manager.start_new_context()
                else:
                    active_context_id = self.context_manager.start_new_context()
            except ContextError as exc:
                raise AgentError("Failed to prepare conversation context") from exc

            execution.context_id = active_context_id
            if active_context_id:
                self.context_manager.set_metadata("context_id", active_context_id)
                self.context_manager.set_metadata(
                    "last_invocation",
                    {
                        "agent": agent_key,
                        "timestamp": time.time(),
                    },
                )
            
            # Create agent
            agent = await self.create_agent(agent_key, context_path)
            
            # Parse message if it's a JSON string (for multimodal messages with images)
            # According to Agents SDK, Runner.run accepts: str | list[TResponseInputItem]
            # If message is JSON string, parse it to dict/list before passing to Runner
            # IMPORTANT: When using session, we can only pass string, not list
            # So for multimodal messages, we need to disable session or use session_input_callback
            parsed_message = message
            is_multimodal = False
            try:
                if isinstance(message, str) and message.strip().startswith('{'):
                    # Try to parse as JSON - might be a multimodal message
                    parsed_message = json.loads(message)
                    # If it's a single message dict, wrap in list (as per SDK examples)
                    if isinstance(parsed_message, dict) and "role" in parsed_message:
                        # Check if it contains images
                        content = parsed_message.get("content", [])
                        if isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") in ("input_image", "image_url"):
                                    is_multimodal = True
                                    break
                        parsed_message = [parsed_message]
                    elif isinstance(parsed_message, list):
                        # Check if list contains multimodal content
                        for msg in parsed_message:
                            if isinstance(msg, dict):
                                content = msg.get("content", [])
                                if isinstance(content, list):
                                    for part in content:
                                        if isinstance(part, dict) and part.get("type") in ("input_image", "image_url"):
                                            is_multimodal = True
                                            break
            except (json.JSONDecodeError, ValueError):
                # Not JSON, keep as string
                parsed_message = message
            
            # Определяем, нужно ли включать контекст диалога
            # Контекст включается если:
            # 1. Указан явный context_id (пользователь хочет продолжить диалог)
            # 2. Используется активный контекст (интерактивный режим)
            include_conversation_context = (
                context_id is not None or  # Явно указан context_id
                use_active_context  # Используем активный контекст
            )
            
            # Check if conversation history contains images
            # If yes, we need to use list format instead of session for ALL messages
            # (because session doesn't preserve images from previous messages)
            history_has_images = False
            try:
                # Get raw conversation history (ContextMessage objects)
                with safe_lock(self.context_manager._lock, timeout=5.0):
                    history = self.context_manager._conversation_history
                    for msg in history:
                        # Check if message has images using ContextMessage.has_images() method
                        if hasattr(msg, "has_images"):
                            if msg.has_images():
                                history_has_images = True
                                break
                        # Fallback: check content directly
                        elif hasattr(msg, "content"):
                            if isinstance(msg.content, list):
                                for part in msg.content:
                                    if isinstance(part, dict):
                                        if part.get("type") in ("input_image", "image_url", "image_file"):
                                            history_has_images = True
                                            break
                                    elif hasattr(part, "type") and part.type in ("input_image", "image_url", "image_file"):
                                        history_has_images = True
                                        break
                        if history_has_images:
                            break
            except Exception as e:
                logger.debug(f"Failed to check history for images: {e}")
            
            # If current message is multimodal OR history has images, use list format
            needs_list_format = is_multimodal or history_has_images
            
            # Не добавляем инструкции агента в диалог; сохраняем в metadata для служебного использования
            if not self.context_manager.get_conversation_context():
                agent_instructions = self._build_agent_instructions(agent_key, context_path, include_conversation_context=False)
                self.context_manager.set_metadata("agent_instructions", agent_instructions)
            
            # For messages that need list format, get history BEFORE adding current message
            # (so we can prepend it to the input list)
            history_messages = []
            if needs_list_format:
                history_messages = self.context_manager.get_conversation_history_as_sdk_messages()
            
            # Добавляем сообщение в контекст для текущей сессии
            # For multimodal messages, we need to parse JSON and create proper ContextMessage
            if is_multimodal and isinstance(parsed_message, list) and len(parsed_message) > 0:
                # Extract content from parsed message
                msg_dict = parsed_message[0] if isinstance(parsed_message[0], dict) else {}
                msg_content = msg_dict.get("content", [])
                
                # Convert SDK format content parts to ContextMessage format
                # SDK uses: [{"type": "input_text", "text": "..."}, {"type": "input_image", "image_url": "..."}]
                # ContextMessage needs: [TextContent(...), ImageContent(...)]
                from schemas import ContextMessage, TextContent, ImageContent, ImageUrl
                from datetime import datetime
                
                content_parts = []
                for part in msg_content:
                    if isinstance(part, dict):
                        part_type = part.get("type")
                        if part_type == "input_text":
                            content_parts.append(TextContent(type="text", text=part.get("text", "")))
                        elif part_type == "input_image":
                            image_url = part.get("image_url", "")
                            detail = part.get("detail", "auto")
                            # image_url should already be base64 data URL from prepare_agent_message
                            # Store it as ImageContent so it can be converted back to SDK format
                            content_parts.append(ImageContent(
                                type="image_url",
                                image_url=ImageUrl(url=image_url, detail=detail)
                            ))
                            logger.debug(f"Storing image in context: {len(image_url)} chars (base64 URL)")
                        # Pass through other types as dict
                        else:
                            content_parts.append(part)
                    else:
                        content_parts.append(part)
                
                # Create ContextMessage with multimodal content
                multimodal_msg = ContextMessage(
                    role="user",
                    content=content_parts,
                    timestamp=datetime.now().isoformat(),
                    metadata={
                        "context_id": active_context_id,
                        "agent": agent_key,
                        "type": "user_input",
                    },
                )
                # Add directly to context history using safe_lock
                try:
                    with safe_lock(self.context_manager._lock, timeout=5.0):
                        self.context_manager._conversation_history.append(multimodal_msg)
                        # Trim history if needed
                        if len(self.context_manager._conversation_history) > self.context_manager.max_history:
                            self.context_manager._conversation_history.pop(0)
                        # Update context bucket
                        active_bucket = self.context_manager._contexts.get(self.context_manager._current_context_id)
                        if active_bucket is not None:
                            active_bucket["updated_at"] = datetime.now().isoformat()
                except Exception as e:
                    logger.warning(f"Failed to add multimodal message to context: {e}, falling back to string")
                    self.context_manager.add_message(
                        "user",
                        message,
                        metadata={
                            "context_id": active_context_id,
                            "agent": agent_key,
                            "type": "user_input",
                        },
                    )
            else:
                # Simple text message - use standard add_message
                self.context_manager.add_message(
                    "user",
                    message,  # Store original message string for context
                    metadata={
                        "context_id": active_context_id,
                        "agent": agent_key,
                        "type": "user_input",
                    },
                )
            

            agent_instructions = self._build_agent_instructions(agent_key, context_path, include_conversation_context)
            
            # Run agent with max_turns configuration and timeout
            max_turns = self.config.get_max_turns()
            timeout_seconds = self.config.get_agent_timeout()
            
            # Prepare session scoped to the active context
            # IMPORTANT: Session cannot be used with list input (multimodal messages)
            # For multimodal messages, we disable session and manage history manually via context
            if not active_context_id:
                raise AgentError("Failed to prepare conversation context")
            
            # For multimodal messages OR if history has images, don't use session (SDK limitation)
            # We'll manage history by prepending it to the input message list
            if needs_list_format:
                session = None
                if is_multimodal:
                    logger.debug("Multimodal message detected - disabling session, prepending history to input")
                elif history_has_images:
                    logger.debug("History contains images - disabling session, prepending history to input")
                
                # Convert current message to list format if it's a string
                if isinstance(parsed_message, str):
                    # Simple text message - convert to SDK format
                    parsed_message = [{"role": "user", "content": parsed_message}]
                elif isinstance(parsed_message, dict):
                    parsed_message = [parsed_message]
                # If already a list, keep it as is
                
                # Prepend history to current message (history was fetched before adding current message)
                parsed_message = history_messages + parsed_message
            else:
                session = self._get_agent_session(agent_key, active_context_id)
                agent._session = session
            if stream:
                # Streaming режим: прозрачная подсветка tool/MCP вызовов через наблюдателя
                result_output: Optional[str] = None
                try:
                    run_result_streaming = _get_runner().run_streamed(
                        agent,
                        parsed_message,  # Use parsed message (dict/list or string) with history prepended
                        context=self.context_manager.get_conversation_context() if not needs_list_format else None,
                        max_turns=max_turns,
                        session=session,
                    )
                    streaming_text_parts: List[str] = []
                    async for event in run_result_streaming.stream_events():
                        try:
                            fragment = self._stream_observer.handle_event(event, agent_key=agent_key)
                            if fragment:
                                streaming_text_parts.append(fragment)
                        except Exception:
                            logger.exception(
                                "Stream observer failed for %s", type(event).__name__
                            )
                    result_output = (
                        run_result_streaming.final_output
                        if run_result_streaming.final_output is not None
                        else ""
                    )
                    if (not result_output or str(result_output).strip() == "") and streaming_text_parts:
                        try:
                            buffered_text = "".join(streaming_text_parts).strip()
                            if buffered_text:
                                result_output = buffered_text
                        except Exception:
                            logger.exception("Failed to merge streaming text fragments")
                except asyncio.TimeoutError:
                    logger.error(f"Agent execution timed out after {timeout_seconds} seconds")
                    raise AgentError(f"Agent execution timed out after {timeout_seconds} seconds")
                except Exception as e:
                    raise AgentError(f"Agent execution failed: {e}") from e
                result = result_output
            else:
                try:
                    # Запускаем агента и получаем RunResult объект
                    result = await asyncio.wait_for(
                        _get_runner().run(
                            agent, 
                            parsed_message,  # Use parsed message (dict/list or string) with history prepended
                            context=self.context_manager.get_conversation_context() if not needs_list_format else None,
                            max_turns=max_turns, 
                            session=session
                        ),
                        timeout=timeout_seconds
                    )

                except asyncio.TimeoutError:
                    raise AgentError(f"Agent execution timed out after {timeout_seconds} seconds")
                except Exception as e:
                    raise AgentError(f"Agent execution failed: {e}") from e
            
            # Process result - more robust extraction
            try:
                            # Проверяем, является ли result строкой (уже обработанной)
                if isinstance(result, str):
                    output = result
                elif hasattr(result, 'final_output') and result.final_output:
                    output = result.final_output
                elif hasattr(result, 'output') and result.output:
                    output = result.output
                elif hasattr(result, 'content') and result.content:
                    output = result.content
                else:
                    output = str(result)
                
                # Ensure we have a non-empty response
                if not output or output.strip() == "":
                    output = "Агент выполнил задачу, но не предоставил текстовый ответ. Проверьте логи для деталей выполнения."
            except Exception as e:
                output = "Произошла ошибка при обработке результата агента. Проверьте логи."
            
            # Добавляем ответ агента в контекст для текущей сессии
            # Post-process potential manual tool call before storing response
            try:
                manual_tool_result = await self._execute_first_tool_call_in_text(output)
                if manual_tool_result is not None:
                    output = manual_tool_result
            except Exception as e:
                logger.debug("Manual tool-call hook failed: %s", e, exc_info=e)

            if active_context_id:
                context_marker_line = f"\u041a\u043e\u043d\u0442\u0435\u043a\u0441\u0442 ID: {active_context_id}"
                english_marker_line = f"Context ID: {active_context_id}"
                normalized_output = output or ""
                has_marker = context_marker_line in normalized_output or english_marker_line in normalized_output
                if not has_marker:
                    trimmed_output = normalized_output.rstrip()
                    if trimmed_output:
                        output = f"{trimmed_output}\n\n{context_marker_line}"
                    else:
                        output = context_marker_line
                else:
                    output = normalized_output
            else:
                context_marker_line = None

            self.context_manager.add_message(
                "assistant",
                output,
                metadata={
                    "context_id": active_context_id,
                    "agent": agent_key,
                    "type": "agent_response",
                },
            )

            # Update execution record
            execution.end_time = time.time()
            execution.output = output
            # Если у нас был RunResult, попробуем извлечь список инструментов
            tools_used: List[str] = []
            try:
                if not isinstance(result, str):
                    tools_used = self._extract_tools_used(result)
                execution.tools_used = tools_used
            except Exception as e:
                execution.tools_used = []
                logger.debug("Failed to extract tools used from result: %s", e, exc_info=e)
            
            duration = execution.end_time - start_time
            
            self.context_manager.add_execution(execution)
            
            return output
            
        except Exception as e:
            execution.end_time = time.time()
            execution.error = str(e)
            
            self.context_manager.add_execution(execution)
            
            raise
    
    def _build_agent_instructions(self, agent_key: str, context_path: Optional[str] = None, include_conversation_context: bool = True) -> str:
        """Build complete agent instructions with context."""
        base_instructions = self.config.build_agent_prompt(agent_key)
        
        # Add path context
        path_context = self._build_path_context(context_path)
        
        # Combine all parts
        parts = [base_instructions]
        
        if path_context:
            parts.append(path_context)
        
        context_identifier = self.context_manager.get_current_context_id()
        if context_identifier:
            context_instruction = (
                f"Context reference: {context_identifier}. "
                f"Always append the line \"\u041a\u043e\u043d\u0442\u0435\u043a\u0441\u0442 ID: {context_identifier}\" "
                "to every reply so humans or agents can return to this dialogue via that identifier."
            )
            parts.append(context_instruction)

        # Добавляем контекст текущей сессии только если явно запрошено
        if include_conversation_context:
            conversation_context = self.context_manager.get_conversation_context()
            if conversation_context:
                parts.append(conversation_context)
        
        return "\n\n".join(parts)
    
    def _build_path_context(self, context_path: Optional[str] = None) -> str:
        """Build path context information."""
        working_dir = self.config.get_working_directory()
        config_dir = self.config.get_config_directory()
        
        context_parts = [
            "Информация о путях:",
            f"Рабочая директория: {working_dir}",
            f"Директория конфигурации: {config_dir}"
        ]
        
        if context_path:
            absolute_path = self.config.get_absolute_path(context_path)
            context_parts.extend([
                f"Контекстный путь: {context_path}",
                f"Абсолютный контекстный путь: {absolute_path}"
            ])
        
        context_parts.extend([
            "",
            "Используй эти пути для работы с файлами и директориями."
        ])
        
        return "\n".join(context_parts)
    
    async def _get_agent_tools(self, agent_config: AgentConfig) -> List[Any]:
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
                    "Tool configuration missing for agent", extra={"agent": agent_config.name, "tool": tool_name}, exc_info=exc
                )
        
        # Add function tools
        if function_tools:
            try:
                func_tools = get_tools_by_names(function_tools)
                tools.extend(func_tools)

            except Exception as e:
                logger.error("Failed to load function tools %s: %s", function_tools, e, exc_info=e)
        
        # Add agent tools
        if agent_tools:
            try:
                agent_tool_instances = await self._create_agent_tools(agent_tools)
                tools.extend(agent_tool_instances)

            except Exception as e:
                logger.error("Failed to create agent tools %s: %s", agent_tools, e, exc_info=e)
 

        # Cache tools
        self._tool_cache[cache_key] = tools
        
        return tools
    
    async def _create_agent_tools(self, agent_keys: List[str]) -> List[Any]:
        """Create agent tools with proper logging and context sharing."""
        tools = []
        
        for agent_key in agent_keys:
            try:
                # Create sub-agent
                sub_agent = await self.create_agent(agent_key)
                
                # Get tool configuration
                tool_config = self.config.get_tool(agent_key)
                tool_name = tool_config.name or f"call_{agent_key}"
                tool_description = tool_config.description or f"Calls {sub_agent.name}"
                
                # Get context sharing parameters from tool config
                context_strategy = getattr(tool_config, 'context_strategy', 'conversation')
                context_depth = getattr(tool_config, 'context_depth', 5)
                include_tool_history = getattr(tool_config, 'include_tool_history', True)
                
                # Create context-aware tool (основное имя)
                main_tool = self._create_context_aware_agent_tool(
                    agent_key=agent_key,
                    sub_agent=sub_agent,
                    tool_name=tool_name,
                    tool_description=tool_description,
                    context_strategy=context_strategy,
                    context_depth=context_depth,
                    include_tool_history=include_tool_history
                )
                
                # Wrap for logging
                wrapped_main = self._wrap_agent_tool(main_tool, sub_agent.name)
                tools.append(wrapped_main)
                
                # Добавим алиасы каналов, чтобы не падать, если модель приписывает суффиксы каналов
                channel_suffixes = ("_commentary", "_tool", "_final")
                for suffix in channel_suffixes:
                    alias_tool = self._create_context_aware_agent_tool(
                        agent_key=agent_key,
                        sub_agent=sub_agent,
                        tool_name=f"{tool_name}{suffix}",
                        tool_description=tool_description,
                        context_strategy=context_strategy,
                        context_depth=context_depth,
                        include_tool_history=include_tool_history
                    )
                    wrapped_alias = self._wrap_agent_tool(alias_tool, sub_agent.name)
                    tools.append(wrapped_alias)
                
            except Exception as e:
                logger.error(
                    "Failed to configure agent tool", extra={"agent_tool": agent_key}, exc_info=e
                )

        return tools
    
    def _wrap_agent_tool(self, agent_tool: Any, agent_name: str) -> Any:
        """Wrap agent tool for proper logging and execution tracking."""
        if not hasattr(agent_tool, 'on_invoke_tool'):
            return agent_tool
        
        original_invoke = agent_tool.on_invoke_tool
        
        async def wrapped_invoke_tool(tool_context, tool_call_arguments):
            start_time = time.time()
            # Нормализуем и логируем аргументы инструмента
            normalized_args = tool_call_arguments
            # Приводим к словарю и сводим все алиасы к одному обязательному полю 'input'
            preferred_text: Optional[str] = None
            if isinstance(tool_call_arguments, dict):
                # Приоритет текстовых алиасов над input, чтобы не терять задачу
                for alias in ('task', 'message', 'prompt', 'input'):
                    value = tool_call_arguments.get(alias)
                    if isinstance(value, str) and value.strip():
                        preferred_text = value.strip()
                        break
                # Если пришёл null/None или пустые строки — заменим на пустую строку
                if not isinstance(preferred_text, str):
                    preferred_text = ""
                normalized_args = { 'input': preferred_text }
            else:
                # Если пришла не-структурированная форма, приводим к строке
                preferred_text = str(tool_call_arguments) if tool_call_arguments is not None else ""
                normalized_args = { 'input': preferred_text }

            # Безопасно преобразуем аргументы в строку для логов
            requested_context_id = self._extract_context_id_from_text(preferred_text)
            sub_context_id = requested_context_id or f"ctx-{uuid.uuid4().hex[:8]}"

            # Безопасно преобразуем аргументы в строку для логов
            input_data = str(normalized_args)

            execution = AgentExecution(
                agent_name=agent_name,
                start_time=str(start_time),
                input_message=input_data
            )
            execution.context_id = sub_context_id

            try:
                
                
                # Логируем вызов инструмента с красивым именем
                tool_display_name = getattr(agent_tool, 'name', agent_name)
                # Добавляем префикс для агентов-инструментов
                formatted_tool_name = f"Agent-Tool: {tool_display_name}"

                # Call original function с нормализованными аргументами
                result = original_invoke(tool_context, **normalized_args)
                if hasattr(result, '__await__'):
                    result = await result
                
                execution.end_time = time.time()
                # Безопасно преобразуем результат в строку
                if isinstance(result, str):
                    result_text = result
                else:
                    result_text = str(result)

                marker_line = f"Контекст ID: {sub_context_id}"
                if marker_line not in result_text:
                    result_text = result_text.rstrip() + "\n\n" + marker_line

                execution.output = result_text
                
                duration = execution.end_time - execution.start_time
                
                self.context_manager.add_execution(execution)
                
                return result_text
                
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
        sub_agent: Agent,
        tool_name: str,
        tool_description: str,
        context_strategy: str = "minimal",
        context_depth: int = 5,
        include_tool_history: bool = False
    ) -> Any:
        """Create an agent tool that can share context with the sub-agent."""
        
        # Усиливаем описание инструмента, но выносим общие правила в общий промпт (см. settings.tools_common_rules)
        effective_description = (tool_description or "")
        # Ключевые локальные правила оставим кратко (одна строка), остальное в общем блоке
        local_rule = "Вызов: передавай одно поле input (string). Допустимые алиасы: task, message, prompt."
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
            # Подготавливаем человекочитаемый контекст для подагента
            # На этом уровне input должен быть строкой, т.к. нормализация прошла в `wrapped_invoke_tool`
            if not isinstance(input, str) or not input.strip():
                return f"❌ Пустой ввод для инструмента '{tool_name}'. Передайте непустой 'input' (string)."
            
            raw_input = input.strip()
            
            # Определяем, нужно ли передавать контекст агенту-инструменту
            # Контекст передается ТОЛЬКО если:
            # 1. Явно указан context_id в запросе
            should_include_context = (
                self._extract_context_id_from_text(raw_input) is not None  # Явно указан context_id в запросе
            )
            
            if should_include_context:
                enhanced_input = self.context_manager.get_context_for_agent_tool(
                    strategy=context_strategy,
                    depth=context_depth,
                    include_tools=include_tool_history,
                    task_input=raw_input
                )
            else:
                # Для новых сессий передаем только исходный запрос без контекста
                enhanced_input = raw_input
            
            # Сессия подагента привязывается к выбранному контексту
            # Если контекст не передается, создаем новый контекст для подагента
            if should_include_context:
                # Используем текущий контекст, если контекст передается
                current_context_id = self.context_manager.get_current_context_id()
                session = self._get_agent_session(agent_key, current_context_id or f"ctx-{uuid.uuid4().hex[:8]}")
            else:
                # Создаем новый контекст для подагента, если контекст не передается
                new_context_id = f"ctx-{uuid.uuid4().hex[:8]}"
                session = self._get_agent_session(agent_key, new_context_id)
            sub_agent._session = session
            
            # Run the sub-agent with enhanced input and session
            output = await _get_runner().run(
                starting_agent=sub_agent,
                input=enhanced_input,
                context=context.context,
                session=session,
                max_turns=self.config.get_max_turns(),
            )
            
            # Запишем результат как сообщение ассистента, чтобы главный агент мог обсуждать и давать правки
            try:
                self.context_manager.add_tool_result_as_message(tool_name, output)
            except Exception as exc:
                logger.debug("Failed to record tool result in context: %s", exc, exc_info=exc)
            
            return output
        
        return run_agent_with_context
    
    async def _create_mcp_servers(self, mcp_tool_names: List[str]) -> List[Any]:
        """Create and connect MCP servers using the Agents SDK."""
        servers: list[Any] = []
        unavailable: list[str] = []
        for name in mcp_tool_names:
            try:
                server = await self._get_mcp_server(name)
                if server is not None:
                    servers.append(server)
                else:
                    unavailable.append(name)
            except Exception:
                unavailable.append(name)
        if unavailable:
            try:
                self.context_manager.set_metadata("mcp_unavailable", unavailable)
            except Exception as exc:
                logger.warning("Failed to store MCP availability metadata: %s", exc, exc_info=exc)
        return servers

    async def _get_mcp_server(self, tool_name: str) -> Optional[Any]:
        """Get or create an SDK-based MCP server (MCPServerStdio)."""
        if tool_name in self._mcp_servers:
            return self._mcp_servers[tool_name]

        tool_config = self.config.get_tool(tool_name)
        if tool_config.type != "mcp":
            return None

        server_command = tool_config.server_command or []
        if not server_command:
            return None

        command = server_command[0]
        args = list(server_command[1:])
        # Make npx non-interactive
        if command.lower() in ("npx", "npx.cmd") and "-y" not in args:
            args.insert(0, "-y")

        env = tool_config.env_vars or {}
        cwd = self.config.get_working_directory()

        server = MCPServerStdio(
            params={
                "command": command,
                "args": args,
                "env": env,
                "cwd": cwd,
            },
            cache_tools_list=True,
            name=tool_name,
        )

        await server.connect()
        self._mcp_servers[tool_name] = server
        return server
    
    def _extract_tools_used(self, result: Any) -> List[str]:
        """
        Extract the names of tools invoked during the run.
        
        The SDK `Runner.run` returns an object that (as of v0.2.x) contains
        a ``tool_calls`` attribute – a list of ``ToolCall`` objects with a
        ``name`` field.  If the attribute is missing we fall back to an empty
        list to keep the system robust.
        """
        try:
            if hasattr(result, "tool_calls"):
                tool_calls = getattr(result, "tool_calls")
                names = []
                for call in tool_calls:
                    try:
                        name = getattr(call, "name", None)
                        if name:
                            names.append(str(name))
                    except Exception:
                        continue
                return names
        except Exception as e:
            logger.debug("Failed to extract tool call metadata: %s", e, exc_info=e)
        return []
    
    def _extract_context_id_from_text(self, text: Optional[str]) -> Optional[str]:
        """Extract context identifier (ctx-XXXXXXXX) from arbitrary text."""
        if not text:
            return None
        match = CONTEXT_ID_REGEX.search(text)
        return match.group(0).lower() if match else None
    
    # Context management methods
    def add_to_context(self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
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
        for session in self._agent_sessions.values():
            try:
                await session.clear_session()
            except asyncio.CancelledError:
                logger.debug("Agent session cleanup cancelled", exc_info=True)
            except Exception as e:
                logger.warning("Failed to cleanup agent session: %s", e, exc_info=e)
        
        # Clear caches
        self.clear_cache()
        self._mcp_servers.clear()
        self._agent_sessions.clear()
        

    
    # Fallback: заглушка для ручного парсинга tool call из текста ответа
    async def _execute_first_tool_call_in_text(self, output: str) -> Optional[str]:
        """Safely ignore manual tool call parsing until fully implemented."""
        return None
