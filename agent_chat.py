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

# Use Proactor event loop on Windows to support asyncio subprocess APIs (required for MCP)
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from core.config import Config
from core.agent_factory import AgentFactory
from core.security_agent_factory import SecurityAwareAgentFactory
from utils.logger import Logger
from utils.pretty_logger import PrettyLogger
from utils.unified_logger import configure_unified_logger, LogLevel
from utils.cli_logger import CLILogger

# Create CLI logger instance
cli_logger = CLILogger()
from utils.exceptions import GridError
import logging
import time

# Configure logging to suppress technical messages
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("grid").setLevel(logging.INFO)
import os
os.environ['PROJECT_ROOT'] = str(Path(__file__).parent)
Logger.configure(level="INFO", enable_console=False, log_dir="logs", enable_legacy_logs=True, force_reconfigure=True)  # логируем строго в logs/ проекта

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
        cli_logger.info("Запуск Grid Agent System...")
        
        # Load configuration
        cli_logger.info("Load Config")
        config = Config(args.config, args.path)
        cli_logger.info("Load Config - Конфигурация загружена")
        
        # Create factory
        cli_logger.info("Initialize SecurityAwareAgentFactory")
        factory = SecurityAwareAgentFactory(config, args.path)
        cli_logger.info("Initialize SecurityAwareAgentFactory - Фабрика агентов инициализирована")
        
        # Настройка универсального логирования
        agent_logging_config = config.config.settings.agent_logging
        if agent_logging_config.enabled:
            console_level = LogLevel.INFO
            file_level = LogLevel.DEBUG
            
            if agent_logging_config.level == "basic":
                console_level = LogLevel.INFO
                file_level = LogLevel.INFO
            elif agent_logging_config.level == "detailed":
                console_level = LogLevel.INFO
                file_level = LogLevel.DEBUG
            elif agent_logging_config.level == "full":
                console_level = LogLevel.DEBUG
                file_level = LogLevel.DEBUG
            
            configure_unified_logger("logs", console_level, file_level, enable_colors=True)
            cli_logger.info("Универсальное логирование включено")
        else:
            cli_logger.info("Универсальное логирование отключено")
        
        # Determine agent
        agent_key = args.agent or config.get_default_agent()
        
        # Автоматически очищаем контекст при запуске - агенты не должны помнить предыдущие чаты
        cli_logger.info("Clear Context")
        factory.clear_context()
        
        # Также удаляем файл с сохраненным контекстом, если он существует
        import os
        context_file = "logs/context.json"
        if os.path.exists(context_file):
            os.remove(context_file)
            cli_logger.info(f"Удален файл сохраненного контекста: {context_file}")
        
        cli_logger.info("Clear Context - Контекст очищен при запуске")
        
        cli_logger.info("Grid Agent System готов к работе")
        
        print("\n" + "="*60)
        print("🤖 Grid Agent System ")
        print("="*60)
        print(f"Агент: {agent_key}")
        print(f"Рабочая директория: {config.get_working_directory()}")
        if args.context_path:
            print(f"Контекстный путь: {args.context_path}")
        print("="*60)
        
        if args.message:
            # Single message mode
            cli_logger.info(f"Обработка сообщения")
            
            try:
                # Track agent execution
                cli_logger.info(f"Agent {agent_key} (agent: {agent_key})")
                
                start_time = time.time()
                use_streaming = True  # Включаем стриминг для режима одного сообщения
                response = await factory.run_agent(agent_key, args.message, args.context_path, stream=use_streaming)
                duration = time.time() - start_time
                
                # Try to get token usage information
                token_usage = None
                try:
                    # Estimate token usage (approximation since we don't have direct access)
                    # This is a rough estimate - in production you'd want to capture real usage
                    estimated_prompt_tokens = len(args.message.split()) * 1.3  # rough estimate
                    estimated_completion_tokens = len(response.split()) * 1.3
                    
                    # Try to get model from agent config
                    agent_config = config.get_agent(agent_key)
                    model_name = getattr(agent_config, 'model', 'unknown')
                    
                    # Token calculation removed
                except Exception as e:
                    pass  # Ignore token calculation errors
                
                cli_logger.info(f"Ответ сгенерирован ({duration:.2f}с, {len(response)} символов)")
                
                print(f"\n🤖 Ответ:")
                print("-" * 60)
                # При стриминге ответ уже выведен в реальном времени, просто добавляем разделитель
                if use_streaming:
                    print()  # Добавляем новую строку после стримингового вывода
                else:
                    print(response)
                
                cli_logger.info("Success")
                
            except Exception as e:
                cli_logger.info("Operation completed")
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
                        cli_logger.info("Clear Context")
                        factory.clear_context()
                        cli_logger.info("Clear Context - Контекст очищен")
                        cli_logger.info("Success")
                        continue
                    elif user_input.lower() == 'context':
                        cli_logger.info("Get Context")
                        context_info = factory.get_context_info()
                        cli_logger.info("Get Context - Информация о контексте получена")
                        
                        print(f"\n📋 Информация о контексте:")
                        print(f"   Сообщений: {context_info.get('conversation_messages', 0)}")
                        print(f"   История выполнения: {context_info.get('execution_history', 0)}")
                        print(f"   Использование памяти: {context_info.get('memory_usage_mb', 0):.2f} МБ")
                        if context_info.get('last_user_message'):
                            last_msg = context_info['last_user_message']
                            print(f"   Последнее сообщение: {last_msg}")
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
                    try:
                        # Track execution with token counting
                        cli_logger.info(f"Agent {agent_key} (agent: {agent_key})")
                        
                        start_time = time.time()
                        use_streaming = True  # Включаем стриминг для интерактивного режима
                        response = await factory.run_agent(agent_key, user_input, args.context_path, stream=use_streaming)
                        duration = time.time() - start_time
                        
                        # Try to get token usage information
                        token_usage = None
                        try:
                            estimated_prompt_tokens = len(user_input.split()) * 1.3
                            estimated_completion_tokens = len(response.split()) * 1.3
                            
                            agent_config = config.get_agent(agent_key)
                            model_name = getattr(agent_config, 'model', 'unknown')
                            # Token calculation removed
                        except Exception:
                            pass
                        
                        cli_logger.info(f"Ответ получен ({duration:.2f}с, {len(response)} символов)")
                        
                        # При стриминге ответ уже выведен в реальном времени, добавляем только новую строку
                        if use_streaming:
                            print(f"\n")  # Добавляем новую строку после стримингового вывода
                        else:
                            print(f"\n🤖 {agent_key}: {response}")
                        
                    except Exception as e:
                        cli_logger.info("Operation completed")
                        print(f"❌ Ошибка: {e}")
                    
                except KeyboardInterrupt:
                    print("\n\n👋 Interrupted. Goodbye!")
                    break
                except EOFError:
                    print("\n\n👋 EOF. Goodbye!")
                    break
        
        # Beautiful cleanup and session summary
        cli_logger.info("Cleanup")
        await factory.cleanup()
        cli_logger.info("Cleanup - Ресурсы освобождены")
        
        # Session summary
        cli_logger.info("Grid Agent System завершил работу")
        
    except GridError as e:
        cli_logger.error(f"Ошибка Grid: {e}")
        print(f"❌ Grid Error: {e}")
        sys.exit(1)
    except Exception as e:
        cli_logger.error(f"Неожиданная ошибка: {e}")
        print(f"❌ Unexpected Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())