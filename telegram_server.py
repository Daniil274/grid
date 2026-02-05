"""
Telegram Server - главная точка входа для Unified Agent Bot

Запуск:
    python telegram_server.py
    python telegram_server.py --config config/telegram_config.yaml
"""

import asyncio
import argparse
import logging
import signal
import sys
from pathlib import Path
from typing import Optional
import yaml

from channels.telegram_bridge import TelegramBridge, BridgeConfig


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('telegram_server.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


class TelegramServer:
    """Главный сервер для Telegram бота"""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.bridge: Optional[TelegramBridge] = None
        self.shutdown_event = asyncio.Event()

    def load_config(self) -> BridgeConfig:
        """Загрузка конфигурации из YAML файла"""
        try:
            logger.info(f"Загрузка конфигурации из {self.config_path}")

            if not self.config_path.exists():
                raise FileNotFoundError(f"Файл конфигурации не найден: {self.config_path}")

            with open(self.config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)

            # Извлечь параметры
            telegram_config = config_data.get('telegram', {})
            paths_config = config_data.get('paths', {})
            memory_config = config_data.get('memory', {})
            features_config = config_data.get('features', {})
            access_config = config_data.get('access', {})

            # Получить токен
            telegram_token = telegram_config.get('token')
            if not telegram_token:
                raise ValueError("Telegram token не указан в конфигурации")

            # Получить пути
            workspace_path = Path(paths_config.get('workspace', './workspace'))
            persist_path = Path(paths_config.get('persist', './data'))

            # Создать директории если не существуют
            workspace_path.mkdir(parents=True, exist_ok=True)
            persist_path.mkdir(parents=True, exist_ok=True)

            # Allowed users (опционально)
            allowed_users = access_config.get('allowed_users')

            # Создать BridgeConfig
            bridge_config = BridgeConfig(
                telegram_token=telegram_token,
                workspace_path=workspace_path,
                persist_path=persist_path,
                max_message_history=memory_config.get('max_message_history', 15),
                enable_skills=features_config.get('enable_skills', True),
                enable_transparency=features_config.get('enable_transparency', True),
                allowed_users=allowed_users
            )

            logger.info("✅ Конфигурация успешно загружена")
            logger.info(f"   Workspace: {workspace_path}")
            logger.info(f"   Persist: {persist_path}")
            logger.info(f"   Skills: {'enabled' if bridge_config.enable_skills else 'disabled'}")
            logger.info(f"   Transparency: {'enabled' if bridge_config.enable_transparency else 'disabled'}")

            return bridge_config

        except Exception as e:
            logger.error(f"❌ Ошибка при загрузке конфигурации: {e}")
            raise

    async def run(self):
        """Главный цикл сервера"""
        try:
            logger.info("=" * 60)
            logger.info("🤖 UNIFIED AGENT BOT - Telegram Server")
            logger.info("=" * 60)

            # Загрузить конфигурацию
            config = self.load_config()

            # Создать и запустить bridge
            self.bridge = TelegramBridge(config)
            await self.bridge.start()

            logger.info("✅ Сервер запущен и готов к работе")
            logger.info("Нажмите Ctrl+C для остановки")

            # Ожидание сигнала остановки
            await self.shutdown_event.wait()

        except Exception as e:
            logger.error(f"❌ Критическая ошибка сервера: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise

        finally:
            await self.shutdown()

    async def shutdown(self):
        """Graceful shutdown"""
        try:
            logger.info("🛑 Начало graceful shutdown...")

            if self.bridge:
                await self.bridge.stop()

            logger.info("✅ Shutdown завершен")

        except Exception as e:
            logger.error(f"Ошибка при shutdown: {e}")

    def handle_shutdown_signal(self, signum, frame):
        """Обработчик сигналов остановки"""
        logger.info(f"Получен сигнал {signum}, начинаю остановку...")
        self.shutdown_event.set()


def main():
    """Главная функция"""
    # Парсинг аргументов
    parser = argparse.ArgumentParser(description='Unified Agent Bot - Telegram Server')
    parser.add_argument(
        '--config',
        type=str,
        default='config/telegram_config.yaml',
        help='Путь к файлу конфигурации (по умолчанию: config/telegram_config.yaml)'
    )
    parser.add_argument(
        '--log-level',
        type=str,
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Уровень логирования (по умолчанию: INFO)'
    )

    args = parser.parse_args()

    # Установить уровень логирования
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # Создать сервер
    config_path = Path(args.config)
    server = TelegramServer(config_path)

    # Регистрация обработчиков сигналов для graceful shutdown
    signal.signal(signal.SIGINT, server.handle_shutdown_signal)
    signal.signal(signal.SIGTERM, server.handle_shutdown_signal)

    # Запуск сервера
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logger.info("Получен KeyboardInterrupt")
    except Exception as e:
        logger.error(f"❌ Необработанная ошибка: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)

    logger.info("👋 Сервер остановлен")
    sys.exit(0)


if __name__ == '__main__':
    main()
