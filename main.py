"""
Главный модуль приложения.
"""

import asyncio
import sys
import argparse
import os
from src.demo.chat_demo import run_chat_demo
from src.config.openrouter_config import Config
# Инициализируем логгер в самом начале
from src.utils.logger import logger

def parse_arguments():
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(description="OpenRouter Agents Demo")
    parser.add_argument(
        "--path", "-p",
        type=str,
        help="Рабочая директория для агента (по умолчанию текущая)"
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default="config.yaml",
        help="Путь к файлу конфигурации (по умолчанию config.yaml)"
    )
    parser.add_argument(
        "--agent", "-a",
        type=str,
        help="Имя агента для запуска (по умолчанию из конфигурации)"
    )
    parser.add_argument(
        "--context-path",
        type=str,
        help="Контекстный путь для передачи агенту"
    )
    return parser.parse_args()

async def main():
    """Главная функция."""
    try:
        # Парсим аргументы
        args = parse_arguments()
        
        # Инициализируем логгер перед всем остальным
        logger._setup_logging()
        print(f"🔍 Логгирование: {'включено' if logger.debug_enabled else 'выключено'}")
        
        # Устанавливаем рабочую директорию
        working_dir = args.path or os.getcwd()
        print(f"📁 Рабочая директория: {working_dir}")
        
        # Загружаем конфигурацию
        config = Config()
        
        print("🚀 OpenRouter Agents Demo")
        print("========================")
        print(f"🤖 Модель: {config.model_name}")
        print(f"🔗 API: {config.base_url}")
        print(f"📂 Конфигурация: {args.config}")
        if args.context_path:
            print(f"🎯 Контекстный путь: {args.context_path}")
        print()
        
        # Запускаем чат-демо с передачей путей
        await run_chat_demo(
            working_directory=working_dir,
            config_path=args.config,
            agent_name=args.agent,
            context_path=args.context_path
        )
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        pass


if __name__ == "__main__":
    asyncio.run(main())