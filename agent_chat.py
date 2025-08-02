#!/usr/bin/env python3
"""
Legacy chat interface for Grid Agent System.
Simplified version of the main.py CLI for backward compatibility.
"""

import asyncio
import argparse
import sys
from pathlib import Path

# Add grid package to path
sys.path.insert(0, str(Path(__file__).parent))

from core.config import Config
from core.agent_factory import AgentFactory
from utils.logger import Logger
from utils.pretty_logger import PrettyLogger, update_todos
from utils.exceptions import GridError
import logging
import time

# Configure logging to suppress technical messages
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("grid").setLevel(logging.WARNING)
Logger.configure(level="WARNING", enable_console=False)

# Initialize beautiful logger
pretty_logger = PrettyLogger("agent_chat")

async def main():
    """Главная функция."""
    parser = argparse.ArgumentParser(description="Legacy Grid agent chat interface")
    parser.add_argument(
        "--agent", "-a",
        type=str,
        default=None,
        help="Agent name (default from config)"
    )
    parser.add_argument(
        "--path", "-p",
        type=str,
        default=None,
        help="Working directory"
    )
    parser.add_argument(
        "--context-path",
        type=str,
        default=None,
        help="Context path for agent"
    )
    parser.add_argument(
        "--message", "-m",
        type=str,
        default=None,
        help="Single message to send (interactive mode if not provided)"
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default="config.yaml",
        help="Configuration file path"
    )
    
    args = parser.parse_args()
    
    try:
        # Beautiful initialization
        pretty_logger.info("Запуск Grid Agent System...")
        
        # Load configuration
        operation = pretty_logger.tool_start("Config", path=args.config)
        config = Config(args.config, args.path)
        pretty_logger.tool_result(operation, result="Конфигурация загружена")
        
        # Create factory
        operation = pretty_logger.tool_start("AgentFactory")
        factory = AgentFactory(config, args.path)
        pretty_logger.tool_result(operation, result="Фабрика агентов инициализирована")
        
        # Determine agent
        agent_key = args.agent or config.get_default_agent()
        
        pretty_logger.success("Grid Agent System готов к работе")
        
        print("\n" + "="*60)
        print("🤖 Grid Agent System - Красивый Интерфейс")
        print("="*60)
        print(f"Агент: {agent_key}")
        print(f"Рабочая директория: {config.get_working_directory()}")
        if args.context_path:
            print(f"Контекстный путь: {args.context_path}")
        print("="*60)
        
        if args.message:
            # Single message mode
            pretty_logger.info(f"Обработка сообщения: {args.message}")
            
            # Show todos for processing
            update_todos([
                {"id": "1", "content": f"Обработать сообщение агентом {agent_key}", "status": "in_progress", "priority": "high"},
                {"id": "2", "content": "Сгенерировать ответ", "status": "pending", "priority": "high"},
                {"id": "3", "content": "Обновить контекст беседы", "status": "pending", "priority": "medium"}
            ])
            
            try:
                # Track agent execution
                operation = pretty_logger.tool_start("AgentExecution", 
                                                   agent=agent_key, 
                                                   message_length=len(args.message))
                
                start_time = time.time()
                response = await factory.run_agent(agent_key, args.message, args.context_path)
                duration = time.time() - start_time
                
                # Update todos - completed
                update_todos([
                    {"id": "1", "content": f"Обработать сообщение агентом {agent_key}", "status": "completed", "priority": "high"},
                    {"id": "2", "content": "Сгенерировать ответ", "status": "completed", "priority": "high"},
                    {"id": "3", "content": "Обновить контекст беседы", "status": "completed", "priority": "medium"}
                ])
                
                pretty_logger.tool_result(operation, 
                                        result=f"Ответ сгенерирован ({duration:.2f}с, {len(response)} символов)")
                
                print(f"\n🤖 Ответ:")
                print("-" * 60)
                print(response)
                
                pretty_logger.success("Сообщение успешно обработано")
                
            except Exception as e:
                # Update todos - error
                update_todos([
                    {"id": "1", "content": f"Обработать сообщение агентом {agent_key}", "status": "completed", "priority": "high"},
                    {"id": "2", "content": "Сгенерировать ответ", "status": "pending", "priority": "high"},
                    {"id": "3", "content": f"Обработать ошибку: {str(e)}", "status": "in_progress", "priority": "high"}
                ])
                pretty_logger.error(f"Ошибка выполнения агента: {e}")
                print(f"❌ Ошибка: {e}")
        else:
            # Interactive mode
            print("\nCommands:")
            print("  'exit' or 'quit' - Exit")
            print("  'clear' - Clear conversation history")
            print("  'context' - Show context info")
            print("  'help' - Show this help")
            print("-" * 60)
            
            while True:
                try:
                    user_input = input("\n👤 You: ").strip()
                    
                    if user_input.lower() in ['exit', 'quit']:
                        print("👋 Goodbye!")
                        break
                    elif user_input.lower() == 'clear':
                        operation = pretty_logger.tool_start("ClearContext")
                        factory.clear_context()
                        pretty_logger.tool_result(operation, result="Контекст очищен")
                        pretty_logger.success("Контекст беседы очищен")
                        continue
                    elif user_input.lower() == 'context':
                        operation = pretty_logger.tool_start("GetContext")
                        context_info = factory.get_context_info()
                        pretty_logger.tool_result(operation, result="Информация о контексте получена")
                        
                        print(f"\n📋 Информация о контексте:")
                        print(f"   Сообщений: {context_info.get('conversation_messages', 0)}")
                        print(f"   История выполнения: {context_info.get('execution_history', 0)}")
                        print(f"   Использование памяти: {context_info.get('memory_usage_mb', 0):.2f} МБ")
                        if context_info.get('last_user_message'):
                            last_msg = context_info['last_user_message'][:100]
                            print(f"   Последнее сообщение: {last_msg}...")
                        continue
                    elif user_input.lower() == 'help':
                        print("\nAvailable commands:")
                        print("  exit, quit - Exit the chat")
                        print("  clear - Clear conversation history")
                        print("  context - Show context information")
                        print("  help - Show this help message")
                        continue
                    elif not user_input:
                        continue
                    
                    # Process user message with beautiful logging
                    pretty_logger.info(f"Обработка сообщения агентом {agent_key}...")
                    
                    # Show processing todos
                    update_todos([
                        {"id": "1", "content": f"Передать сообщение агенту {agent_key}", "status": "in_progress", "priority": "high"},
                        {"id": "2", "content": "Обработать запрос", "status": "pending", "priority": "high"},
                        {"id": "3", "content": "Сгенерировать ответ", "status": "pending", "priority": "medium"}
                    ])
                    
                    try:
                        # Track execution
                        operation = pretty_logger.tool_start("AgentExecution", 
                                                           agent=agent_key,
                                                           message_length=len(user_input))
                        
                        start_time = time.time()
                        response = await factory.run_agent(agent_key, user_input, args.context_path)
                        duration = time.time() - start_time
                        
                        # Update todos - success
                        update_todos([
                            {"id": "1", "content": f"Передать сообщение агенту {agent_key}", "status": "completed", "priority": "high"},
                            {"id": "2", "content": "Обработать запрос", "status": "completed", "priority": "high"},
                            {"id": "3", "content": "Сгенерировать ответ", "status": "completed", "priority": "medium"}
                        ])
                        
                        pretty_logger.tool_result(operation, 
                                                result=f"Ответ получен ({duration:.2f}с, {len(response)} символов)")
                        
                        print(f"\n🤖 {agent_key}: {response}")
                        
                    except Exception as e:
                        # Update todos - error
                        update_todos([
                            {"id": "1", "content": f"Передать сообщение агенту {agent_key}", "status": "completed", "priority": "high"},
                            {"id": "2", "content": "Обработать запрос", "status": "pending", "priority": "high"},
                            {"id": "3", "content": f"Обработать ошибку: {str(e)}", "status": "in_progress", "priority": "high"}
                        ])
                        pretty_logger.error(f"Ошибка выполнения: {e}")
                        print(f"❌ Ошибка: {e}")
                    
                except KeyboardInterrupt:
                    print("\n\n👋 Interrupted. Goodbye!")
                    break
                except EOFError:
                    print("\n\n👋 EOF. Goodbye!")
                    break
        
        # Beautiful cleanup
        operation = pretty_logger.tool_start("Cleanup")
        await factory.cleanup()
        pretty_logger.tool_result(operation, result="Ресурсы освобождены")
        pretty_logger.success("Grid Agent System завершил работу")
        
    except GridError as e:
        pretty_logger.error(f"Ошибка Grid: {e}")
        print(f"❌ Grid Error: {e}")
        sys.exit(1)
    except Exception as e:
        pretty_logger.error(f"Неожиданная ошибка: {e}")
        print(f"❌ Unexpected Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())