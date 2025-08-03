"""
Enterprise Agent Factory with caching, tracing, and error handling.
"""

import asyncio
import time
import sys
import os
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from openai import AsyncOpenAI

# Set environment variables to avoid TensorFlow issues
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'

# Simple import blocking approach
import builtins
original_import = builtins.__import__

def patched_import(name, *args, **kwargs):
    if 'tensorflow' in name and 'contrib' in name:
        raise ImportError(f"Blocked TensorFlow contrib import: {name}")
    if name == 'gym':  # Also block gym which causes issues
        raise ImportError(f"Blocked gym import: {name}")
    return original_import(name, *args, **kwargs)

# Apply the patch temporarily
builtins.__import__ = patched_import

try:
    from agents import Agent, OpenAIChatCompletionsModel, set_tracing_disabled, Runner, function_tool, RunContextWrapper, SQLiteSession
    from agents.items import ItemHelpers
finally:
    # Restore original import
    builtins.__import__ = original_import

from .config import Config
from .context import ContextManager
from schemas import AgentConfig, AgentExecution
from tools import get_tools_by_names, MCPClient
from utils.exceptions import AgentError, ConfigError
from utils.logger import Logger

from utils.unified_logger import (
    log_agent_start, log_agent_end, log_agent_error, 
    log_prompt, log_tool_call, set_current_agent, clear_current_agent,
    get_unified_logger
)

load_dotenv()
logger = Logger(__name__)


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
        # Disable agents SDK tracing by default
        set_tracing_disabled(True)
        
        self.config = config or Config()
        if working_directory:
            self.config.set_working_directory(working_directory)
        
        # Initialize managers
        self.context_manager = ContextManager(
            max_history=self.config.get_max_history(),
            persist_path="logs/context.json" if self.config.is_debug() else None
        )
        
        # Caches
        self._agent_cache: Dict[str, Agent] = {}
        self._tool_cache: Dict[str, List[Any]] = {}
        self._mcp_clients: Dict[str, MCPClient] = {}
        
        # Session management for agent memory
        self._agent_sessions: Dict[str, SQLiteSession] = {}
        
        logger.info("Agent Factory initialized")
    
    def _get_agent_session(self, agent_key: str) -> SQLiteSession:
        """Get or create session for an agent to maintain memory."""
        if agent_key not in self._agent_sessions:
            # Create a stable session for each agent (without timestamp)
            session_id = f"agent_{agent_key}"
            self._agent_sessions[agent_key] = SQLiteSession(session_id, "logs/agent_sessions.db")
            logger.debug(f"Created new session for agent {agent_key}: {session_id}")
        
        return self._agent_sessions[agent_key]
    
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
            logger.debug(f"Using cached agent: {agent_key}")
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
            
            # Create model
            model = OpenAIChatCompletionsModel(
                model=model_config.name,
                openai_client=client
            )
            
            # Build instructions with context
            instructions = self._build_agent_instructions(agent_key, context_path)
            
            # Get tools
            tools = await self._get_agent_tools(agent_config)
            
            # Create agent
            agent = Agent(
                name=agent_config.name,
                instructions=instructions,
                model=model,
                tools=tools
            )
            
            # Create and attach session to agent for memory
            session = self._get_agent_session(agent_key)
            agent._session = session  # Attach session to agent
            logger.debug(f"Attached session {session.session_id} to agent {agent_key}")
            
            # Cache agent
            self._agent_cache[cache_key] = agent
            
            logger.log_agent_creation(agent_key, agent_config.name)
            logger.info(
                f"Created agent '{agent_key}'",
                agent_name=agent_config.name,
                model=model_config.name,
                provider=model_config.provider,
                tools_count=len(tools)
            )
            
            return agent
            
        except Exception as e:
            error_msg = f"Failed to create agent '{agent_key}': {e}"
            logger.error(error_msg, agent_key=agent_key, error_type=type(e).__name__)
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
            # Set current agent for tool logging
            set_current_agent(agent_key)
            
            # Create agent
            agent = await self.create_agent(agent_key, context_path)
            
            # Add user message to context
            self.context_manager.add_message("user", message)
            
            # Начинаем детальное логирование
            log_agent_start(agent.name, message)
            
            # Логируем промпт агента
            agent_instructions = self._build_agent_instructions(agent_key, context_path)
            log_prompt(agent.name, "full", agent_instructions)
            
            # Log start (legacy)
            logger.log_agent_start(agent.name, message)
            
            # Run agent with max_turns configuration and timeout
            max_turns = self.config.get_max_turns()
            timeout_seconds = self.config.get_agent_timeout()
            
            logger.info(f"Starting agent execution with max_turns={max_turns}, timeout={timeout_seconds}s")
            
            try:
                # Get session from the agent (attached during creation)
                session = getattr(agent, '_session', None)
                if not session:
                    # Fallback: create session if not attached
                    session = self._get_agent_session(agent_key)
                
                if stream:
                    # Handle streaming (simplified for now)
                    result = await asyncio.wait_for(
                        Runner.run(agent, message, max_turns=max_turns, session=session),
                        timeout=timeout_seconds
                    )
                else:
                    result = await asyncio.wait_for(
                        Runner.run(agent, message, max_turns=max_turns, session=session),
                        timeout=timeout_seconds
                    )
            except asyncio.TimeoutError:
                logger.error(f"Agent execution timed out after {timeout_seconds} seconds")
                raise AgentError(f"Agent execution timed out after {timeout_seconds} seconds")
            except Exception as e:
                raise AgentError(f"Agent execution failed: {e}") from e
            
            # Process result
            output = result.final_output if hasattr(result, 'final_output') else str(result)
            
            # Add assistant response to context
            self.context_manager.add_message("assistant", output)
            
            # Update execution record
            execution.end_time = time.time()
            execution.output = output
            execution.tools_used = self._extract_tools_used(result)
            
            # Завершаем детальное логирование
            duration = execution.end_time - execution.start_time
            log_agent_end(agent.name, output, duration)
            
            # Log completion (legacy)
            logger.log_agent_end(agent.name, output, duration)
            
            # Add to execution history
            self.context_manager.add_execution(execution)
            
            # Clear current agent
            clear_current_agent()
            
            return output
            
        except Exception as e:
            execution.end_time = time.time()
            execution.error = str(e)
            
            # Логируем ошибку в детальном логгере
            log_agent_error(agent.name, e)
            
            # Log error (legacy)
            logger.log_agent_error(agent_key, e)
            self.context_manager.add_execution(execution)
            
            # Clear current agent on error too
            clear_current_agent()
            
            raise AgentError(f"Agent execution failed: {e}") from e
    
    def _build_agent_instructions(self, agent_key: str, context_path: Optional[str] = None) -> str:
        """Build complete agent instructions with context."""
        base_instructions = self.config.build_agent_prompt(agent_key)
        
        # Add path context
        path_context = self._build_path_context(context_path)
        
        # Add conversation context
        conversation_context = self.context_manager.get_conversation_context()
        
        # Combine all parts
        parts = [base_instructions]
        
        if path_context:
            parts.append(path_context)
        
        if conversation_context:
            parts.append(conversation_context)
        
        return "\\n\\n".join(parts)
    
    def _build_path_context(self, context_path: Optional[str] = None) -> str:
        """Build path context information."""
        working_dir = self.config.get_working_directory()
        config_dir = self.config.get_config_directory()
        
        context_parts = [
            "📁 Информация о путях:",
            f"   Рабочая директория: {working_dir}",
            f"   Директория конфигурации: {config_dir}"
        ]
        
        if context_path:
            absolute_path = self.config.get_absolute_path(context_path)
            context_parts.extend([
                f"   Контекстный путь: {context_path}",
                f"   Абсолютный контекстный путь: {absolute_path}"
            ])
        
        context_parts.extend([
            "",
            "💡 Используй эти пути для работы с файлами и директориями."
        ])
        
        return "\\n".join(context_parts)
    
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
                logger.warning(f"Tool '{tool_name}' not found in configuration")
        
        # Add function tools
        if function_tools:
            try:
                func_tools = get_tools_by_names(function_tools)
                tools.extend(func_tools)
                logger.debug(f"Added {len(func_tools)} function tools")
            except Exception as e:
                logger.error(f"Failed to load function tools: {e}")
        
        # Add agent tools
        if agent_tools:
            try:
                agent_tool_instances = await self._create_agent_tools(agent_tools)
                tools.extend(agent_tool_instances)
                logger.debug(f"Added {len(agent_tool_instances)} agent tools")
            except Exception as e:
                logger.error(f"Failed to create agent tools: {e}")
        
        # Add MCP tools
        if mcp_tools and (agent_config.mcp_enabled or self.config.is_mcp_enabled()):
            try:
                mcp_tool_instances = await self._get_mcp_tools(mcp_tools)
                tools.extend(mcp_tool_instances)
                logger.debug(f"Added {len(mcp_tool_instances)} MCP tools")
            except Exception as e:
                logger.error(f"Failed to get MCP tools: {e}")
        
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
                context_strategy = getattr(tool_config, 'context_strategy', 'minimal')
                context_depth = getattr(tool_config, 'context_depth', 5)
                include_tool_history = getattr(tool_config, 'include_tool_history', False)
                
                # Create context-aware tool
                agent_tool = self._create_context_aware_agent_tool(
                    sub_agent=sub_agent,
                    tool_name=tool_name,
                    tool_description=tool_description,
                    context_strategy=context_strategy,
                    context_depth=context_depth,
                    include_tool_history=include_tool_history
                )
                
                # Wrap for logging
                wrapped_tool = self._wrap_agent_tool(agent_tool, sub_agent.name)
                tools.append(wrapped_tool)
                
            except Exception as e:
                logger.error(f"Failed to create agent tool '{agent_key}': {e}")
        
        return tools
    
    def _wrap_agent_tool(self, agent_tool: Any, agent_name: str) -> Any:
        """Wrap agent tool for proper logging and execution tracking."""
        if not hasattr(agent_tool, 'on_invoke_tool'):
            return agent_tool
        
        original_invoke = agent_tool.on_invoke_tool
        
        async def wrapped_invoke_tool(tool_context, tool_call_arguments):
            start_time = time.time()
            # Безопасно преобразуем аргументы в строку
            if isinstance(tool_call_arguments, dict):
                input_data = str(tool_call_arguments)
            elif isinstance(tool_call_arguments, list):
                input_data = str(tool_call_arguments)
            else:
                input_data = str(tool_call_arguments)
            
            execution = AgentExecution(
                agent_name=agent_name,
                start_time=str(start_time),
                input_message=input_data
            )
            
            try:
                # Set current agent for tool logging
                set_current_agent(agent_name)
                
                # Начинаем детальное логирование
                execution_id = log_agent_start(agent_name, input_data)
                
                # Логируем вызов инструмента
                log_tool_call("call_agent", {"input": input_data})
                
                logger.log_agent_tool_start(agent_name, "call_agent", input_data)
                logger.log_agent_start(agent_name, input_data)
                
                # Call original function
                result = original_invoke(tool_context, tool_call_arguments)
                if hasattr(result, '__await__'):
                    result = await result
                
                execution.end_time = time.time()
                # Безопасно преобразуем результат в строку
                if isinstance(result, str):
                    execution.output = result
                else:
                    execution.output = str(result)
                
                duration = execution.end_time - execution.start_time
                
                # Завершаем детальное логирование
                log_agent_end(agent_name, str(result), duration)
                
                logger.log_agent_end(agent_name, str(result), duration)
                
                self.context_manager.add_execution(execution)
                
                # Clear current agent
                clear_current_agent()
                
                return result
                
            except Exception as e:
                execution.end_time = time.time()
                execution.error = str(e)
                
                # Логируем ошибку в детальном логгере
                log_agent_error(agent_name, e)
                
                logger.log_agent_error(agent_name, e)
                self.context_manager.add_execution(execution)
                
                # Clear current agent on error too
                clear_current_agent()
                
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
        
        @function_tool(
            name_override=tool_name,
            description_override=tool_description,
        )
        async def run_agent_with_context(context: RunContextWrapper, input: str) -> str:
            from agents import Runner
            
            # Add this tool call to context BEFORE getting context
            self.context_manager.add_message("user", f"Tool call to {tool_name}: {input}")
            
            # Get context based on strategy - use the factory's context manager
            enhanced_input = self.context_manager.get_context_for_agent_tool(
                strategy=context_strategy,
                depth=context_depth,
                include_tools=include_tool_history,
                task_input=input
            )
            
            # Get session from the sub-agent (attached during creation)
            session = getattr(sub_agent, '_session', None)
            if not session:
                # Fallback: create session if not attached
                agent_key = tool_name.replace("call_", "")
                session = self._get_agent_session(agent_key)
                logger.warning(f"Session not found on agent {agent_key}, created new session: {session.session_id}")
            else:
                logger.debug(f"Using existing session {session.session_id} for agent {sub_agent.name}")
            
            # Run the sub-agent with enhanced input and session
            output = await Runner.run(
                starting_agent=sub_agent,
                input=enhanced_input,
                context=context.context,
                session=session,
            )
            
            # Add tool response to context
            output_text = ItemHelpers.text_message_outputs(output.new_items)
            self.context_manager.add_message("assistant", f"Tool response from {tool_name}: {output_text}")
            
            return output_text
        
        return run_agent_with_context
    
    async def _get_mcp_tools(self, mcp_tool_names: List[str]) -> List[Any]:
        """Get MCP tools with connection management."""
        tools = []
        
        for tool_name in mcp_tool_names:
            try:
                mcp_client = await self._get_mcp_client(tool_name)
                if mcp_client:
                    mcp_tools = await mcp_client.get_tools()
                    tools.extend(mcp_tools)
                    
            except Exception as e:
                logger.error(f"Failed to get MCP tools for '{tool_name}': {e}")
        
        return tools
    
    async def _get_mcp_client(self, tool_name: str) -> Optional[MCPClient]:
        """Get or create MCP client."""
        if tool_name in self._mcp_clients:
            return self._mcp_clients[tool_name]
        
        try:
            tool_config = self.config.get_tool(tool_name)
            
            if tool_config.type != "mcp":
                return None
            
            # Create MCP client
            mcp_client = MCPClient(
                name=tool_name,
                server_command=tool_config.server_command or [],
                env_vars=tool_config.env_vars or {}
            )
            
            await mcp_client.connect()
            self._mcp_clients[tool_name] = mcp_client
            
            logger.log_mcp_connection(tool_name, "connected")
            
            return mcp_client
            
        except Exception as e:
            logger.log_mcp_connection(tool_name, "failed")
            logger.error(f"MCP client creation failed for '{tool_name}': {e}")
            return None
    
    def _extract_tools_used(self, result: Any) -> List[str]:
        """Extract list of tools used from result."""
        # This would need to be implemented based on the actual result structure
        # from the agents SDK
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
        logger.info("Agent factory caches cleared")
    
    async def cleanup(self) -> None:
        """Cleanup resources."""
        # Disconnect MCP clients
        for mcp_client in self._mcp_clients.values():
            try:
                await mcp_client.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting MCP client: {e}")
        
        # Clear agent sessions
        for session in self._agent_sessions.values():
            try:
                await session.clear_session()
            except Exception as e:
                logger.error(f"Error clearing agent session: {e}")
        
        # Clear caches
        self.clear_cache()
        self._mcp_clients.clear()
        self._agent_sessions.clear()
        
        logger.info("Agent factory cleanup completed")