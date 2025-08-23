#!/usr/bin/env python3
"""
Grid MCP Server - предоставляет доступ к Grid агентской системе через Model Context Protocol.
Позволяет Claude Code и другим MCP клиентам использовать Grid агентов как инструменты.
"""

import asyncio
import sys
import json
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

# Добавляем путь к grid пакету
sys.path.insert(0, str(Path(__file__).parent))

# MCP Server импорты
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Resource, Tool, TextContent, ImageContent, EmbeddedResource
)

# Grid система импорты
from core.config import Config
from core.security_agent_factory import SecurityAwareAgentFactory
from utils.logger import Logger
from utils.exceptions import GridError
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создаем MCP сервер
server = Server("grid-agents")

# Глобальные переменные для Grid системы
grid_config: Optional[Config] = None
agent_factory: Optional[SecurityAwareAgentFactory] = None


async def initialize_grid_system(config_path: str = "config.yaml", working_directory: Optional[str] = None):
    """Инициализация Grid системы."""
    global grid_config, agent_factory
    
    try:
        logger.info(f"Initializing Grid system with config: {config_path}")
        
        # Загружаем конфигурацию
        grid_config = Config(config_path, working_directory)
        
        # Создаем фабрику агентов
        agent_factory = SecurityAwareAgentFactory(grid_config, working_directory)
        await agent_factory.initialize()
        
        # Очищаем контекст при запуске
        agent_factory.clear_context()
        
        logger.info("Grid system initialized successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to initialize Grid system: {e}")
        return False


@server.list_resources()
async def list_resources() -> List[Resource]:
    """Список доступных ресурсов Grid системы."""
    if not grid_config:
        return []
    
    resources = []
    
    # Добавляем информацию о конфигурации
    resources.append(Resource(
        uri="grid://config",
        name="Grid Configuration",
        description="Current Grid system configuration",
        mimeType="application/json"
    ))
    
    # Добавляем список агентов
    for agent_key, agent_config in grid_config.config.agents.items():
        resources.append(Resource(
            uri=f"grid://agent/{agent_key}",
            name=f"Agent: {agent_config.name}",
            description=f"Configuration for agent {agent_config.name}: {agent_config.description}",
            mimeType="application/json"
        ))
    
    # Добавляем список инструментов
    for tool_key, tool_config in grid_config.config.tools.items():
        resources.append(Resource(
            uri=f"grid://tool/{tool_key}",
            name=f"Tool: {tool_key}",
            description=f"{tool_config.type} tool: {tool_config.description}",
            mimeType="application/json"
        ))
    
    return resources


@server.read_resource()
async def read_resource(uri: str) -> str:
    """Чтение ресурса Grid системы."""
    if not grid_config:
        return "Grid system not initialized"
    
    # Преобразуем uri в строку, если это объект
    if hasattr(uri, 'startswith'):
        uri_str = str(uri)
    else:
        uri_str = uri
    
    try:
        if uri_str == "grid://config":
            # Возвращаем основную информацию о конфигурации
            config_info = {
                "default_agent": grid_config.get_default_agent(),
                "working_directory": grid_config.get_working_directory(),
                "mcp_enabled": grid_config.is_mcp_enabled(),
                "max_history": grid_config.get_max_history(),
                "max_turns": grid_config.get_max_turns(),
                "agent_count": len(grid_config.config.agents),
                "tool_count": len(grid_config.config.tools),
                "model_count": len(grid_config.config.models),
                "provider_count": len(grid_config.config.providers)
            }
            return json.dumps(config_info, indent=2, ensure_ascii=False)
        
        elif uri_str.startswith("grid://agent/"):
            agent_key = uri_str.replace("grid://agent/", "")
            if agent_key in grid_config.config.agents:
                agent_config = grid_config.config.agents[agent_key]
                agent_info = {
                    "name": agent_config.name,
                    "model": agent_config.model,
                    "tools": agent_config.tools,
                    "description": agent_config.description,
                    "mcp_enabled": agent_config.mcp_enabled,
                    "base_prompt": agent_config.base_prompt
                }
                return json.dumps(agent_info, indent=2, ensure_ascii=False)
            else:
                return f"Agent '{agent_key}' not found"
        
        elif uri_str.startswith("grid://tool/"):
            tool_key = uri_str.replace("grid://tool/", "")
            if tool_key in grid_config.config.tools:
                tool_config = grid_config.config.tools[tool_key]
                tool_info = {
                    "type": tool_config.type.value,
                    "name": tool_config.name,
                    "description": tool_config.description,
                    "prompt_addition": tool_config.prompt_addition
                }
                # Добавляем специфичные поля для разных типов инструментов
                if tool_config.type.value == "mcp" and tool_config.server_command:
                    tool_info["server_command"] = tool_config.server_command
                elif tool_config.type.value == "agent" and tool_config.target_agent:
                    tool_info["target_agent"] = tool_config.target_agent
                
                return json.dumps(tool_info, indent=2, ensure_ascii=False)
            else:
                return f"Tool '{tool_key}' not found"
        
        else:
            return f"Resource '{uri_str}' not found"
    
    except Exception as e:
        logger.error(f"Error reading resource {uri}: {e}")
        return f"Error reading resource: {e}"


@server.list_tools()
async def list_tools() -> List[Tool]:
    """Список доступных Grid агентов как MCP инструментов."""
    if not grid_config or not agent_factory:
        return []
    
    tools = []
    
    # Создаем инструмент для каждого агента
    for agent_key, agent_config in grid_config.config.agents.items():
        tool_description = f"Run Grid agent '{agent_config.name}'"
        if agent_config.description:
            tool_description += f": {agent_config.description}"
        
        # Добавляем информацию о доступных инструментах агента
        if agent_config.tools:
            tool_description += f" (Available tools: {', '.join(agent_config.tools[:3])}{'...' if len(agent_config.tools) > 3 else ''})"
        
        tools.append(Tool(
            name=f"run_agent_{agent_key}",
            description=tool_description,
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": f"Message to send to {agent_config.name}"
                    },
                    "context_path": {
                        "type": "string",
                        "description": "Optional context path for agent execution",
                        "default": None
                    },
                    "stream": {
                        "type": "boolean",
                        "description": "Whether to stream the response (default: false)",
                        "default": False
                    }
                },
                "required": ["message"]
            }
        ))
    
    # Добавляем общие инструменты для управления Grid системой
    tools.extend([
        Tool(
            name="list_grid_agents",
            description="List all available Grid agents with their configurations",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_grid_status",
            description="Get current status and health information of Grid system",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        ),
        Tool(
            name="clear_grid_context",
            description="Clear conversation context for all agents",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        )
    ])
    
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """Обработка вызовов инструментов Grid."""
    if not agent_factory or not grid_config:
        return [TextContent(
            type="text",
            text="❌ Grid system not initialized"
        )]
    
    try:
        logger.info(f"MCP tool called: {name} with arguments: {arguments}")
        
        # Обработка вызовов агентов
        if name.startswith("run_agent_"):
            agent_key = name.replace("run_agent_", "")
            message = arguments.get("message", "")
            context_path = arguments.get("context_path")
            stream = arguments.get("stream", False)
            
            if not message:
                return [TextContent(
                    type="text",
                    text="❌ Message is required"
                )]
            
            # Проверяем, что агент существует
            if agent_key not in grid_config.config.agents:
                return [TextContent(
                    type="text",
                    text=f"❌ Agent '{agent_key}' not found"
                )]
            
            try:
                # Выполняем агента
                logger.info(f"Running agent {agent_key} with message: {message[:100]}...")
                result = await agent_factory.run_agent(
                    agent_key=agent_key,
                    message=message,
                    context_path=context_path,
                    stream=stream
                )
                
                logger.info(f"Agent {agent_key} completed successfully, result length: {len(result)}")
                return [TextContent(
                    type="text",
                    text=result
                )]
                
            except Exception as e:
                logger.error(f"Error running agent {agent_key}: {e}")
                return [TextContent(
                    type="text",
                    text=f"❌ Error running agent {agent_key}: {e}"
                )]
        
        # Обработка системных инструментов
        elif name == "list_grid_agents":
            agents_info = []
            for agent_key, agent_config in grid_config.config.agents.items():
                model_config = grid_config.get_model(agent_config.model)
                agents_info.append({
                    "key": agent_key,
                    "name": agent_config.name,
                    "description": agent_config.description,
                    "model": agent_config.model,
                    "provider": model_config.provider,
                    "tools_count": len(agent_config.tools),
                    "tools": agent_config.tools[:5],  # Показываем первые 5 инструментов
                    "mcp_enabled": agent_config.mcp_enabled
                })
            
            return [TextContent(
                type="text", 
                text=json.dumps(agents_info, indent=2, ensure_ascii=False)
            )]
        
        elif name == "get_grid_status":
            try:
                logger.debug("Getting grid status...")
                status_info = {
                    "system": "Grid Agent System",
                    "status": "running"
                }
                
                # Безопасно добавляем информацию
                try:
                    status_info.update({
                        "config_file": getattr(grid_config, 'config_file', 'config.yaml'),
                        "working_directory": grid_config.get_working_directory(),
                        "default_agent": grid_config.get_default_agent(),
                        "mcp_enabled": grid_config.is_mcp_enabled(),
                    })
                except Exception as e:
                    logger.debug(f"Error adding basic config info: {e}")
                
                try:
                    status_info["agents"] = {
                        "total": len(grid_config.config.agents),
                        "mcp_enabled": sum(1 for a in grid_config.config.agents.values() if a.mcp_enabled)
                    }
                except Exception as e:
                    logger.debug(f"Error adding agents info: {e}")
                    
                try:
                    status_info["tools"] = {
                        "total": len(grid_config.config.tools)
                    }
                except Exception as e:
                    logger.debug(f"Error adding tools info: {e}")
                
                try:
                    status_info.update({
                        "models": len(grid_config.config.models),
                        "providers": len(grid_config.config.providers)
                    })
                except Exception as e:
                    logger.debug(f"Error adding model/provider info: {e}")
                
                logger.debug("Status info collected, returning...")
                return [TextContent(
                    type="text",
                    text=json.dumps(status_info, indent=2, ensure_ascii=False)
                )]
            except Exception as e:
                logger.error(f"Error getting status: {e}")
                return [TextContent(
                    type="text",
                    text=f"❌ Error getting status: {e}"
                )]
        
        elif name == "clear_grid_context":
            try:
                agent_factory.clear_context()
                return [TextContent(
                    type="text",
                    text="✅ Grid conversation context cleared successfully"
                )]
            except Exception as e:
                logger.error(f"Error clearing context: {e}")
                return [TextContent(
                    type="text",
                    text=f"❌ Error clearing context: {e}"
                )]
        
        else:
            return [TextContent(
                type="text",
                text=f"❌ Unknown tool: {name}"
            )]
    
    except Exception as e:
        logger.error(f"Error in call_tool {name}: {e}")
        return [TextContent(
            type="text",
            text=f"❌ Error executing tool {name}: {e}"
        )]


async def cleanup_grid_system():
    """Очистка ресурсов Grid системы."""
    global agent_factory
    
    if agent_factory:
        try:
            # Используем print вместо logger, так как логгер может быть закрыт
            await agent_factory.cleanup()
        except Exception as e:
            # Игнорируем ошибки при очистке, так как система уже завершается
            pass


async def main():
    """Главная функция MCP сервера."""
    parser = argparse.ArgumentParser(description="Grid MCP Server")
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to Grid configuration file"
    )
    parser.add_argument(
        "--working-directory", "-w",
        help="Working directory for Grid system"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level"
    )
    
    args = parser.parse_args()
    
    # Настройка логирования
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    logger.info(f"Starting Grid MCP Server with config: {args.config}")
    
    # Инициализация Grid системы
    if not await initialize_grid_system(args.config, args.working_directory):
        logger.error("Failed to initialize Grid system, exiting...")
        return 1
    
    try:
        # Запуск MCP сервера
        logger.info("Starting MCP server...")
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    except KeyboardInterrupt:
        logger.info("MCP server stopped by user")
    except Exception as e:
        logger.error(f"MCP server error: {e}")
        return 1
    finally:
        # Очистка ресурсов
        await cleanup_grid_system()
    
    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)