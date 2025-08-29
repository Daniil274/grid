"""
Agent Factory with caching, tracing, and error handling.
"""

import asyncio
import time
import logging
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from openai import AsyncOpenAI

# OpenAI Agents SDK imports
from agents import Agent, OpenAIChatCompletionsModel, set_tracing_disabled, Runner, function_tool, RunContextWrapper, SQLiteSession
from agents.items import ItemHelpers
from agents.mcp import MCPServerStdio

from .config import Config
from .context import ContextManager
from schemas import AgentConfig, AgentExecution
from tools import get_tools_by_names
from utils.exceptions import AgentError, ConfigError
from core.tracing_config import get_tracing_config
from core.verification_agents import hallucination_guardrail

# Создаем logger для модуля
logger = logging.getLogger(__name__)

import json
import re
import sys

load_dotenv()
tracing_config = get_tracing_config()


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
    
    def __init__(self, config: Optional[Config] = None, working_directory: Optional[str] = None):
        """
        Initialize Agent Factory.
        
        Args:
            config: Configuration instance (creates default if None)
            working_directory: Working directory override
        """
        # Configure tracing instead of logging
        tracing_config.configure_console_tracing("INFO")
        tracing_config.apply()
        
        # Set up minimal logging for agents SDK to avoid spam
        agents_logger = logging.getLogger("openai.agents")
        agents_logger.setLevel(logging.WARNING)
        
        self.config = config or Config()
        if working_directory:
            self.config.set_working_directory(working_directory)
        
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
        
        # Session management for agent memory
        self._agent_sessions: Dict[str, SQLiteSession] = {}
        # Track emitted warnings to avoid log spam (e.g., Responses API fallbacks)
        self._responses_warning_keys: set[str] = set()
        

    
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
    
    def _get_agent_session(self, agent_key: str) -> SQLiteSession:
        """Get or create session for an agent to maintain memory."""
        if agent_key not in self._agent_sessions:
            # Create an in-memory session per agent to avoid persistence across restarts
            session_id = f"agent_{agent_key}"
            # Use default ':memory:' DB path to keep session ephemeral for the current process
            self._agent_sessions[agent_key] = SQLiteSession(session_id)

        
        return self._agent_sessions[agent_key]
    
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
            else:
                pass
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
            else:
                pass
            
            if model is None:
                model = OpenAIChatCompletionsModel(
                    model=model_config.name,
                    openai_client=client
                )
            
            # Build instructions with context
            instructions = self._build_agent_instructions(agent_key, context_path)
            
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

            # Подготавливаем guardrails для проверки галлюцинаций
            output_guardrails = []
            
            # Проверяем, нужно ли включить проверку галлюцинаций
            if (self.config.config.settings.verify_hallucinations and 
                getattr(agent_config, 'verify_output', False)):
                output_guardrails.append(hallucination_guardrail)
                logger.info(f"Включена проверка галлюцинаций для агента {agent_key}")
            
            # Create agent
            agent = Agent(
                name=agent_config.name,
                instructions=instructions,
                model=model,
                tools=tools,
                mcp_servers=mcp_servers_list,
                output_guardrails=output_guardrails
            )
            
            # Сохраняем конфигурацию агента для доступа в guardrails
            agent._agent_config = agent_config
            # Сохраняем общую конфигурацию для доступа в guardrails
            agent._config = self.config
            
            # Create and attach session to agent for memory
            session = self._get_agent_session(agent_key)
            agent._session = session  # Attach session to agent

            
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
        stream: bool = False
    ) -> str:
        """
        Run agent with message and context management.
        
        Args:
            agent_key: Agent to run
            message: Input message
            context_path: Optional context path
            stream: Whether to stream response
            
        Returns:
            Agent response
        """
        start_time = time.time()
        execution = AgentExecution(
            agent_name=agent_key,
            start_time=str(start_time),
            input_message=message
        )
        
        try:

            
            # Create agent
            agent = await self.create_agent(agent_key, context_path)
            

            
            # Не добавляем инструкции агента в диалог; сохраняем в metadata для служебного использования
            if not self.context_manager.get_conversation_context():
                agent_instructions = self._build_agent_instructions(agent_key, context_path)
                self.context_manager.set_metadata("agent_instructions", agent_instructions)
            
            # Добавляем сообщение в контекст для текущей сессии
            self.context_manager.add_message("user", message)
            

            agent_instructions = self._build_agent_instructions(agent_key, context_path)
            
            # Run agent with max_turns configuration and timeout
            max_turns = self.config.get_max_turns()
            timeout_seconds = self.config.get_agent_timeout()
            
            # Prepare session
            session = getattr(agent, '_session', None)
            if not session:
                session = self._get_agent_session(agent_key)
            if stream:
                # Streaming режим: прозрачно показываем tool/MCP вызовы
                from agents import Runner, RunItemStreamEvent, RawResponsesStreamEvent
                result_output: Optional[str] = None
                try:
                    run_result_streaming = Runner.run_streamed(
                        agent,
                        message,
                        context=self.context_manager.get_conversation_context(),
                        max_turns=max_turns,
                        session=session,
                    )
                    # Стримим события и подсвечиваем tool calls/outputs
                    # Буфер для надёжного накопления текстовых дельт стрима
                    streaming_text_parts: List[str] = []
                    async for event in run_result_streaming.stream_events():
                        try:
                            # Отображение инструментов
                            if isinstance(event, RunItemStreamEvent):
                                name = getattr(event, 'name', '')
                                item = getattr(event, 'item', None)
                                if name == "tool_called" and item is not None:
                                    raw_item = getattr(item, 'raw_item', None)
                                    tool_name = getattr(raw_item, 'name', None) or getattr(raw_item, 'type', None) or "tool"
                                    # Попробуем достать аргументы (для функций/МСР)
                                    arguments = getattr(raw_item, 'arguments', None)
                                    # Преобразуем в строку с усечением (табулированно, не JSON)
                                    try:
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
                                        else:
                                            args_str = str(arguments) if arguments is not None else ""
                                    except Exception:
                                        args_str = str(arguments) if arguments is not None else ""
                                    
                                    # Определяем MCP инструменты по именам
                                    mcp_tools = {
                                        "sequentialthinking": "sequential_thinking",
                                        "read_text_file": "filesystem", 
                                        "write_text_file": "filesystem",
                                        "list_directory": "filesystem",
                                        "create_directory": "filesystem",
                                        "delete_file": "filesystem",
                                        "move_file": "filesystem",
                                        "git_status": "git",
                                        "git_log": "git", 
                                        "git_diff": "git",
                                        "git_add": "git",
                                        "git_commit": "git",
                                        "git_push": "git",
                                        "git_pull": "git",
                                        "git_set_working_dir": "git",
                                        "git_show": "git"
                                    }
                                    
                                    server_label = getattr(raw_item, 'server_label', None)
                                    if not server_label and tool_name in mcp_tools:
                                        server_label = mcp_tools[tool_name]
                                    
                                    args_dict = {"args": args_str}
                                    if server_label:
                                        args_dict["server_label"] = server_label
                                        # Добавляем префикс для MCP инструментов в логах
                                        tool_display_name = f"MCP:{server_label}.{tool_name}"
                                    else:
                                        tool_display_name = tool_name
                                    
                                    # Форматируем аргументы для удобного чтения (табулированно, не JSON)
                                    def format_arguments_readable(arguments):
                                        if arguments is None:
                                            return ""
                                        if isinstance(arguments, str):
                                            return arguments
                                        if isinstance(arguments, dict):
                                            parts = []
                                            for key, value in arguments.items():
                                                if isinstance(value, str) and len(value) > 60:
                                                    parts.append(f"{key}=...({len(value)} символов)")
                                                elif isinstance(value, (dict, list)):
                                                    parts.append(f"{key}={type(value).__name__}({len(value)})")
                                                else:
                                                    parts.append(f"{key}={value}")
                                            return " | ".join(parts)
                                        return str(arguments)
                                    
                                    args_str = format_arguments_readable(arguments)
                                    

                                elif name == "tool_output" and item is not None:
                                    raw_item = getattr(item, 'raw_item', None)
                                    tool_name = getattr(raw_item, 'name', None) or getattr(raw_item, 'type', None) or "tool"
                                    output_val = getattr(item, 'output', '')
                                    
                                    # Определяем MCP инструменты по именам (используем тот же словарь)
                                    mcp_tools = {
                                        "sequentialthinking": "sequential_thinking",
                                        "read_text_file": "filesystem", 
                                        "write_text_file": "filesystem",
                                        "list_directory": "filesystem",
                                        "create_directory": "filesystem",
                                        "delete_file": "filesystem",
                                        "move_file": "filesystem",
                                        "git_status": "git",
                                        "git_log": "git", 
                                        "git_diff": "git",
                                        "git_add": "git",
                                        "git_commit": "git",
                                        "git_push": "git",
                                        "git_pull": "git",
                                        "git_set_working_dir": "git",
                                        "git_show": "git"
                                    }
                                    
                                    server_label = getattr(raw_item, 'server_label', None)
                                    if not server_label and tool_name in mcp_tools:
                                        server_label = mcp_tools[tool_name]
                                    
                                    # Добавляем префикс для MCP инструментов
                                    if server_label:
                                        tool_display_name = f"MCP:{server_label}.{tool_name}"
                                    else:
                                        tool_display_name = tool_name
                                    

                            elif isinstance(event, RawResponsesStreamEvent):
                                # Отображение текстовых дельт в реальном времени
                                try:
                                    # Пробуем извлечь текст из события
                                    content = None
                                    if hasattr(event, 'content') and event.content:
                                        content = event.content
                                    elif hasattr(event, 'delta') and event.delta:
                                        content = event.delta
                                    elif hasattr(event, 'text') and event.text:
                                        content = event.text
                                    elif hasattr(event, 'data') and event.data:
                                        # event.data содержит объекты типа ResponseTextDeltaEvent
                                        if hasattr(event.data, 'delta'):
                                            content = event.data.delta
                                        elif hasattr(event.data, 'content'):
                                            content = event.data.content
                                        elif hasattr(event.data, 'text'):
                                            content = event.data.text
                                        elif isinstance(event.data, dict):
                                            content = event.data.get('content') or event.data.get('delta') or event.data.get('text')
                                        elif hasattr(event.data, 'type') and event.data.type == 'response.output_text.delta':
                                            # Для ResponseTextDeltaEvent извлекаем delta
                                            if hasattr(event.data, 'delta'):
                                                content = event.data.delta
                                    
                                    if content and isinstance(content, str) and content.strip():
                                        # Выводим текст без новой строки для плавного стриминга
                                        print(content, end='', flush=True)
                                        # Накопление контента в буфер для случая отсутствия final_output
                                        try:
                                            streaming_text_parts.append(content)
                                        except Exception:
                                            # Никогда не роняем стрим из-за проблем с буферизацией
                                            pass
                                except Exception as e:
                                    # Игнорируем ошибки в отображении стриминга, но логируем их
                                    print(f"[DEBUG] Error in streaming: {e}", file=sys.stderr)
                                    pass
                        except Exception:
                            # Никогда не роняем выполнение из-за отображения логов
                            pass
                    # После завершения стрима забираем финальный вывод
                    result_output = run_result_streaming.final_output if run_result_streaming.final_output is not None else ""
                    # Если финального вывода нет, используем накопленный текст стрима
                    try:
                        if (not result_output or str(result_output).strip() == "") and streaming_text_parts:
                            buffered_text = "".join(streaming_text_parts).strip()
                            if buffered_text:
                                result_output = buffered_text
                    except Exception:
                        # В случае ошибки оставляем result_output как есть — дальнейшая логика подставит фоллбек
                        pass
                except asyncio.TimeoutError:
                    logger.error(f"Agent execution timed out after {timeout_seconds} seconds")
                    raise AgentError(f"Agent execution timed out after {timeout_seconds} seconds")
                except Exception as e:
                    # Проверяем, не является ли это исключением guardrail
                    if "OutputGuardrailTripwireTriggered" in str(type(e).__name__):
                        logger.warning(f"Ответ агента {agent_key} заблокирован guardrail (streaming): {e}")
                        # Возвращаем сообщение об ошибке вместо исключения
                        result_output = "❌ Ответ отклонён: обнаружены признаки галлюцинации или недостоверной информации. Пожалуйста, попробуйте переформулировать запрос."
                    else:
                        raise AgentError(f"Agent execution failed: {e}") from e
                result = result_output
            else:
                try:
                    # Запускаем агента и получаем RunResult объект
                    from agents import Runner
                    result = await asyncio.wait_for(
                        Runner.run(agent, message, max_turns=max_turns, session=session),
                        timeout=timeout_seconds
                    )

                except asyncio.TimeoutError:
                    raise AgentError(f"Agent execution timed out after {timeout_seconds} seconds")
                except Exception as e:
                    # Проверяем, не является ли это исключением guardrail
                    if "OutputGuardrailTripwireTriggered" in str(type(e).__name__):
                        logger.warning(f"Ответ агента {agent_key} заблокирован guardrail: {e}")
                        # Возвращаем сообщение об ошибке вместо исключения
                        return "❌ Ответ отклонён: обнаружены признаки галлюцинации или недостоверной информации. Пожалуйста, попробуйте переформулировать запрос."
                    else:
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
            self.context_manager.add_message("assistant", output)
            
            # Принудительный разбор и выполнение первого tool call из текстового ответа
            try:
                manual_tool_result = await self._execute_first_tool_call_in_text(output)
                if manual_tool_result is not None:
                    output = manual_tool_result
            except Exception as e:
                pass
            
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
            
            duration = execution.end_time - start_time
            
            self.context_manager.add_execution(execution)
            
            return output
            
        except Exception as e:
            execution.end_time = time.time()
            execution.error = str(e)
            
            self.context_manager.add_execution(execution)
            
            raise
    
    def _build_agent_instructions(self, agent_key: str, context_path: Optional[str] = None) -> str:
        """Build complete agent instructions with context."""
        base_instructions = self.config.build_agent_prompt(agent_key)
        
        # Add path context
        path_context = self._build_path_context(context_path)
        
        # Добавляем контекст текущей сессии (но не между разными запусками)
        conversation_context = self.context_manager.get_conversation_context()
        
        # Combine all parts
        parts = [base_instructions]
        
        if path_context:
            parts.append(path_context)
        
        # Добавляем контекст текущей сессии
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
                    
            except ConfigError:
                            pass
        
        # Add function tools
        if function_tools:
            try:
                func_tools = get_tools_by_names(function_tools)
                tools.extend(func_tools)

            except Exception as e:
                            pass
        
        # Add agent tools
        if agent_tools:
            try:
                agent_tool_instances = await self._create_agent_tools(agent_tools)
                tools.extend(agent_tool_instances)

            except Exception as e:
                            pass
 
        # Добавим алиасы каналов только для function tools (не для MCP)
        # MCP инструменты обрабатываются SDK отдельно и не нуждаются в алиасах
        if tools and not mcp_tools:  # Только если нет MCP инструментов
            try:
                channel_suffixes = ("_commentary", "_tool", "_final")
                alias_count = 0
                # Собираем снимок списка, чтобы не итерироваться по растущему
                base_tools_snapshot = list(tools)
                for base_tool in base_tools_snapshot:
                    tool_name = getattr(base_tool, 'name', None)
                    on_invoke = getattr(base_tool, 'on_invoke_tool', None)
                    if not tool_name or not callable(on_invoke):
                        continue
                    for suffix in channel_suffixes:
                        alias_name = f"{tool_name}{suffix}"
                        # Создаем лёгкий прокси-инструмент, перенаправляющий вызов на исходный
                        @function_tool(name_override=alias_name, description_override=getattr(base_tool, 'description', '') or f"Alias of {tool_name}")
                        async def alias_tool_proxy(tool_context: RunContextWrapper, **kwargs):
                            # Передаем исходные аргументы как есть
                            return await on_invoke(tool_context, kwargs if kwargs else {})
                        tools.append(alias_tool_proxy)
                        alias_count += 1
                if alias_count:
                    pass
            except Exception as e:
                pass

        # NOTE: MCP tools are no longer added as function tools. They are exposed to the model
        # via Agent.mcp_servers using the SDK integration. We only record unavailability metadata.
        if mcp_tools:
            if not (agent_config.mcp_enabled or self.config.is_mcp_enabled()):
                        pass
        
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
                pass
        
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
            input_data = str(normalized_args)
            
            
            execution = AgentExecution(
                agent_name=agent_name,
                start_time=str(start_time),
                input_message=input_data
            )
            
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
                    execution.output = result
                else:
                    execution.output = str(result)
                
                duration = execution.end_time - execution.start_time
                
                self.context_manager.add_execution(execution)
                
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
            from agents import Runner
            
            # Подготавливаем человекочитаемый контекст для подагента
            # На этом уровне input должен быть строкой, т.к. нормализация прошла в `wrapped_invoke_tool`
            if not isinstance(input, str) or not input.strip():
                return f"❌ Пустой ввод для инструмента '{tool_name}'. Передайте непустой 'input' (string)."
            
            raw_input = input.strip()
            
            enhanced_input = self.context_manager.get_context_for_agent_tool(
                strategy=context_strategy,
                depth=context_depth,
                include_tools=include_tool_history,
                task_input=raw_input
            )
            
            # Get session from the sub-agent (attached during creation)
            session = getattr(sub_agent, '_session', None)
            if not session:
                # Fallback: create session if not attached
                agent_key = tool_name.replace("call_", "")
                session = self._get_agent_session(agent_key)
            else:
                pass
            
            # Run the sub-agent with enhanced input and session
            output = await Runner.run(
                starting_agent=sub_agent,
                input=enhanced_input,
                context=context.context,
                session=session,
                max_turns=self.config.get_max_turns(),
            )
            
            # Запишем результат как сообщение ассистента, чтобы главный агент мог обсуждать и давать правки
            try:
                self.context_manager.add_tool_result_as_message(tool_name, output)
            except Exception:
                pass
            
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
            except Exception:
                pass
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
            pass
        return []
    
    # Context management methods
    def add_to_context(self, role: str, content: str) -> None:
        """Add message to conversation context."""
        self.context_manager.add_message(role, content)
    
    def clear_context(self) -> None:
        """Clear conversation context."""
        self.context_manager.clear_history()
    
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
                pass
            except Exception as e:
                pass
        
        # Clear agent sessions
        for session in self._agent_sessions.values():
            try:
                await session.clear_session()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                pass
        
        # Clear caches
        self.clear_cache()
        self._mcp_servers.clear()
        self._agent_sessions.clear()
        

    
    # Fallback: заглушка для ручного парсинга tool call из текста ответа
    async def _execute_first_tool_call_in_text(self, output: str) -> Optional[str]:
        """Safely ignore manual tool call parsing until fully implemented."""
        return None