#!/usr/bin/env python3
"""
Простой скрипт для запуска агента с поддержкой путей.
"""

import asyncio
import argparse
import os
import sys
from pathlib import Path

# Добавляем src в путь для импортов
sys.path.insert(0, str(Path(__file__).parent / "src"))

from agent_factory import AgentFactory
from config_loader import config
from agents import Runner

async def main():
    """Главная функция."""
    parser = argparse.ArgumentParser(description="Запуск агента с поддержкой путей")
    parser.add_argument(
        "--agent", "-a",
        type=str,
        default=None,
        help="Имя агента (по умолчанию из конфигурации)"
    )
    parser.add_argument(
        "--path", "-p",
        type=str,
        default=None,
        help="Рабочая директория"
    )
    parser.add_argument(
        "--context-path",
        type=str,
        default=None,
        help="Контекстный путь для агента"
    )
    parser.add_argument(
        "--message", "-m",
        type=str,
        default=None,
        help="Сообщение для отправки агенту (если не указано, запускается интерактивный режим)"
    )
    
    args = parser.parse_args()
    
    try:
        # Создаем фабрику агентов
        factory = AgentFactory(working_directory=args.path)
        
        # Определяем агента
        agent_key = args.agent or config.get_default_agent()
        print(f"🤖 Используется агент: {agent_key}")
        
        # Логгируем информацию об агенте
        try:
            from utils.logger import log_custom
            agent_config = config.get_agent(agent_key)
            log_custom('info', 'agent_creation', f"Creating agent '{agent_key}' ({agent_config.name})")
            log_custom('debug', 'agent_creation', f"Model: {agent_config.model}")
            log_custom('debug', 'agent_creation', f"Tools: {agent_config.tools}")
            log_custom('debug', 'agent_creation', f"Base prompt: {agent_config.base_prompt}")
            log_custom('debug', 'agent_creation', f"Has custom prompt: {bool(agent_config.custom_prompt)}")
            
            # Логгируем информацию об агентах-инструментах
            agent_tools = [tool for tool in agent_config.tools if config._config.get('tools', {}).get(tool, {}).get('type') == 'agent']
            if agent_tools:
                log_custom('info', 'agent_creation', f"Agent tools available: {agent_tools}")
                for tool in agent_tools:
                    tool_config = config._config.get('tools', {}).get(tool, {})
                    agent_name = tool_config.get('name', f"call_{tool}")
                    log_custom('debug', 'agent_creation', f"  - {tool} -> {agent_name}")
        except Exception as e:
            print(f"⚠️ Ошибка логирования: {e}")
        
        # Создаем агента
        agent = await factory.create_agent(agent_key, context_path=args.context_path)
        
        # Показываем информацию о путях
        print("\n📁 Информация о путях:")
        print(f"   Рабочая директория: {config.get_working_directory()}")
        print(f"   Директория конфигурации: {config.get_config_directory()}")
        if args.context_path:
            print(f"   Контекстный путь: {args.context_path}")
            print(f"   Абсолютный контекстный путь: {config.get_absolute_path(args.context_path)}")
        print()
        
        if args.message:
            # Одноразовое сообщение
            print(f"💬 Отправка сообщения: {args.message}")
            
            # Добавляем сообщение пользователя в контекст
            factory.add_to_context("user", args.message)
            
            # Логгируем запуск агента
            import time
            start_time = time.time()
            try:
                from utils.logger import log_agent_start
                log_agent_start(agent.name, args.message)
            except Exception as e:
                print(f"⚠️ Ошибка логирования: {e}")
            
            result = await Runner.run(agent, args.message)
            
            # Добавляем ответ агента в контекст
            factory.add_to_context("assistant", result.final_output)
            
            # Логгируем завершение агента
            duration = time.time() - start_time
            try:
                from utils.logger import log_agent_end
                log_agent_end(agent.name, result.final_output, duration)
            except Exception as e:
                print(f"⚠️ Ошибка логирования: {e}")
            
            print(f"\n🤖 Ответ агента:\n{result.final_output}")
        else:
            # Интерактивный режим
            print("=== Интерактивный режим ===")
            print("Введите 'exit' для выхода")
            print("Введите 'clear' для очистки истории")
            print("Введите 'paths' для показа информации о путях")
            print("Введите 'context' для показа контекста")
            print("==========================")
            
            while True:
                try:
                    user_input = input("\nВы: ").strip()
                    
                    if user_input.lower() == 'exit':
                        break
                    elif user_input.lower() == 'clear':
                        # Очищаем контекст
                        factory.clear_context()
                        print("✅ История очищена")
                        continue
                    elif user_input.lower() == 'paths':
                        print("\n📁 Информация о путях:")
                        print(f"   Рабочая директория: {config.get_working_directory()}")
                        print(f"   Директория конфигурации: {config.get_config_directory()}")
                        if args.context_path:
                            print(f"   Контекстный путь: {args.context_path}")
                            print(f"   Абсолютный контекстный путь: {config.get_absolute_path(args.context_path)}")
                        continue
                    elif user_input.lower() == 'context':
                        context_info = factory.get_context_info()
                        print(f"\n📋 Информация о контексте:")
                        print(f"   Сообщений в истории: {context_info['history_count']}")
                        if context_info['last_user_message']:
                            print(f"   Последнее сообщение пользователя: {context_info['last_user_message'][:100]}...")
                        else:
                            print(f"   Последнее сообщение пользователя: нет")
                        continue
                    elif not user_input:
                        continue
                    
                    print("\nАгент думает...")
                    
                    # Добавляем сообщение пользователя в контекст
                    factory.add_to_context("user", user_input)
                    
                    # Логгируем запуск агента
                    import time
                    start_time = time.time()
                    try:
                        from utils.logger import log_agent_start
                        log_agent_start(agent.name, user_input)
                    except Exception as e:
                        print(f"⚠️ Ошибка логирования: {e}")
                    
                    result = await Runner.run(agent, user_input)
                    
                    # Добавляем ответ агента в контекст
                    factory.add_to_context("assistant", result.final_output)
                    
                    # Логгируем завершение агента
                    duration = time.time() - start_time
                    try:
                        from utils.logger import log_agent_end
                        log_agent_end(agent.name, result.final_output, duration)
                    except Exception as e:
                        print(f"⚠️ Ошибка логирования: {e}")
                    
                    print(f"\nАгент: {result.final_output}")
                    
                except KeyboardInterrupt:
                    print("\nПрерывание работы...")
                    break
                except Exception as e:
                    print(f"\nОшибка: {e}")
                    continue
        
        # Очищаем ресурсы
        await factory.cleanup()
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())