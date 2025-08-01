"""
Фабрика агентов с полной поддержкой YAML конфигурации и MCP.
"""

import asyncio
import os
from typing import List, Any, Optional
from dotenv import load_dotenv
from openai import AsyncOpenAI
from agents import Agent, OpenAIChatCompletionsModel, set_tracing_disabled

from config_loader import config, AgentConfig
from tools import get_tools_by_names
from mcp_client import MCPClient

load_dotenv()

class AgentFactory:
    """Фабрика для создания агентов из YAML конфигурации."""
    
    def __init__(self, working_directory: Optional[str] = None):
        """Инициализация фабрики."""
        set_tracing_disabled(True)
        self.mcp_clients = {}
        self.agent_cache = {}
        self.agent_tools_cache = {}
        
        # Устанавливаем рабочую директорию если передана
        if working_directory:
            config.set_working_directory(working_directory)
    
    async def create_agent(self, agent_key: str, context_path: Optional[str] = None) -> Agent:
        """Создает агента по его ключу из конфигурации."""
        agent_config = config.get_agent(agent_key)
        model_config = config.get_model(agent_config.model)
        provider_config = config.get_provider(model_config.provider)
        
        # Проверяем API ключ
        api_key = config.get_api_key(model_config.provider)
        if not api_key:
            raise ValueError(f"❌ API ключ не найден для {provider_config.name}. "
                           f"Установите переменную {provider_config.api_key_env}")
        
        # Создаем клиента
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=provider_config.base_url,
            timeout=provider_config.timeout,
            max_retries=provider_config.max_retries
        )
        
        # Создаем модель (параметры temperature и max_tokens не поддерживаются в конструкторе)
        model = OpenAIChatCompletionsModel(
            model=model_config.name,
            openai_client=client
        )
        
        # Строим промпт с учетом инструментов и путей
        instructions = self._build_agent_prompt_with_paths(agent_key, context_path)
        
        # Логгируем промпт агента
        from utils.logger import log_agent_prompt
        log_agent_prompt(agent_config.name, instructions)
        
        # Получаем инструменты
        tools = await self._get_agent_tools(agent_config)
        
        # Создаем агента
        agent = Agent(
            name=agent_config.name,
            instructions=instructions,
            model=model,
            tools=tools
        )
        
        return agent
    
    def _build_agent_prompt_with_paths(self, agent_key: str, context_path: Optional[str] = None) -> str:
        """Строит промпт агента с информацией о путях."""
        base_instructions = config.build_agent_prompt(agent_key)
        
        # Добавляем информацию о путях
        path_info = self._get_path_context(context_path)
        
        if path_info:
            full_instructions = f"{base_instructions}\n\n{path_info}"
        else:
            full_instructions = base_instructions
        
        return full_instructions
    
    def _get_path_context(self, context_path: Optional[str] = None) -> str:
        """Формирует контекст с информацией о путях."""
        working_dir = config.get_working_directory()
        config_dir = config.get_config_directory()
        
        path_context = [
            "📁 Информация о путях:",
            f"   Рабочая директория: {working_dir}",
            f"   Директория конфигурации: {config_dir}"
        ]
        
        if context_path:
            absolute_context_path = config.get_absolute_path(context_path)
            path_context.append(f"   Контекстный путь: {context_path}")
            path_context.append(f"   Абсолютный контекстный путь: {absolute_context_path}")
        
        path_context.append("")
        path_context.append("💡 Используй эти пути для работы с файлами и директориями.")
        
        return "\n".join(path_context)
    
    async def _get_agent_tools(self, agent_config: AgentConfig) -> List[Any]:
        """Получает все инструменты для агента (function tools + MCP + agents as tools)."""
        tools = []
        
        # Добавляем function tools
        function_tools = []
        mcp_tools = []
        agent_tools = []
        
        for tool_name in agent_config.tools:
            tool_config = config._config.get('tools', {}).get(tool_name, {})
            
            if tool_config.get('type') == 'function':
                function_tools.append(tool_name)
            elif tool_config.get('type') == 'mcp':
                mcp_tools.append(tool_name)
            elif tool_config.get('type') == 'agent':
                agent_tools.append(tool_name)
        
        # Получаем function tools
        if function_tools:
            tools.extend(get_tools_by_names(function_tools))
        
        # Получаем агентов как инструменты
        if agent_tools:
            agent_tools_list = await self._create_agent_tools(agent_tools)
            tools.extend(agent_tools_list)
        
        # Подключаем MCP серверы
        if mcp_tools and (agent_config.mcp_enabled or config.is_mcp_enabled()):
            for tool_name in mcp_tools:
                mcp_client = await self._get_mcp_client(tool_name)
                if mcp_client:
                    mcp_tools_list = await mcp_client.get_tools()
                    tools.extend(mcp_tools_list)
        
        return tools
    
    async def _create_agent_tools(self, agent_keys: List[str]) -> List[Any]:
        """Создает инструменты из агентов для использования в других агентах."""
        tools = []
        
        for agent_key in agent_keys:
            if agent_key not in self.agent_tools_cache:
                # Создаем агента
                agent = await self.create_agent(agent_key)
                
                # Получаем конфигурацию инструмента
                tool_config = config._config.get('tools', {}).get(agent_key, {})
                tool_name = tool_config.get('name', f"call_{agent_key}")
                tool_description = tool_config.get('description', f"Вызывает {agent.name} для выполнения задач")
                
                # Создаем инструмент из агента
                agent_tool = agent.as_tool(
                    tool_name=tool_name,
                    tool_description=tool_description
                )
                
                self.agent_tools_cache[agent_key] = agent_tool
            
            tools.append(self.agent_tools_cache[agent_key])
        
        return tools
    
    async def _get_mcp_client(self, tool_name: str) -> MCPClient:
        """Получает или создает MCP клиента для инструмента."""
        if tool_name in self.mcp_clients:
            return self.mcp_clients[tool_name]
        
        tool_config = config._config.get('tools', {}).get(tool_name, {})
        if tool_config.get('type') != 'mcp':
            return None
        
        server_command = tool_config.get('server_command', [])
        env_vars = tool_config.get('env_vars', {})
        
        # Создаем MCP клиента
        mcp_client = MCPClient(tool_name, server_command, env_vars)
        
        try:
            await mcp_client.connect()
            self.mcp_clients[tool_name] = mcp_client
            print(f"✅ MCP сервер '{tool_name}' подключен")
            return mcp_client
        except Exception as e:
            print(f"❌ Ошибка подключения MCP сервера '{tool_name}': {e}")
            return None
    
    async def cleanup(self):
        """Закрывает все MCP соединения."""
        for mcp_client in self.mcp_clients.values():
            await mcp_client.disconnect()
        self.mcp_clients.clear()
        self.agent_cache.clear()
        self.agent_tools_cache.clear()

# Глобальная фабрика
agent_factory = AgentFactory()