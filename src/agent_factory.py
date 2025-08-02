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
from context_manager import ContextManager

load_dotenv()

class AgentFactory:
    """Фабрика для создания агентов из YAML конфигурации."""
    
    def __init__(self, working_directory: Optional[str] = None):
        """Инициализация фабрики."""
        set_tracing_disabled(True)
        self.mcp_clients = {}
        self.agent_cache = {}
        self.agent_tools_cache = {}
        self.context_manager = ContextManager()
        
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
        from utils.logger import log_agent_prompt, log_custom
        log_agent_prompt(agent_config.name, instructions)
        
        # Дополнительное логирование для координатора (только в файл)
        if agent_key == "coordinator":
            log_custom('debug', 'coordinator_prompt', f"Coordinator prompt length: {len(instructions)}")
            log_custom('debug', 'coordinator_prompt', f"Contains 'call_thinker': {'call_thinker' in instructions}")
            log_custom('debug', 'coordinator_prompt', f"Contains 'ПРАВИЛО': {'ПРАВИЛО' in instructions}")
            log_custom('debug', 'coordinator_prompt', f"Contains 'ПРИМЕР': {'ПРИМЕР' in instructions}")
        
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
    
    def add_to_context(self, role: str, content: str):
        """Добавляет сообщение в контекст разговора."""
        self.context_manager.add_message(role, content)
    
    def clear_context(self):
        """Очищает контекст разговора."""
        self.context_manager.clear_history()
    
    def get_context_info(self) -> dict:
        """Возвращает информацию о контексте."""
        return {
            "history_count": self.context_manager.get_history_count(),
            "last_user_message": self.context_manager.get_last_user_message()
        }
    
    def _build_agent_prompt_with_paths(self, agent_key: str, context_path: Optional[str] = None) -> str:
        """Строит промпт агента с информацией о путях."""
        base_instructions = config.build_agent_prompt(agent_key)
        
        # Добавляем информацию о путях
        path_info = self._get_path_context(context_path)
        
        # Добавляем контекст разговора
        conversation_context = self.context_manager.get_context_message()
        
        # Собираем полный промпт
        parts = [base_instructions]
        
        if path_info:
            parts.append(path_info)
        
        if conversation_context:
            parts.append(conversation_context)
        
        full_instructions = "\n\n".join(parts)
        
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
                
                # Модифицируем инструмент для правильного логирования как агента
                agent_tool = self._wrap_agent_tool_for_logging(agent_tool, agent.name, agent_key)
                
                self.agent_tools_cache[agent_key] = agent_tool
            
            tools.append(self.agent_tools_cache[agent_key])
        
        return tools
    
    def _wrap_agent_tool_for_logging(self, agent_tool, agent_name: str, agent_key: str):
        """Оборачивает агент-инструмент для правильного логирования как агента."""
        import time
        from utils.logger import log_agent_tool_start, log_agent_tool_end, log_agent_tool_error
        
        # Получаем конфигурацию инструмента для более описательного имени
        tool_config = config._config.get('tools', {}).get(agent_key, {})
        tool_name = tool_config.get('name', f"call_{agent_key}")
        display_name = f"{agent_name} ({tool_name})"
        
        # Сохраняем оригинальную функцию
        # FunctionTool использует on_invoke_tool для вызова функции
        if hasattr(agent_tool, 'on_invoke_tool'):
            original_invoke = agent_tool.on_invoke_tool
        else:
            # Если не можем найти функцию, возвращаем инструмент как есть
            print(f"⚠️ Не удалось найти функцию для агента {display_name}")
            return agent_tool
        
        async def wrapped_invoke_tool(tool_context, tool_call_arguments):
            start_time = time.time()
            input_data = str(tool_call_arguments)
            
            # Логируем как агента-инструмент
            log_agent_tool_start(agent_name, tool_name, input_data)
            
            try:
                # Вызываем оригинальную функцию
                result = original_invoke(tool_context, tool_call_arguments)
                
                # Проверяем, является ли результат корутиной
                if hasattr(result, '__await__'):
                    result = await result
                
                duration = time.time() - start_time
                
                # Логируем завершение как агента-инструмент
                log_agent_tool_end(agent_name, tool_name, str(result), duration)
                
                return result
                
            except Exception as e:
                duration = time.time() - start_time
                log_agent_tool_error(agent_name, tool_name, e)
                raise
        
        # Заменяем функцию в инструменте
        agent_tool.on_invoke_tool = wrapped_invoke_tool
        
        return agent_tool
    
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
    
    def clear_agent_cache(self):
        """Очищает кэш агентов для принудительной перезагрузки."""
        self.agent_cache.clear()
        self.agent_tools_cache.clear()
        from utils.logger import log_custom
        log_custom('info', 'cache_clear', "Agent cache cleared")

# Глобальная фабрика
agent_factory = AgentFactory()