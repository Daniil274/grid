"""
Telegram Server - главная точка входа для Unified Agent Bot

Запуск:
    python examples/telegram_bot/telegram_server.py
    python examples/telegram_bot/telegram_server.py --config config.yaml
"""

import sys
import io
from pathlib import Path

# При прямом запуске (python telegram_server.py) корень проекта не в пакете — добавляем в path
if __package__ is None:
    _grid_root = Path(__file__).resolve().parent.parent.parent  # repo root (grid/)
    if str(_grid_root) not in sys.path:
        sys.path.insert(0, str(_grid_root))

# Настроить stdout и stderr для UTF-8 СРАЗУ (fix для Windows эмодзи)
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import asyncio
import argparse
import logging
import signal
import os
from typing import Optional
import yaml
from dotenv import load_dotenv

if __package__ is None:
    from examples.telegram_bot.telegram_bridge import TelegramBridge, BridgeConfig
else:
    from .telegram_bridge import TelegramBridge, BridgeConfig

# Загрузка переменных окружения
load_dotenv()


def setup_logging():
    """Настройка системы логирования"""
    # Создать директорию logs если её нет
    logs_dir = Path('logs')
    logs_dir.mkdir(exist_ok=True)

    # Очистить существующие handlers (если есть)
    root_logger = logging.getLogger()
    if root_logger.handlers:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

    # Настройка форматтера
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # File handler для verbose.log
    verbose_handler = logging.FileHandler('logs/verbose.log', encoding='utf-8', mode='a')
    verbose_handler.setLevel(logging.DEBUG)
    verbose_handler.setFormatter(formatter)
    root_logger.addHandler(verbose_handler)

    # File handler для errors.log
    error_handler = logging.FileHandler('logs/errors.log', encoding='utf-8', mode='a')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)  # В консоль только INFO и выше
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Установить уровень для root logger
    root_logger.setLevel(logging.DEBUG)

    # Настройка уровней для внешних библиотек
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("openai.agents").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("grid").setLevel(logging.DEBUG)

    return logging.getLogger("grid.telegram_server")


# Настроить логирование
logger = setup_logging()

class TelegramServer:
    """Главный сервер для Telegram бота"""

    def __init__(self, config_path: Path, working_directory_override: Optional[str] = None):
        self.config_path = config_path
        self.working_directory_override = working_directory_override
        self.bridge: Optional[TelegramBridge] = None
        self.shutdown_event = asyncio.Event()
        self._shutdown_requested = False

    @staticmethod
    def _resolve_cli_path(path_value: str) -> Path:
        """Resolve a CLI path relative to the current shell working directory."""
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False)

    @staticmethod
    def _resolve_config_path_value(path_value: str, config_dir: Path) -> Path:
        """Resolve a config-defined path relative to config.yaml."""
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = config_dir / path
        return path.resolve(strict=False)

    def load_config(self) -> BridgeConfig:
        """Загрузка конфигурации из YAML файла"""
        try:
            logger.info(f"Загрузка конфигурации из {self.config_path}")

            if not self.config_path.exists():
                raise FileNotFoundError(f"Файл конфигурации не найден: {self.config_path}")

            with open(self.config_path, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)

            # Извлечь параметры telegram
            telegram_config = config_data.get('telegram', {})
            settings_config = config_data.get('settings', {}) or {}
            if not telegram_config:
                raise ValueError("Секция 'telegram' не найдена в конфигурации")

            # Получить токен из переменной окружения
            token_env = telegram_config.get('token_env', 'TELEGRAM_BOT_TOKEN')
            telegram_token = os.getenv(token_env)
            if not telegram_token:
                raise ValueError(f"Telegram token не найден в переменной окружения {token_env}")

            # Получить пути. Относительные пути привязываем к директории config.yaml,
            # а не к текущему cwd процесса.
            config_dir = self.config_path.parent.resolve()
            configured_working_directory = settings_config.get('working_directory') if 'working_directory' in settings_config else None
            configured_workspace_path = telegram_config.get('workspace_path')

            if self.working_directory_override:
                workspace_path = self._resolve_cli_path(self.working_directory_override)
                workspace_source = '--working-directory'
            elif configured_working_directory:
                workspace_path = self._resolve_config_path_value(configured_working_directory, config_dir)
                workspace_source = 'settings.working_directory'
            else:
                workspace_path = self._resolve_config_path_value(
                    configured_workspace_path or './workspace',
                    config_dir,
                )
                workspace_source = 'telegram.workspace_path'

            persist_path = self._resolve_config_path_value(
                telegram_config.get('persist_path', './data'),
                config_dir,
            )

            if configured_working_directory and configured_workspace_path:
                resolved_telegram_workspace = self._resolve_config_path_value(configured_workspace_path, config_dir)
                if resolved_telegram_workspace != workspace_path and workspace_source == 'settings.working_directory':
                    logger.info(
                        "Telegram workspace_path differs from settings.working_directory; "
                        "using settings.working_directory for agent workspace base"
                    )

            # Создать директории если не существуют
            workspace_path.mkdir(parents=True, exist_ok=True)
            persist_path.mkdir(parents=True, exist_ok=True)

            # Прокси: из конфига или из переменных HTTPS_PROXY / HTTP_PROXY
            proxy_url = telegram_config.get('proxy') or os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY')

            if proxy_url:
                os.environ['HTTP_PROXY'] = proxy_url
                os.environ['HTTPS_PROXY'] = proxy_url
                # Также важно исключить локальные адреса, чтобы не ломать локальные сервисы (MCP, локальные LLM)
                if not os.getenv('NO_PROXY'):
                    os.environ['NO_PROXY'] = "localhost,127.0.0.1,0.0.0.0,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"
                
                logger.info(f"🌍 Установлены глобальные настройки прокси: {proxy_url}")
                logger.info(f"   NO_PROXY: {os.environ.get('NO_PROXY')}")

            # Создать BridgeConfig
            bridge_config = BridgeConfig(
                config_path=self.config_path.resolve(),
                telegram_token=telegram_token,
                workspace_path=workspace_path,
                persist_path=persist_path,
                max_message_history=telegram_config.get('max_message_history', 15),
                enable_transparency=telegram_config.get('enable_transparency', True),
                show_tool_calls=telegram_config.get('show_tool_calls', True),
                allowed_users=telegram_config.get('allowed_users'),
                max_concurrent_tasks_per_user=telegram_config.get('max_concurrent_tasks_per_user', 1),
                progress_update_interval=telegram_config.get('progress_update_interval', 1.0),
                proxy_url=proxy_url,
            )

            logger.info("✅ Конфигурация успешно загружена")
            logger.info(f"   Workspace: {workspace_path}")
            logger.info(f"   Workspace source: {workspace_source}")
            logger.info(f"   Persist: {persist_path}")
            logger.info(f"   Transparency: {'enabled' if bridge_config.enable_transparency else 'disabled'}")
            logger.info(f"   Tool calls logging: {'enabled' if bridge_config.show_tool_calls else 'disabled'}")

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
        """Graceful shutdown с таймаутом, чтобы не висеть при зависании stop()."""
        shutdown_timeout = 15.0
        try:
            logger.info("🛑 Начало graceful shutdown...")

            if self.bridge:
                try:
                    await asyncio.wait_for(self.bridge.stop(), timeout=shutdown_timeout)
                except asyncio.TimeoutError:
                    logger.warning(f"Shutdown не завершился за {shutdown_timeout}s, выход")

            logger.info("✅ Shutdown завершен")

        except Exception as e:
            logger.error(f"Ошибка при shutdown: {e}")

    def handle_shutdown_signal(self, signum, _frame):
        """Обработчик сигналов остановки. Второй Ctrl+C — принудительный выход."""
        if self._shutdown_requested:
            logger.warning("Повторный сигнал остановки — принудительный выход")
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os._exit(1)
        self._shutdown_requested = True
        logger.info(f"Получен сигнал {signum}, начинаю остановку...")
        self.shutdown_event.set()


def main():
    """Главная функция"""
    # Парсинг аргументов
    parser = argparse.ArgumentParser(description='Unified Agent Bot - Telegram Server')
    parser.add_argument(
        '--config',
        type=str,
        default='config.yaml',
        help='Путь к файлу конфигурации (по умолчанию: config.yaml)'
    )
    parser.add_argument(
        '--log-level',
        type=str,
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Уровень логирования (по умолчанию: INFO)'
    )

    parser.add_argument(
        '--working-directory',
        type=str,
        help='Переопределить базовую рабочую директорию для Telegram user workspaces'
    )

    args = parser.parse_args()

    # Установить уровень логирования
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # Создать сервер
    config_path = Path(args.config)
    server = TelegramServer(config_path, working_directory_override=args.working_directory)

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
